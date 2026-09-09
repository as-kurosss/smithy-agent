"""P0: deploy must refuse paths escaping the process directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from smithy_agent.executor import ProcessExecutor


@pytest.mark.parametrize(
    "evil_path",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "../evil.py",
        "sub/../../evil.py",
        "..",
    ],
)
async def test_deploy_rejects_escaping_paths(tmp_path: Path, evil_path: str) -> None:
    executor = ProcessExecutor(tmp_path)
    with pytest.raises(ValueError, match="outside process dir"):
        await executor.deploy("p1", {evil_path: "malicious"}, [])


async def test_deploy_writes_nested_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ProcessExecutor, "_run_cmd", _noop)

    executor = ProcessExecutor(tmp_path)
    proc_dir = await executor.deploy("p1", {"main.py": "print(1)", "pkg/mod.py": "x = 1"}, [])
    assert (proc_dir / "main.py").read_text(encoding="utf-8") == "print(1)"
    assert (proc_dir / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1"
