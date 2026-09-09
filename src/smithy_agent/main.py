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
import re
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
# Assets / child environment
# ------------------------------------------------------------------


def _asset_env_vars(assets: list[dict[str, Any]]) -> dict[str, str]:
    """Build ``SMITHY_ASSET_*`` env vars for a deployed process.

    Matches the engine's EnvAssetProvider contract:
    * text asset ``crm-url``  -> ``SMITHY_ASSET_CRM_URL``
    * credential ``crm`` with fields login/password ->
      ``SMITHY_ASSET_CRM_LOGIN`` / ``SMITHY_ASSET_CRM_PASSWORD``
    """
    env: dict[str, str] = {}
    for asset in assets:
        base = "SMITHY_ASSET_" + re.sub(r"[^A-Za-z0-9_]", "_", asset.get("name", "")).upper()
        if asset.get("kind") == "credential":
            for field, value in (asset.get("fields") or {}).items():
                if value != "":
                    env[f"{base}_{re.sub(r'[^A-Za-z0-9_]', '_', field).upper()}"] = str(value)
        elif asset.get("value"):
            env[base] = str(asset["value"])
    return env


async def _run_env(client: OrchestratorClient) -> dict[str, str] | None:
    """Env additions for a run: cloud coordinates + assets.

    Returns None when the orchestrator cannot be reached for assets — the
    run proceeds with the base environment rather than dying.
    """
    env = {
        "SMITHY_ORCHESTRATOR_URL": client.orchestrator_url,
        "SMITHY_AGENT_TOKEN": client.agent_secret or "",
        "SMITHY_AGENT_VERSION": agent_version(),
    }
    try:
        assets = await client.get_assets()
    except Exception:
        logger.warning("Could not fetch assets — running without SMITHY_ASSET_* vars")
        return None
    env.update(_asset_env_vars(assets))
    return env


# ------------------------------------------------------------------
# Command execution
# ------------------------------------------------------------------


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
            "process_data": {"files": {...}, "entry_point": "...", "requirements": [...]},
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
                await executor.deploy(
                    process_id,
                    process_data.get("files", {}),
                    process_data.get("requirements", []),
                )
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

        logger.info("Executing run %s (process %s)", run_id, process_id)
        _run_to_process[run_id] = process_id
        failed = False
        try:
            # Report running state before doing any work
            await client.report_status(run_id, "running")

            # Deploy files and set up venv
            await executor.deploy(
                process_id,
                process_data.get("files", {}),
                process_data.get("requirements", []),
            )

            # Run the process (cloud coordinates + assets ride in the env)
            extra_env = await _run_env(client)
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
    """Best-effort screenshot on failure: must never mask the real error."""
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
