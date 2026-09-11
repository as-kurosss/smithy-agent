"""Command-handling guards: UUID process_id validation and deploy-while-running."""

from __future__ import annotations

import asyncio
from typing import Any

from smithcore_agent import main as agent_main


class FakeClient:
    def __init__(self) -> None:
        self.acks: list[tuple[str, str, str | None]] = []
        self.statuses: list[tuple[str, str]] = []

    async def ack_deployment(
        self, deployment_id: str, status: str, *, error: str | None = None
    ) -> None:
        self.acks.append((deployment_id, status, error))

    async def report_status(self, run_id: str, status: str, **_: Any) -> None:
        self.statuses.append((run_id, status))

    async def push_logs(self, run_id: str, logs: list[dict[str, Any]]) -> None:  # pragma: no cover
        raise AssertionError("not expected in these tests")


class FakeExecutor:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.deploy_calls: list[Any] = []

    def is_running(self, process_id: str) -> bool:
        return self.running

    async def deploy(self, process_id: str, files: Any, requirements: Any) -> None:
        self.deploy_calls.append((process_id, files, requirements))


def _setup() -> None:
    if agent_main._semaphore is None:
        agent_main._semaphore = asyncio.Semaphore(4)


async def test_malformed_process_id_is_rejected() -> None:
    _setup()
    client, executor = FakeClient(), FakeExecutor()
    cmd = {
        "command": "run",
        "process_id": "..\\..\\evil",
        "run_id": "r1",
        "process_data": {"files": {}, "entry_point": "main.py", "requirements": []},
    }
    await agent_main.execute_command(client, executor, cmd)  # type: ignore[arg-type]
    assert executor.deploy_calls == []
    assert client.statuses == []


async def test_windows_drive_process_id_is_rejected() -> None:
    _setup()
    client, executor = FakeClient(), FakeExecutor()
    cmd = {
        "command": "deploy",
        "process_id": "C:\\Windows\\Temp",
        "process_data": {"deployment_id": "d1", "files": {}, "requirements": []},
    }
    await agent_main.execute_command(client, executor, cmd)  # type: ignore[arg-type]
    assert executor.deploy_calls == []
    assert client.acks == []


async def test_deploy_while_running_acks_failed() -> None:
    _setup()
    client, executor = FakeClient(), FakeExecutor(running=True)
    cmd = {
        "command": "deploy",
        "process_id": "123e4567-e89b-12d3-a456-426614174000",
        "process_data": {"deployment_id": "d1", "files": {}, "requirements": []},
    }
    await agent_main.execute_command(client, executor, cmd)  # type: ignore[arg-type]
    assert executor.deploy_calls == []
    assert client.acks == [("d1", "failed", None)] or client.acks[0][1] == "failed"


async def test_valid_deploy_proceeds_and_acks() -> None:
    _setup()
    client, executor = FakeClient(), FakeExecutor()
    cmd = {
        "command": "deploy",
        "process_id": "123e4567-e89b-12d3-a456-426614174000",
        "process_data": {"deployment_id": "d1", "files": {}, "requirements": []},
    }
    await agent_main.execute_command(client, executor, cmd)  # type: ignore[arg-type]
    assert len(executor.deploy_calls) == 1
    assert client.acks == [("d1", "deployed", None)]
