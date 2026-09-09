"""Agent service: task command generation + config file roundtrip."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_task_command_defaults() -> None:
    from smithy_agent.service import TASK_NAME, task_command

    cmd = task_command("C:/py/python.exe")
    assert "-m smithy_agent.service run" in cmd
    assert "New-ScheduledTaskTrigger -AtLogOn" in cmd
    assert f"'{TASK_NAME}'" in cmd
    assert "RestartCount 999" in cmd
    assert "LogonType" not in cmd  # no principal when no --user given


def test_task_command_with_user() -> None:
    from smithy_agent.service import task_command

    cmd = task_command("C:/py/python.exe", user="WORKBOX\\rpa")
    assert "New-ScheduledTaskTrigger -AtLogOn -User 'WORKBOX\\rpa'" in cmd
    assert "LogonType Interactive" in cmd
    assert "-Principal $principal" in cmd


def test_config_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import smithy_agent.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")

    cfg.save_config("http://orch:8000", "agent-1", "http://1.2.3.4:8001", join_token="tok")
    loaded = cfg.load_config()
    assert loaded["orchestrator_url"] == "http://orch:8000"
    assert loaded["agent_name"] == "agent-1"
    assert loaded["agent_url"] == "http://1.2.3.4:8001"
    assert loaded["join_token"] == "tok"

    assert cfg.delete_config() is True
    assert cfg.load_config() == {}
    assert cfg.delete_config() is False  # already gone — idempotent


def test_missing_config_fails_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import smithy_agent.config as cfg
    import smithy_agent.service as svc

    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
    with pytest.raises(SystemExit) as exc:
        svc.run_forever()
    assert exc.value.code == 2
    assert "install" in capsys.readouterr().err
