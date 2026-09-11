"""Pack delivery: download, safe extraction, manifest verification, run wiring."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from smithcore_agent import main as agent_main
from smithcore_agent.client import OrchestratorClient
from smithcore_agent.executor import (
    ProcessExecutor,
    _flow_run_args,
    _safe_extract,
    _verify_pack,
)


def make_pack_zip(
    files: dict[str, bytes], *, name: str = "01_notepad", version: str = "1.0.0"
) -> bytes:
    manifest = {
        "schema": "smithcore-pack-v1",
        "name": name,
        "version": version,
        "entry": {},
        "files": [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in files.items()
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
        archive.writestr("pack.json", json.dumps(manifest))
    return buf.getvalue()


def test_safe_extract_rejects_zip_slip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("../evil.py", "x = 1")
    with pytest.raises(ValueError, match="Unsafe path"):
        _safe_extract(buf.getvalue(), tmp_path)
    assert not (tmp_path.parent / "evil.py").exists()


def test_safe_extract_rejects_windows_drive(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("C:/Windows/win.ini", "x")
    with pytest.raises(ValueError, match="Unsafe path"):
        _safe_extract(buf.getvalue(), tmp_path)


def test_verify_pack_detects_tampering(tmp_path: Path) -> None:
    _safe_extract(make_pack_zip({"main.py": b"print(1)"}), tmp_path)
    assert _verify_pack(tmp_path) == []
    (tmp_path / "main.py").write_text("print(2)", encoding="utf-8")
    assert any("checksum mismatch" in problem for problem in _verify_pack(tmp_path))


async def test_deploy_pack_extracts_and_drops_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ProcessExecutor, "_run_cmd", _noop)
    executor = ProcessExecutor(tmp_path)
    process_id = "123e4567-e89b-12d3-a456-426614174000"

    first = make_pack_zip({"main.py": b"print('v1')", "old.py": b"old"})
    proc_dir = await executor.deploy(process_id, {}, ["smithcore-engine"], pack_data=first)
    assert (proc_dir / "main.py").read_text(encoding="utf-8") == "print('v1')"
    assert (proc_dir / "old.py").exists()

    second = make_pack_zip({"main.py": b"print('v2')"})
    await executor.deploy(process_id, {}, ["smithcore-engine"], pack_data=second)
    assert (proc_dir / "main.py").read_text(encoding="utf-8") == "print('v2')"
    assert not (proc_dir / "old.py").exists()


async def test_deploy_pack_rejects_tampered_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ProcessExecutor, "_run_cmd", _noop)
    executor = ProcessExecutor(tmp_path)

    corrupted = bytearray(make_pack_zip({"main.py": b"print(1)"}))
    with zipfile.ZipFile(io.BytesIO(bytes(corrupted))) as archive:
        manifest = json.loads(archive.read("pack.json"))
    manifest["files"][0]["sha256"] = "0" * 64
    with zipfile.ZipFile(io.BytesIO(bytes(corrupted))) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["pack.json"] = json.dumps(manifest).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(ValueError, match="failed verification"):
        await executor.deploy("p1", {}, [], pack_data=buf.getvalue())


def test_pack_requirements_injects_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMITHCORE_PACK_REQUIREMENT", raising=False)
    assert agent_main._pack_requirements([]) == ["smithcore-engine[windows]"]
    assert agent_main._pack_requirements(["smithcore-engine"]) == ["smithcore-engine"]
    assert agent_main._pack_requirements(["requests"]) == ["requests", "smithcore-engine[windows]"]


def test_pack_requirements_respects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMITHCORE_PACK_REQUIREMENT", "")
    assert agent_main._pack_requirements([]) == []
    monkeypatch.setenv("SMITHCORE_PACK_REQUIREMENT", "smithcore-engine==0.8.4")
    assert agent_main._pack_requirements(["requests"]) == ["requests", "smithcore-engine==0.8.4"]


def test_is_pack_run_requires_name_and_version() -> None:
    assert agent_main._is_pack_run({"pack": {"name": "01_notepad", "version": "1.0.1"}})
    assert not agent_main._is_pack_run({"pack": {"name": "01_notepad"}})
    assert not agent_main._is_pack_run({"files": {}})


def test_flow_run_args_prefers_manifest_stage(tmp_path: Path) -> None:
    (tmp_path / "pack.json").write_text(
        json.dumps({"entry": {"process": "process.flow.json"}}), encoding="utf-8"
    )
    (tmp_path / "process.flow.json").write_text("{}", encoding="utf-8")
    assert _flow_run_args(tmp_path) == ["--pack", str(tmp_path), "--stage", "process"]


def test_flow_run_args_falls_back_to_convention(tmp_path: Path) -> None:
    (tmp_path / "pack.json").write_text(json.dumps({"entry": {}}), encoding="utf-8")
    (tmp_path / "01_notepad_flow.json").write_text("{}", encoding="utf-8")
    assert _flow_run_args(tmp_path) == ["01_notepad_flow.json"]


def test_flow_run_args_without_flow_raises(tmp_path: Path) -> None:
    (tmp_path / "pack.json").write_text(json.dumps({"entry": {}}), encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")
    with pytest.raises(ValueError, match="no flow file"):
        _flow_run_args(tmp_path)


async def test_run_flow_invokes_engine_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = ProcessExecutor(tmp_path)
    proc_dir = executor.processes_dir / "p1"
    proc_dir.mkdir(parents=True)
    (proc_dir / "pack.json").write_text(json.dumps({"entry": {}}), encoding="utf-8")
    (proc_dir / "01_notepad_flow.json").write_text("{}", encoding="utf-8")

    captured: dict[str, Any] = {}

    async def fake_spawn(
        process_id: str, argv: list[str], cwd_dir: Path, env: dict[str, str] | None
    ) -> Any:
        captured["argv"] = argv
        captured["cwd"] = cwd_dir
        return object()

    monkeypatch.setattr(executor, "_spawn", fake_spawn)
    await executor.run_flow("p1")
    assert captured["argv"][1:] == ["-m", "smithcore.run_flow", "01_notepad_flow.json"]
    assert captured["cwd"] == proc_dir


async def test_deploy_process_fetches_pinned_pack() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.fetched: list[tuple[str, str]] = []

        async def fetch_pack(self, name: str, version: str) -> bytes:
            self.fetched.append((name, version))
            return b"zipbytes"

    class FakeExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def deploy(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append((*args, kwargs))

    client, executor = FakeClient(), FakeExecutor()
    await agent_main._deploy_process(
        client,
        executor,
        "p1",
        {"pack": {"name": "01_notepad", "version": "1.0.1"}},
        ["smithcore-engine[windows]"],
    )
    assert client.fetched == [("01_notepad", "1.0.1")]
    assert executor.calls[0][0] == "p1"
    assert executor.calls[0][1] == {}
    assert executor.calls[0][3] == {"pack_data": b"zipbytes"}


async def test_fetch_pack_uses_agent_secret() -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"zipbytes")

    client = OrchestratorClient(
        "http://orch", "agent", "http://agent", agent_id="id", agent_secret="secret"
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orch")
    data = await client.fetch_pack("01_notepad", "1.0.1")
    assert data == b"zipbytes"
    assert captured["url"] == "http://orch/api/packs/01_notepad/versions/1.0.1.zip"
    assert captured["auth"] == "Bearer secret"
    await client.close()
