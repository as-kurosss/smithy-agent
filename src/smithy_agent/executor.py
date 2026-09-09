"""Process execution — deploys files to disk, creates venvs, and runs subprocesses."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
    ) -> Path:
        """Write process files to disk and create/update a virtual environment.

        Parameters
        ----------
        process_id:
            Unique identifier for the process.
        files:
            Mapping of relative file paths to their contents.
        requirements:
            List of pip requirement strings.

        Returns
        -------
        Path
            Absolute path to the deployed process directory.
        """
        proc_dir = self.processes_dir / process_id
        proc_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Deploying process %s to %s", process_id, proc_dir)

        # Write source files (reject paths escaping the process directory)
        base = proc_dir.resolve()
        for rel_path, content in files.items():
            candidate = Path(rel_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"Refusing to write outside process dir: {rel_path!r}")
            file_path = (base / candidate).resolve()
            if file_path != base and base not in file_path.parents:
                raise ValueError(f"Refusing to write outside process dir: {rel_path!r}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            logger.debug("  wrote %s (%d bytes)", rel_path, len(content))

        # Write requirements.txt
        req_path = proc_dir / "requirements.txt"
        req_text = "\n".join(requirements)
        req_path.write_text(req_text, encoding="utf-8")

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
    # Run
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_entry(proc_dir: Path, entry_point: str) -> Path:
        """Resolve entry_point strictly inside proc_dir (traversal guard)."""
        candidate = Path(entry_point)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"Refusing to run outside process dir: {entry_point!r}")
        base = proc_dir.resolve()
        entry = (base / candidate).resolve()
        if entry != base and base not in entry.parents:
            raise ValueError(f"Refusing to run outside process dir: {entry_point!r}")
        return entry

    async def run(
        self,
        process_id: str,
        entry_point: str,
    ) -> asyncio.subprocess.Process:
        """Run the deployed process as a subprocess.

        Parameters
        ----------
        process_id:
            The deployed process id (must have been :meth:`deploy`-ed first).
        entry_point:
            Relative path to the Python entry point, e.g. ``main.py``.

        Returns
        -------
        asyncio.subprocess.Process
            Handle to the running subprocess.
        """
        proc_dir = self.processes_dir / process_id
        python_exe = proc_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            # Non-Windows fallback
            python_exe = proc_dir / ".venv" / "bin" / "python"

        entry = self._resolve_entry(proc_dir, entry_point)
        if not entry.is_file():
            raise FileNotFoundError(f"Entry point {entry_point!r} not found in {proc_dir}")

        logger.info("Running process %s: %s %s", process_id, python_exe, entry)
        # Force UTF-8 stdout/stderr so non-ASCII output (—, кириллица, …)
        # survives the pipe regardless of the Windows locale codepage.
        child_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        proc = await asyncio.create_subprocess_exec(
            str(python_exe),
            str(entry),
            cwd=str(proc_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        self._processes[process_id] = proc
        return proc

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop(self, process_id: str) -> None:
        """Kill a running subprocess by process id."""
        proc = self._processes.get(process_id)
        if proc is None or proc.returncode is not None:
            logger.warning("Process %s is not running — nothing to stop", process_id)
            return

        logger.info("Stopping process %s (pid=%s)", process_id, proc.pid)
        try:
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
    async def _run_cmd(*args: str) -> None:
        """Run a shell command and wait for it to finish."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Command failed ({proc.returncode}): {' '.join(args)}\n"
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
