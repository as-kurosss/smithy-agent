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
from pathlib import Path
from typing import Any

from rich.console import Console

from smithy_agent.client import (
    HEARTBEAT_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    OrchestratorClient,
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
        process_data: dict[str, Any] = cmd.get("process_data", {})
        run_id: str | None = cmd.get("run_id")

        logger.info("Received command %r for process %s", command, process_id)

        # Deploy-only command: write files / prepare the venv, but do not start a run.
        if command == "deploy":
            deployment_id = (
                process_data.get("deployment_id") if isinstance(process_data, dict) else None
            )
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
        try:
            # Report running state before doing any work
            await client.report_status(run_id, "running")

            # Deploy files and set up venv
            await executor.deploy(
                process_id,
                process_data.get("files", {}),
                process_data.get("requirements", []),
            )

            # Run the process
            proc = await executor.run(process_id, process_data["entry_point"])

            # Stream logs back to orchestrator
            streamer = LogStreamer(client, run_id)
            await streamer.stream(proc)

            # Report final status
            if proc.returncode == 0:
                await client.report_status(run_id, "completed")
            else:
                await client.report_status(
                    run_id,
                    "failed",
                    error=f"Process exited with code {proc.returncode}",
                )
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            await client.report_status(run_id, "failed", error=str(exc))
        finally:
            _run_to_process.pop(run_id, None)
            executor.forget(process_id)


# ------------------------------------------------------------------
# Main agent loop
# ------------------------------------------------------------------


async def run_agent(
    orchestrator_url: str,
    agent_name: str,
    agent_url: str,
    join_token: str | None = None,
) -> None:
    """Core agent lifecycle: register, heartbeat, poll, execute."""
    global _semaphore
    _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)

    client = OrchestratorClient(orchestrator_url, agent_name, agent_url)
    executor = ProcessExecutor(Path.home() / ".smithy-agent")

    try:
        await client.register(join_token=join_token or os.environ.get("SMITHY_JOIN_TOKEN"))
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
