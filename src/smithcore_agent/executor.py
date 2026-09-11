"""Process execution — deploys files to disk, creates venvs, and runs subprocesses."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

logger = logging.getLogger(__name__)

#: Pack delivery format — mirrors the engine's ``smithcore.pack``.
PACK_MANIFEST = "pack.json"
PACK_SCHEMA = "smithcore-pack-v1"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
#: Tracks the files a pack deploy placed, so a re-deploy can drop stale ones.
_PACK_MARKER = ".pack-files.json"


# Console-less spawning: the agent runs under pythonw (no console). Console
# children (the deployed process, pip, venv) would each open a flashing
# console window - CREATE_NO_WINDOW suppresses it. The child still runs in
# the interactive desktop session, so UIA automation is unaffected.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _check_rel_path(rel: str) -> None:
    """Reject deploy/run paths that could escape the process directory.

    Checks both the platform path view and the Windows view: on POSIX a
    string like ``C:\\Windows\\win.ini`` is a *relative* path whose
    backslashes are not separators, but the agent is Windows-first and the
    orchestrator is untrusted - the Windows interpretation is what matters.
    """
    candidate = Path(rel)
    win = PureWindowsPath(rel)
    if (
        candidate.is_absolute()
        or win.is_absolute()
        or win.drive
        or ".." in candidate.parts
        or ".." in win.parts
    ):
        raise ValueError(f"Refusing path outside process dir: {rel!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: pip directives that turn a requirements line into repository/code execution.
_BLOCKED_REQ_PREFIXES = ("-", "--", "http://", "https://", "git+", "file:", "ftp:")
_BLOCKED_REQ_SUBSTRINGS = ("--extra-index-url", "--trusted-host", "--index-url", "-f ", ";")


def check_requirements(requirements: list[str]) -> None:
    """Reject pip option injection in server-supplied requirements.

    A `requirements` entry must be a plain `name[extra]specifier` line —
    no options (`--extra-index-url`), no URLs, no `git+`, no `-e`, no
    environment markers with `;` (which can invoke setup code paths).
    """
    import re as _re

    # Permissive but option-free: name + optional extras + version specifiers.
    _ok = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?\s*([=<>!~]+.*)?$")
    for req in requirements:
        if not isinstance(req, str) or not req.strip():
            raise ValueError(f"Refusing empty requirement entry: {req!r}")
        line = req.strip()
        lowered = line.lower()
        if line.startswith(_BLOCKED_REQ_PREFIXES) or lowered.startswith(("-e ", "--")):
            raise ValueError(f"Refusing pip option/URL requirement: {line!r}")
        if any(token in line for token in _BLOCKED_REQ_SUBSTRINGS) or "@" in line:
            raise ValueError(f"Refusing complex requirement (URL/option/marker): {line!r}")
        if not _ok.match(line):
            raise ValueError(f"Refusing malformed requirement: {line!r}")


def _safe_extract(data: bytes, dest: Path) -> None:
    """Extract a pack zip into *dest*, rejecting unsafe member paths.

    Mirrors the engine's zip-slip guard: absolute paths, Windows drive
    letters and ``..`` segments are refused before anything is written.
    """
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            name = member.filename
            if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"Unsafe path in pack archive: {name!r}")
            parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
            if any(part == ".." for part in parts):
                raise ValueError(f"Unsafe path in pack archive: {name!r}")
            target = (root / Path(*parts)).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Unsafe path in pack archive: {name!r}")
        archive.extractall(dest)


def _verify_pack(directory: Path) -> list[str]:
    """Verify a pack's manifest in place; return the list of problems.

    Empty list = intact. This repeats the engine's client-side check so a
    tampered archive is rejected before any process code runs.
    """
    manifest_path = directory / PACK_MANIFEST
    if not manifest_path.is_file():
        return [f"no {PACK_MANIFEST} — not a smithcore pack"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest is unreadable: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("schema") != PACK_SCHEMA:
        schema = manifest.get("schema") if isinstance(manifest, dict) else None
        return [f"unknown manifest schema: {schema!r}"]

    problems: list[str] = []
    files = [item for item in (manifest.get("files") or []) if isinstance(item, dict)]
    if not files:
        problems.append("manifest lists no files")
    for item in files:
        rel = str(item.get("path") or "")
        checksum = str(item.get("sha256") or "")
        if not rel or not _SHA256_RE.match(checksum):
            problems.append(f"manifest entry {rel!r}: bad path or sha256")
            continue
        file_path = directory / rel
        if not file_path.is_file():
            problems.append(f"listed file is missing: {rel}")
        elif _sha256_file(file_path) != checksum:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def _flow_run_args(proc_dir: Path) -> list[str]:
    """Arguments for ``python -m smithcore.run_flow`` selecting the pack's flow.

    Prefers a manifest entry stage (``--pack DIR --stage NAME`` so the
    engine also picks up ``tools.py``/``selectors.json``); falls back to the
    conventional ``*_flow.json`` / ``*.flow.json`` member. Raises
    :class:`ValueError` when the pack carries no flow at all.
    """
    entry: dict[str, str] = {}
    manifest_path = proc_dir / PACK_MANIFEST
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if isinstance(manifest, dict) and isinstance(manifest.get("entry"), dict):
            entry = {str(stage): str(path) for stage, path in manifest["entry"].items()}
    for stage in ("process", "init", "end", *entry):
        flow = entry.get(stage)
        if flow and (proc_dir / flow).is_file():
            return ["--pack", str(proc_dir), "--stage", stage]

    flows = sorted(
        path.name
        for path in proc_dir.iterdir()
        if path.is_file()
        and path.name != PACK_MANIFEST
        and (path.name.endswith(".flow.json") or path.name.endswith("_flow.json"))
    )
    if not flows:
        raise ValueError(f"pack has no flow file in {proc_dir}")
    args = [flows[0]]
    if (proc_dir / "tools.py").is_file():
        args += ["--tools", "tools.py"]
    return args


class ProcessExecutor:
    """Manages deployed processes on the local machine."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    @property
    def processes_dir(self) -> Path:
        return self.base_dir / "processes"

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(
        self,
        process_id: str,
        files: dict[str, str],
        requirements: list[str],
        *,
        pack_data: bytes | None = None,
    ) -> Path:
        """Write process files to disk and create/update a virtual environment.

        Parameters
        ----------
        process_id:
            Unique identifier for the process.
        files:
            Mapping of relative file paths to their contents. Ignored when
            *pack_data* is given (a pack carries its own file set).
        requirements:
            List of pip requirement strings.
        pack_data:
            Raw zip of a pinned pack version; when supplied it is verified
            and extracted into the process dir instead of *files*.

        Returns
        -------
        Path
            Absolute path to the deployed process directory.
        """
        if not isinstance(requirements, list) or not all(
            isinstance(r, str) for r in requirements
        ):
            raise ValueError("requirements must be a list of strings")
        check_requirements(requirements)
        proc_dir = self.processes_dir / process_id
        proc_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Deploying process %s to %s", process_id, proc_dir)

        # Heavy disk/zip/hash IO off the event loop.
        if pack_data is not None:
            await asyncio.to_thread(self._install_pack, process_id, proc_dir, pack_data)
        else:
            await asyncio.to_thread(self._write_files, proc_dir, files)

        # Write requirements.txt
        req_path = proc_dir / "requirements.txt"
        req_text = "\n".join(requirements)
        await asyncio.to_thread(req_path.write_text, req_text, encoding="utf-8")

        # Create / update virtual environment
        venv_dir = proc_dir / ".venv"
        if not venv_dir.exists():
            logger.info("Creating venv for %s", process_id)
            await self._run_cmd(sys.executable, "-m", "venv", str(venv_dir))

        # Resolve the venv Python — use "python -m pip" throughout
        python_exe = venv_dir / "Scripts" / "python.exe"
        if not python_exe.exists():
            python_exe = venv_dir / "bin" / "python"

        # Skip reinstall when requirements are unchanged (hash marker).
        req_hash = hashlib.sha256(req_text.encode("utf-8")).hexdigest()
        hash_path = proc_dir / ".requirements.hash"
        try:
            cached = hash_path.read_text(encoding="utf-8").strip()
        except OSError:
            cached = ""
        if cached == req_hash and (venv_dir.exists() if not requirements else True):
            logger.info("Requirements unchanged for %s — skipping pip install", process_id)
            return proc_dir

        logger.info("Upgrading pip for %s", process_id)
        await self._run_cmd(
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
        )

        if requirements:
            logger.info("Installing %d dependencies for %s", len(requirements), process_id)
            await self._run_cmd(
                str(python_exe),
                "-m",
                "pip",
                "install",
                "-r",
                str(req_path),
            )
        else:
            logger.info("No dependencies to install for %s", process_id)
        try:
            hash_path.write_text(req_hash, encoding="utf-8")
        except OSError:
            logger.warning("Could not write requirements hash for %s", process_id)

        return proc_dir

    # ------------------------------------------------------------------
    # Deploy internals
    # ------------------------------------------------------------------

    @staticmethod
    def _write_files(proc_dir: Path, files: dict[str, str]) -> None:
        """Write source files, rejecting paths escaping the process dir."""
        base = proc_dir.resolve()
        for rel_path, content in files.items():
            _check_rel_path(rel_path)
            candidate = Path(rel_path)
            file_path = (base / candidate).resolve()
            if file_path != base and base not in file_path.parents:
                raise ValueError(f"Refusing to write outside process dir: {rel_path!r}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            logger.debug("  wrote %s (%d bytes)", rel_path, len(content))

    def _install_pack(self, process_id: str, proc_dir: Path, pack_data: bytes) -> None:
        """Verify and extract a pack zip into *proc_dir*.

        Extraction happens in a scratch dir first so a tampered archive is
        rejected (manifest checksum mismatch, zip-slip) before it can touch
        the deployed process. Files a previous pack deploy left behind are
        removed, so a version rollback does not keep stale sources.
        """
        scratch = Path(tempfile.mkdtemp(dir=self.processes_dir, prefix=f".{process_id}.pack-"))
        try:
            _safe_extract(pack_data, scratch)
            problems = _verify_pack(scratch)
            if problems:
                raise ValueError("pack failed verification: " + "; ".join(problems))
            self._remove_previous_pack_files(proc_dir)
            for item in scratch.iterdir():
                target = proc_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            placed = sorted(
                path.relative_to(scratch).as_posix()
                for path in scratch.rglob("*")
                if path.is_file()
            )
            (proc_dir / _PACK_MARKER).write_text(json.dumps(placed), encoding="utf-8")
            logger.info("Installed pack for %s (%d files)", process_id, len(placed))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _remove_previous_pack_files(proc_dir: Path) -> None:
        marker = proc_dir / _PACK_MARKER
        try:
            listed = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        base = proc_dir.resolve()
        for rel in listed if isinstance(listed, list) else []:
            if not isinstance(rel, str):
                continue
            try:
                _check_rel_path(rel)
            except ValueError:
                continue
            target = (base / rel).resolve()
            if target.is_relative_to(base) and target.is_file():
                target.unlink()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_entry(proc_dir: Path, entry_point: str) -> Path:
        """Resolve entry_point strictly inside proc_dir (traversal guard)."""
        _check_rel_path(entry_point)
        candidate = Path(entry_point)
        base = proc_dir.resolve()
        entry = (base / candidate).resolve()
        if entry != base and base not in entry.parents:
            raise ValueError(f"Refusing to run outside process dir: {entry_point!r}")
        return entry

    async def run(
        self,
        process_id: str,
        entry_point: str,
        *,
        env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """Run the deployed process as a subprocess.

        Parameters
        ----------
        process_id:
            The deployed process id (must have been :meth:`deploy`-ed first).
        entry_point:
            Relative path to the Python entry point, e.g. ``main.py``.
        env:
            Extra environment entries (cloud coordinates, ``SMITHCORE_ASSET_*``
            credentials) merged over ``os.environ``. ``None`` keeps the
            plain base environment.

        Returns
        -------
        asyncio.subprocess.Process
            Handle to the running subprocess.
        """
        proc_dir = self.processes_dir / process_id
        entry = self._resolve_entry(proc_dir, entry_point)
        if not entry.is_file():
            raise FileNotFoundError(f"Entry point {entry_point!r} not found in {proc_dir}")

        python_exe = self._venv_python(proc_dir)
        logger.info("Running process %s: %s %s", process_id, python_exe, entry)
        return await self._spawn(process_id, [str(python_exe), str(entry)], proc_dir, env)

    async def run_flow(
        self,
        process_id: str,
        *,
        env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """Run a pack's flow with the engine (``python -m smithcore.run_flow``).

        Pack processes are flows, not arbitrary code: nothing from the pack
        is executed directly — the engine's runner only dispatches
        registered tools. The working directory is the process dir so the
        engine finds ``selectors.json`` (and ``tools.py``).
        """
        proc_dir = self.processes_dir / process_id
        args = _flow_run_args(proc_dir)
        python_exe = self._venv_python(proc_dir)
        logger.info(
            "Running pack flow %s: %s -m smithcore.run_flow %s",
            process_id,
            python_exe,
            " ".join(args),
        )
        return await self._spawn(
            process_id, [str(python_exe), "-m", "smithcore.run_flow", *args], proc_dir, env
        )

    async def _spawn(
        self,
        process_id: str,
        argv: list[str],
        proc_dir: Path,
        env: dict[str, str] | None,
    ) -> asyncio.subprocess.Process:
        """Spawn a child in *proc_dir* piping output; track it for stop()."""
        # Force UTF-8 stdout/stderr so non-ASCII output (—, кириллица, …)
        # survives the pipe regardless of the Windows locale codepage.
        child_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if env:
            child_env.update(env)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(proc_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            creationflags=_NO_WINDOW,
        )
        self._processes[process_id] = proc
        return proc

    @staticmethod
    def _venv_python(proc_dir: Path) -> Path:
        python_exe = proc_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            # Non-Windows fallback
            python_exe = proc_dir / ".venv" / "bin" / "python"
        return python_exe

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop(self, process_id: str) -> None:
        """Kill a running subprocess — including its child process tree.

        A deployed RPA process routinely spawns children (browsers, office
        apps); on Windows ``terminate()`` would leave that whole tree alive,
        so the tree is killed via ``taskkill /T`` instead.
        """
        proc = self._processes.get(process_id)
        if proc is None or proc.returncode is not None:
            logger.warning("Process %s is not running — nothing to stop", process_id)
            return

        logger.info("Stopping process %s (pid=%s)", process_id, proc.pid)
        try:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                )
                await killer.wait()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            else:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except TimeoutError:
                    logger.warning("Process %s did not terminate — killing", process_id)
                    proc.kill()
                    await proc.wait()
        finally:
            self._processes.pop(process_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_running(self, process_id: str) -> bool:
        """Return *True* if the process is still running."""
        proc = self._processes.get(process_id)
        return proc is not None and proc.returncode is None

    def forget(self, process_id: str) -> None:
        """Drop bookkeeping for a finished process (no-op if unknown)."""
        self._processes.pop(process_id, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_cmd(*args: str, timeout_s: float = 600.0) -> None:
        """Run a shell command and wait for it to finish.

        A hung command (pip stalling on a slow mirror) would otherwise hold
        a run-semaphore slot forever, so the child is killed on timeout.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Command timed out after {timeout_s:.0f}s: {' '.join(args)}"
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"Command failed ({proc.returncode}): {' '.join(args)}\n"
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
