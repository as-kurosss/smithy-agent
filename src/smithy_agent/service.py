"""Run the agent unattended: crash-restart loop + Windows scheduled task.

Why a scheduled task instead of a Windows service: UI automation (UIA)
needs an **interactive desktop session**; a session-0 SYSTEM service has
no desktop, so real bots cannot click anything. The task therefore:

* triggers **at log on** of a dedicated automation user (auto-logon is
  the recommended setup), running inside that user's desktop;
* restarts on failure (Task Scheduler restart policy) and, belt and
  braces, the agent itself runs in a crash-restart loop with backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

from smithy_agent.config import CONFIG_PATH, load_config, save_config

logger = logging.getLogger("smithy_agent")

TASK_NAME = "SmithyAgent"
_RESTART_BACKOFF_START_S = 5.0
_RESTART_BACKOFF_MAX_S = 60.0


def _log_path() -> Path:
    log_dir = CONFIG_PATH.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "agent.log"


def _configure_logging(log_path: Path, level: int) -> None:
    """File logging always; console only when one exists.

    Under ``pythonw.exe`` (the windowless scheduled-task launcher)
    ``sys.stderr``/``sys.stdout`` are ``None`` - a StreamHandler on them
    would lose every record, so it is skipped in that case.
    """
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def run_forever() -> None:
    """Start the agent and restart it after any exit (crash-restart loop)."""
    from smithy_agent.main import run_agent

    cfg = load_config()
    if not cfg.get("orchestrator_url"):
        print(
            f"No agent config at {CONFIG_PATH} — run `smithy-agent-service install` first",
            file=sys.stderr,
        )
        sys.exit(2)

    _configure_logging(
        _log_path(),
        getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
    )

    backoff = _RESTART_BACKOFF_START_S
    while True:
        logger.info("Agent starting (restart backoff %.0fs)", backoff)
        start = time.monotonic()

        def persist_credentials(agent_id: str, secret: str) -> None:
            save_config(
                str(cfg["orchestrator_url"]),
                str(cfg["agent_name"]),
                str(cfg["agent_url"]),
                cfg.get("join_token"),
                str(cfg.get("log_level", "INFO")),
                agent_id=agent_id,
                agent_secret=secret,
            )

        try:
            asyncio.run(
                run_agent(
                    str(cfg["orchestrator_url"]),
                    str(cfg["agent_name"]),
                    str(cfg["agent_url"]),
                    join_token=cfg.get("join_token"),
                    agent_id=cfg.get("agent_id"),
                    agent_secret=cfg.get("agent_secret"),
                    on_credentials=persist_credentials,
                )
            )
        except Exception:
            logger.exception("Agent crashed")
        runtime = time.monotonic() - start
        # A clean long-lived run resets the backoff; quick crash-loops grow it.
        backoff = (
            _RESTART_BACKOFF_START_S if runtime > 300 else min(backoff * 2, _RESTART_BACKOFF_MAX_S)
        )
        logger.info("Agent exited after %.0fs — restarting in %.0fs", runtime, backoff)
        time.sleep(backoff)
        if not CONFIG_PATH.exists():
            logger.info("Config removed — service loop stopping")
            break


# ---------------------------------------------------------------------
# Scheduled task management (Windows Task Scheduler, interactive session)
# ---------------------------------------------------------------------


def task_command(
    python_exe: str,
    user: str | None = None,
) -> str:
    """The Register-ScheduledTask PowerShell one-liner for the agent task.

    Uses ``pythonw.exe`` (no console window) when available - the agent logs
    to a file, the console is pure noise in an interactive desktop session.
    Note: the interpreter path must not contain spaces (Task Scheduler
    rejects quoted -Execute paths on some builds with "file not found").
    """
    exe = Path(python_exe)
    windowless = exe.with_name("pythonw.exe")
    if windowless.is_file():
        python_exe = str(windowless)
    parts = [
        "$action = New-ScheduledTaskAction -Execute "
        f"'{python_exe}' -Argument '-m smithy_agent.service run' "
        f"-WorkingDirectory '{Path.home()}'",
        f"$trigger = New-ScheduledTaskTrigger -AtLogOn -User '{user}'"
        if user
        else "$trigger = New-ScheduledTaskTrigger -AtLogOn",
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable",
    ]
    if user:
        parts.append(
            f"$principal = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive"
        )
        parts.append(
            "Register-ScheduledTask -TaskName "
            f"'{TASK_NAME}' -Action $action -Trigger $trigger "
            "-Settings $settings -Principal $principal -Force"
        )
    else:
        parts.append(
            "Register-ScheduledTask -TaskName "
            f"'{TASK_NAME}' -Action $action -Trigger $trigger "
            "-Settings $settings -Force"
        )
    return " ; ".join(parts)


def _run_powershell(script: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
    )


def install_task(user: str | None = None) -> bool:
    """Create (or recreate) the scheduled task; True on success."""
    res = _run_powershell(task_command(sys.executable, user))
    if res.returncode != 0:
        stderr = res.stderr.decode(errors="replace")
        denied = (
            "0x80070005" in stderr
            or "access is denied" in stderr.lower()
            or "отказано" in stderr.lower()
        )
        if denied:
            print(
                "Task registration denied — creating a scheduled task requires "
                "administrator rights.\nRun PowerShell as Administrator and repeat "
                "the install command.",
                file=sys.stderr,
            )
        else:
            print(stderr, file=sys.stderr)
        return False
    return True


def uninstall_task() -> bool:
    res = _run_powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    return res.returncode == 0


def update_package() -> bool:
    """Upgrade smithy-agent in the current venv and restart the task.

    The running agent keeps the old code until it restarts, so the task is
    bounced right after a successful upgrade (the crash-restart loop and
    saved credentials make this seamless).
    """
    import subprocess as sp

    print(f"Upgrading smithy-agent (interpreter: {sys.executable})...")
    proc = sp.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "smithy-agent[screenshot]",
        ]
    )
    if proc.returncode != 0:
        print("Upgrade failed (see pip output above)", file=sys.stderr)
        return False
    from smithy_agent.client import agent_version

    print(f"Now installed: smithy-agent {agent_version()}")
    print("Restarting the scheduled task to pick up the new version...")
    stop_task()
    if not start_task():
        print(
            "Task restart failed — start it manually: smithy-agent-service start", file=sys.stderr
        )
        return False
    print("Done.")
    return True


def start_task() -> bool:
    return _run_powershell(f"Start-ScheduledTask -TaskName '{TASK_NAME}'").returncode == 0


def stop_task() -> bool:
    return _run_powershell(f"Stop-ScheduledTask -TaskName '{TASK_NAME}'").returncode == 0


def task_status() -> str:
    """Human-readable one-screen status: task state + agent config + process."""
    lines: list[str] = []
    try:
        res = _run_powershell(
            f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction Stop).State"
        )
        state = res.stdout.decode(errors="replace").strip() or "not installed"
    except Exception:
        state = "not installed"
    lines.append(f"task:    {state} ({TASK_NAME})")

    cfg = load_config()
    if cfg.get("orchestrator_url"):
        lines.append(f"agent:   {cfg.get('agent_name', '?')}")
        lines.append(f"cloud:   {cfg['orchestrator_url']}")
        if cfg.get("agent_url"):
            lines.append(f"url:     {cfg['agent_url']}")
    else:
        lines.append("agent:   (no config - run `smithy-agent-service install`)")

    # Running interpreter (venv pythonw spawns the real pythonw as a child:
    # both share the command line, so one match is enough).
    try:
        res = _run_powershell(
            "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' AND "
            "CommandLine LIKE '%smithy_agent.service%'\" "
            "| Select-Object -First 1 -ExpandProperty ProcessId)"
        )
        pid = res.stdout.decode(errors="replace").strip()
        if pid:
            lines.append(f"pid:     {pid} (pythonw, background - no console window)")
    except Exception:
        pass

    lines.append(f"log:     {CONFIG_PATH.parent / 'agent.log'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="smithy-agent-service",
        description="Unattended agent runner: crash-restart loop + task management.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the crash-restart loop (used by the task)")
    install = sub.add_parser("install", help="write config + create the scheduled task")
    install.add_argument("--orchestrator", required=True)
    install.add_argument("--name", required=True)
    install.add_argument("--url", help="public agent URL (default: auto-detect)")
    install.add_argument("--join-token", default=None)
    install.add_argument(
        "--user", help="Windows account for the log-on trigger (default: current user)"
    )
    sub.add_parser("uninstall", help="remove the scheduled task")
    sub.add_parser("start", help="start the task now")
    sub.add_parser("stop", help="stop the task")
    sub.add_parser("status", help="show the task state")
    sub.add_parser("update", help="upgrade smithy-agent from PyPI and restart the task")

    args = parser.parse_args(argv)

    if args.command == "run":
        run_forever()
        return

    if args.command == "install":
        url = args.url or _auto_url(args.orchestrator)
        path = _write_config(args.orchestrator, args.name, url, args.join_token)
        if install_task(args.user):
            print(f"Config written: {path}")
            print(f"Scheduled task '{TASK_NAME}' installed")
            print("Start it now with: smithy-agent-service start")
            print("Note: the task runs at log on — use a dedicated auto-logon user")
            print("      so UI automation always has an interactive desktop.")
        else:
            print("Task registration failed (see stderr above)", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "uninstall":
        print("Task removed" if uninstall_task() else "Task not found or removal failed")
        return
    if args.command == "start":
        print("Task started" if start_task() else "Failed to start task")
        return
    if args.command == "stop":
        print("Task stopped" if stop_task() else "Failed to stop task")
        return
    if args.command == "status":
        print(task_status())
        return
    if args.command == "update":
        sys.exit(0 if update_package() else 1)


def _auto_url(orchestrator_url: str) -> str:
    from smithy_agent.config import agent_url_for

    return agent_url_for(orchestrator_url)


def _write_config(orchestrator_url: str, name: str, url: str, join_token: str | None) -> Path:
    from smithy_agent.config import save_config

    return save_config(orchestrator_url, name, url, join_token)


if __name__ == "__main__":
    main()
