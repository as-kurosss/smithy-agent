"""Entry point for the Smithy agent.

Run with::

    smithy-agent --orchestrator http://localhost:8000 --name my-agent --url http://localhost:8001
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from smithy_agent.client import (
    HEARTBEAT_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    OrchestratorClient,
    agent_version,
)
from smithy_agent.executor import ProcessExecutor
from smithy_agent.streamer import LogStreamer

logger = logging.getLogger("smithy_agent")
console = Console()

_running_tasks: set[asyncio.Task[None]] = set()
_MAX_CONCURRENT_RUNS = 4
_MAX_COMMANDS_PER_POLL = 100
_semaphore: asyncio.Semaphore | None = None
_run_to_process: dict[str, str] = {}


# ------------------------------------------------------------------
# Heartbeat loop
# ------------------------------------------------------------------


async def heartbeat_loop(client: OrchestratorClient) -> None:
    """Send a heartbeat to the orchestrator every 30 seconds."""
    while True:
        try:
            await client.heartbeat()
        except Exception:
            logger.exception("Heartbeat failed")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS + random.uniform(0, 2.0))


# ------------------------------------------------------------------
# Run environment
# ------------------------------------------------------------------


async def _run_env(client: OrchestratorClient, process_id: str) -> dict[str, str]:
    """Env additions for a run: orchestrator coordinates for on-demand lookups.

    Assets are **not** dumped into the environment: the engine fetches one
    asset by id/GUID or name from the orchestrator at run time
    (``HttpAssetProvider``), scoped to *process_id* so a flow can only read
    the assets its process is allowed.

    NOTE: ``SMITHY_AGENT_TOKEN`` is the agent credential — deployed process
    code is trusted to the same degree as the agent itself. Do not run
    unreviewed third-party flows with production credentials; scope assets
    per process server-side.
    """
    return {
        "SMITHY_ORCHESTRATOR_URL": client.orchestrator_url,
        "SMITHY_AGENT_ID": client.agent_id or "",
        "SMITHY_AGENT_TOKEN": client.agent_secret or "",
        "SMITHY_AGENT_VERSION": agent_version(),
        "SMITHY_PROCESS_ID": process_id,
    }


# ------------------------------------------------------------------
# Command execution
# ------------------------------------------------------------------

#: A pack ships flows, not Python dependencies — the engine must be present
#: for the deployed ``main.py`` to import ``smithy``. Override via
#: ``SMITHY_PACK_REQUIREMENT`` (empty string disables the injection).
_DEFAULT_ENGINE_REQUIREMENT = "smithy-engine[windows]"


def _pack_requirements(requirements: list[str]) -> list[str]:
    """Ensure a pack run installs the smithy engine unless already covered."""
    override = os.environ.get("SMITHY_PACK_REQUIREMENT")
    if override is not None:
        extra = override.strip()
        if not extra:
            return requirements
    else:
        extra = _DEFAULT_ENGINE_REQUIREMENT
    if any("smithy" in req for req in requirements):
        return requirements
    return [*requirements, extra]


def _is_pack_run(process_data: dict[str, Any]) -> bool:
    """True when the command references a pinned pack (name + version)."""
    pack = process_data.get("pack")
    return isinstance(pack, dict) and bool(pack.get("name")) and bool(pack.get("version"))


async def _deploy_process(
    client: OrchestratorClient,
    executor: ProcessExecutor,
    process_id: str,
    process_data: dict[str, Any],
    requirements: list[str],
) -> None:
    """Materialize process code: a pinned pack zip, or inline files."""
    if _is_pack_run(process_data):
        pack = process_data["pack"]
        data = await client.fetch_pack(str(pack["name"]), str(pack["version"]))
        await executor.deploy(process_id, {}, requirements, pack_data=data)
    else:
        await executor.deploy(process_id, process_data.get("files", {}), requirements)


async def execute_command(
    client: OrchestratorClient,
    executor: ProcessExecutor,
    cmd: dict[str, Any],
) -> None:
    """Execute a single command received from the orchestrator.

    The orchestrator sends commands in the form::

        {
            "command": "run",
            "process_id": "...",
            "run_id": "...",
            "process_data": {
                "files": {...},
                "entry_point": "...",
                "requirements": [...],
                # pack-based process: {"name": "01_notepad", "version": "1.0.1"}
                "pack": {...},
            },
        }
    """
    if _semaphore is None:
        raise RuntimeError("_semaphore must be initialized by run_agent")
    async with _semaphore:
        command: str = cmd.get("command", "run")
        process_id: str = cmd["process_id"]
        # The orchestrator is untrusted: process_id is interpolated into
        # filesystem paths by the executor, so it must be a UUID — anything
        # else (e.g. "..\..\evil") is rejected before touching the disk.
        try:
            uuid.UUID(str(process_id))
        except (ValueError, AttributeError):
            logger.warning("Command with malformed process_id %r — ignoring", process_id)
            return
        process_data: dict[str, Any] = cmd.get("process_data", {})
        run_id: str | None = cmd.get("run_id")

        logger.info("Received command %r for process %s", command, process_id)

        # Deploy-only command: write files / prepare the venv, but do not start a run.
        if command == "deploy":
            deployment_id = (
                process_data.get("deployment_id") if isinstance(process_data, dict) else None
            )
            if executor.is_running(process_id):
                # Redeploying over a running process would rewrite its files
                # (and possibly its venv) underneath it — refuse instead.
                error = f"Process {process_id} is currently running — deploy refused"
                logger.warning(error)
                if deployment_id is not None:
                    try:
                        await client.ack_deployment(str(deployment_id), "failed", error=error)
                    except Exception:
                        logger.exception("Deploy ack for process %s failed", process_id)
                return
            try:
                requirements = process_data.get("requirements", [])
                if not isinstance(requirements, list) or not all(
                    isinstance(r, str) for r in requirements
                ):
                    raise ValueError("malformed requirements (must be a list of strings)")
                if _is_pack_run(process_data):
                    requirements = _pack_requirements(requirements)
                await _deploy_process(client, executor, process_id, process_data, requirements)
                if deployment_id is not None:
                    await client.ack_deployment(str(deployment_id), "deployed")
            except Exception as exc:
                logger.exception("Deploy for process %s failed", process_id)
                if deployment_id is not None:
                    try:
                        await client.ack_deployment(str(deployment_id), "failed", error=str(exc))
                    except Exception:
                        logger.exception("Deploy ack for process %s failed", process_id)
            return

        if command == "stop":
            if run_id is None:
                logger.warning("Stop command for process %s has no run_id — skipping", process_id)
                return
            target = _run_to_process.get(run_id, process_id)
            if executor.is_running(target):
                await executor.stop(target)
                await client.report_status(run_id, "stopped", error="Stopped by user")
            else:
                logger.warning("Stop for run %s: process %s not running", run_id, target)
                await client.report_status(run_id, "stopped", error="Process was not running")
            _run_to_process.pop(run_id, None)
            executor.forget(target)
            return

        if command != "run":
            logger.warning("Ignoring unsupported command %r", command)
            return

        if run_id is None:
            logger.warning("Run command for process %s has no run_id — skipping", process_id)
            return
        requirements = process_data.get("requirements", [])
        if not isinstance(requirements, list) or not all(
            isinstance(r, str) for r in requirements
        ):
            logger.warning("Run %s has malformed requirements — ignoring", run_id)
            return

        logger.info("Executing run %s (process %s)", run_id, process_id)
        _run_to_process[run_id] = process_id
        failed = False
        try:
            # Report running state before doing any work
            await client.report_status(run_id, "running")

            # Deploy the pinned pack (or inline files) and set up the venv
            is_pack = _is_pack_run(process_data)
            requirements = process_data.get("requirements", [])
            if is_pack:
                requirements = _pack_requirements(requirements)
            await _deploy_process(client, executor, process_id, process_data, requirements)

            # Run the process (cloud coordinates + asset scoping ride in the env).
            # A pack is a flow, not code: the engine's runner executes it.
            extra_env = await _run_env(client, process_id)
            if is_pack:
                proc = await executor.run_flow(process_id, env=extra_env)
            else:
                proc = await executor.run(
                    process_id,
                    process_data["entry_point"],
                    env=extra_env,
                )

            # Stream logs back to orchestrator
            streamer = LogStreamer(client, run_id)
            await streamer.stream(proc)

            # Report final status
            if proc.returncode == 0:
                await client.report_status(run_id, "completed")
            else:
                failed = True
                await client.report_status(
                    run_id,
                    "failed",
                    error=f"Process exited with code {proc.returncode}",
                )
        except Exception as exc:
            failed = True
            logger.exception("Run %s failed", run_id)
            await client.report_status(run_id, "failed", error=str(exc))
        finally:
            if failed:
                await _attach_failure_screenshot(client, run_id)
            _run_to_process.pop(run_id, None)
            executor.forget(process_id)


async def _attach_failure_screenshot(client: OrchestratorClient, run_id: str) -> None:
    """Best-effort screenshot on failure: must never mask the real error.

    Opt-out via ``SMITHY_SCREENSHOT_ON_FAILURE=0``: screenshots capture the
    whole virtual desktop and may contain passwords/PII.
    """
    import os as _os

    if _os.environ.get("SMITHY_SCREENSHOT_ON_FAILURE", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return
    from smithy_agent.screenshot import capture_screenshot

    try:
        shot = capture_screenshot()
        if shot is None:
            return
        data, filename = shot
        await client.push_artifact(run_id, filename, "image/png", data)
        logger.info("Failure screenshot attached to run %s (%d bytes)", run_id, len(data))
    except Exception:
        logger.exception("Failed to attach screenshot for run %s", run_id)


# ------------------------------------------------------------------
# Main agent loop
# ------------------------------------------------------------------


async def run_agent(
    orchestrator_url: str,
    agent_name: str,
    agent_url: str,
    join_token: str | None = None,
    *,
    agent_id: str | None = None,
    agent_secret: str | None = None,
    on_credentials: Callable[[str, str], None] | None = None,
) -> None:
    """Core agent lifecycle: register, heartbeat, poll, execute.

    When persisted credentials (``agent_id``/``agent_secret``) are supplied
    they are presented on registration to prove ownership of the existing
    agent entry, so restarts do not rotate the secret unnecessarily.
    """
    global _semaphore
    _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)

    from smithy_agent.config import check_orchestrator_url

    try:
        check_orchestrator_url(orchestrator_url)
    except ValueError as exc:
        raise SystemExit(f"refusing orchestrator URL: {exc}") from exc

    client = OrchestratorClient(
        orchestrator_url,
        agent_name,
        agent_url,
        join_token=join_token or os.environ.get("SMITHY_JOIN_TOKEN"),
        agent_id=agent_id,
        agent_secret=agent_secret,
        on_credentials=on_credentials,
    )
    executor = ProcessExecutor(Path.home() / ".smithy-agent")

    try:
        if client.agent_id and client._secret:
            console.print(f"[green]Agent {agent_name!r} reusing saved credentials[/green]")
        else:
            await client.register()
            console.print(f"[green]Agent {agent_name!r} registered with {orchestrator_url}[/green]")

        # Start background heartbeat
        heartbeat_task = asyncio.create_task(heartbeat_loop(client))
        _running_tasks.add(heartbeat_task)
        heartbeat_task.add_done_callback(_running_tasks.discard)

        # Main polling loop (jitter avoids thundering herd of many agents).
        console.print("[cyan]Polling for commands…[/cyan]")
        while True:
            try:
                commands = await client.poll()
                if len(commands) > _MAX_COMMANDS_PER_POLL:
                    logger.warning(
                        "Poll returned %d commands, capping to %d",
                        len(commands),
                        _MAX_COMMANDS_PER_POLL,
                    )
                    commands = commands[:_MAX_COMMANDS_PER_POLL]
                for cmd in commands:
                    task = asyncio.create_task(execute_command(client, executor, cmd))
                    _running_tasks.add(task)
                    task.add_done_callback(_running_tasks.discard)
            except Exception:
                logger.exception("Poll cycle failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, 1.0))
    finally:
        # Cancel all running tasks
        for task in list(_running_tasks):
            task.cancel()
        if _running_tasks:
            await asyncio.gather(*_running_tasks, return_exceptions=True)
        _running_tasks.clear()
        await client.close()


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and start the agent."""
    parser = argparse.ArgumentParser(
        prog="smithy-agent",
        description="Smithy Cloud agent — communicates with the orchestrator.",
    )
    parser.add_argument(
        "--orchestrator",
        required=True,
        help="URL of the orchestrator (e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Human-readable name for this agent",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Public URL this agent is reachable at",
    )
    parser.add_argument(
        "--join-token",
        default=None,
        help="Join token for auth-on orchestrators (or SMITHY_JOIN_TOKEN).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    try:
        asyncio.run(run_agent(args.orchestrator, args.name, args.url, join_token=args.join_token))
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped.[/yellow]")
