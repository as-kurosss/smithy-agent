"""Agent configuration file: everything the service needs to (re)start.

Stored at ``%LOCALAPPDATA%\\smithcore_agent\\config.json`` (per-user: the agent
runs inside the user's interactive session, which UI automation requires).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "smithcore_agent"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_AGENT_PORT = 8001


def load_config() -> dict[str, Any]:
    """Read the config file; empty dict when absent."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(
    orchestrator_url: str,
    agent_name: str,
    agent_url: str,
    join_token: str | None = None,
    log_level: str = "INFO",
    agent_id: str | None = None,
    agent_secret: str | None = None,
) -> Path:
    """Write (or merge into) the config file.

    Merging matters: the service loop updates agent_id/agent_secret while
    running and must not lose the rest of the config.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    try:
        existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload.update(existing)
    except (OSError, json.JSONDecodeError):
        pass
    payload.update(
        {
            "orchestrator_url": orchestrator_url,
            "agent_name": agent_name,
            "agent_url": agent_url,
            "log_level": log_level,
        }
    )
    if join_token:
        payload["join_token"] = join_token
    if agent_id:
        payload["agent_id"] = agent_id
    if agent_secret:
        payload["agent_secret"] = agent_secret
    # Atomic write + owner-only permissions: config holds bearer secrets.
    import os
    import tempfile

    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        Path(tmp_name).replace(CONFIG_PATH)
        with contextlib.suppress(OSError):
            os.chmod(CONFIG_PATH, 0o600)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
    return CONFIG_PATH


def delete_config() -> bool:
    try:
        CONFIG_PATH.unlink()
        return True
    except OSError:
        return False


def detect_local_ip(orchestrator_url: str) -> str:
    """Local IP the orchestrator would see this machine at (no packets sent)."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(orchestrator_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def agent_url_for(orchestrator_url: str, port: int = DEFAULT_AGENT_PORT) -> str:
    """Default public URL: http://<this-machine-ip>:<port>."""
    return f"http://{detect_local_ip(orchestrator_url)}:{port}"


def check_orchestrator_url(url: str) -> None:
    """Refuse cleartext http orchestrator URLs for non-loopback hosts.

    The join token and agent secret travel as Bearer credentials — plain
    http would expose them. Loopback stays allowed for local development.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("orchestrator URL must be http(s)")
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                "orchestrator URL must use https:// for non-loopback hosts "
                "(tokens would travel in cleartext)"
            )
