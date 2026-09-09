"""HTTP client for communicating with the Smithy orchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 30
REAUTH_BACKOFF_S = 60.0


class OrchestratorError(Exception):
    """Raised when the orchestrator returns an unexpected response."""


class OrchestratorClient:
    """Async HTTP client wrapping all orchestrator API calls."""

    def __init__(
        self,
        orchestrator_url: str,
        agent_name: str,
        agent_url: str,
        *,
        join_token: str | None = None,
        agent_id: str | None = None,
        agent_secret: str | None = None,
        on_credentials: Callable[[str, str], None] | None = None,
    ) -> None:
        self.orchestrator_url = orchestrator_url.rstrip("/")
        self.agent_name = agent_name
        self.agent_url = agent_url
        self.join_token = join_token
        self.agent_id: str | None = agent_id
        self._secret: str | None = agent_secret
        # Called with (agent_id, secret) whenever the orchestrator issues a
        # new secret so the caller can persist it and survive restarts
        # without re-registering (re-registration rotates the secret and a
        # join token alone can no longer rotate an existing agent's secret).
        self._on_credentials = on_credentials
        self._reauth_lock = asyncio.Lock()
        self._last_reauth_attempt = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.orchestrator_url,
            timeout=httpx.Timeout(30.0),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def register(self, join_token: str | None = None) -> None:
        """Register (or re-register) this agent with the orchestrator.

        When the client already holds an agent id + secret they are sent
        along to prove ownership of the existing agent entry; the server
        answers with a fresh secret either way.
        """
        self.join_token = join_token or self.join_token
        headers = {"Authorization": f"Bearer {self.join_token}"} if self.join_token else None
        body: dict[str, Any] = {"name": self.agent_name, "url": self.agent_url}
        if self.agent_id and self._secret:
            body["agent_secret"] = self._secret
        resp = await self._http.post("/api/agents", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self.agent_id = data["id"]
        self._secret = data.get("secret")
        if self._on_credentials is not None and self.agent_id and self._secret:
            try:
                self._on_credentials(self.agent_id, self._secret)
            except Exception:  # noqa: BLE001 - persistence must never kill the agent
                logger.exception("Could not persist agent credentials")
        logger.info("Registered with orchestrator — agent id=%s", self.agent_id)

    async def _re_authenticate(self) -> None:
        """Recover from 401s: re-register once (single-flight, with backoff).

        A rotated secret (e.g. after an accidental double registration or a
        cloud-side reset) used to leave the agent 401-ing forever; the
        join token in the config lets it recover without a restart. Failed
        attempts back off so a rejected agent does not hammer the server
        on every poll cycle.
        """
        if time.monotonic() - self._last_reauth_attempt < REAUTH_BACKOFF_S:
            raise OrchestratorError("Re-registration attempted recently — backing off")
        async with self._reauth_lock:
            if time.monotonic() - self._last_reauth_attempt < REAUTH_BACKOFF_S:
                raise OrchestratorError("Re-registration attempted recently — backing off")
            if not self.join_token:
                raise OrchestratorError(
                    "Unauthorized and no join token available for re-registration"
                )
            self._last_reauth_attempt = time.monotonic()
            logger.warning("401 from orchestrator — re-registering")
            await self.register()

    async def close(self) -> None:
        """Shut down the HTTP client."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(self) -> None:
        """Send a single heartbeat to the orchestrator."""
        await self._post(f"/api/agents/{self._agent_id}/heartbeat")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll(self) -> list[dict[str, Any]]:
        """Poll the orchestrator for pending commands.

        On a 401 (rotated/stale secret) the agent re-registers once and
        retries, instead of warning forever and never working again.

        Returns a list of command dicts.  Each command has at least a ``type``
        key, e.g.::

            {"type": "run", "run_id": "...", "process": {…}}
        """
        resp = await self._get(f"/api/agents/{self._agent_id}/poll")
        if resp.status_code == 401:
            await self._re_authenticate()
            resp = await self._get(f"/api/agents/{self.agent_id}/poll")
        if resp.status_code == 204:
            return []
        resp.raise_for_status()
        return cast(list[dict[str, Any]], resp.json())

    # ------------------------------------------------------------------
    # Logs & status
    # ------------------------------------------------------------------

    async def push_logs(self, run_id: str, logs: list[dict[str, Any]]) -> None:
        """Push a batch of log entries to the orchestrator."""
        if not logs:
            return
        await self._post(
            f"/api/agents/{self._agent_id}/logs",
            json={"run_id": run_id, "logs": logs},
            retry=True,
        )

    async def report_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """Report a run status change to the orchestrator."""
        payload: dict[str, Any] = {"run_id": run_id, "status": status}
        if error is not None:
            payload["error"] = error
        await self._post(f"/api/agents/{self._agent_id}/status", json=payload, retry=True)

    async def ack_deployment(
        self,
        deployment_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """Confirm a deployment result (deployed/failed) to the orchestrator."""
        payload: dict[str, Any] = {"status": status}
        if error is not None:
            payload["error"] = error
        await self._post(
            f"/api/agents/{self._agent_id}/deployments/{deployment_id}/ack",
            json=payload,
        )

    async def push_artifact(
        self,
        run_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> None:
        """Upload a binary artifact for a run (e.g. a failure screenshot)."""
        import base64

        payload: dict[str, Any] = {
            "run_id": run_id,
            "filename": filename,
            "content_type": content_type,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        await self._post(
            f"/api/agents/{self._agent_id}/runs/{run_id}/artifacts",
            json=payload,
            retry=True,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _agent_id(self) -> str:
        if self.agent_id is None:
            raise OrchestratorError("Agent not registered — call register() first")
        return self.agent_id

    @property
    def _auth_headers(self) -> dict[str, str]:
        if self._secret is None:
            return {}
        return {"Authorization": f"Bearer {self._secret}"}

    async def _get(self, path: str) -> httpx.Response:
        logger.debug("GET %s", path)
        return await self._http.get(path, headers=self._auth_headers)

    async def _post(self, path: str, *, json: Any = None, retry: bool = False) -> httpx.Response:
        logger.debug("POST %s", path)
        # Status/log pushes are critical: transient 5xx/network blips are
        # retried with backoff so runs don't stick in RUNNING forever.
        attempts = 3 if retry else 1
        delay = 0.5
        resp: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                resp = await self._http.post(path, json=json, headers=self._auth_headers)
            except httpx.HTTPError:
                logger.warning("POST %s attempt %d failed (network)", path, attempt + 1)
                resp = None
            else:
                if resp.status_code < 500:
                    break
                logger.warning("POST %s attempt %d -> %s", path, attempt + 1, resp.status_code)
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
                delay *= 2.0
        if resp is None:
            raise OrchestratorError(f"POST {path} failed after {attempts} attempts (network)")
        if resp.status_code >= 400:
            # 4xx is terminal (auth/validation) — warn but don't raise so the
            # agent loop survives; 5xx after retries also only warns.
            logger.warning("POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
        return resp
