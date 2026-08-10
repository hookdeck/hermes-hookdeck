"""Thin async client for the Hookdeck Admin REST API.

Only the endpoints this plugin actually uses are wrapped. Everything goes
through :meth:`HookdeckAPI.request` so authentication, error shaping and the
API-version base URL live in exactly one place.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import httpx

from .constants import API_BASE_URL


def _clean_params(params: Any) -> Any:
    """Drop empty values, preserving a list of pairs so keys can repeat.

    Some Hookdeck query params are nested and bracket-encoded, and `measures[]`
    legitimately appears more than once — which a dict cannot express.
    """
    if params is None:
        return None
    if isinstance(params, Mapping):
        return {k: v for k, v in params.items() if v not in (None, "")}
    return [(k, v) for k, v in params if v not in (None, "")]


class HookdeckAPIError(RuntimeError):
    """Non-2xx response from the Hookdeck API."""

    def __init__(self, status: int, method: str, path: str, body: str):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:400]}")


class HookdeckAPI:
    """Async Hookdeck API client.

    Usable as an async context manager, or long-lived with an explicit
    :meth:`aclose`. The adapter keeps one instance for the lifetime of the
    gateway; the CLI creates one per command via :func:`run_sync`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = API_BASE_URL,
        timeout: float = 20.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_key = api_key or os.getenv("HOOKDECK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "HookdeckAPI":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Any:
        if not self.api_key:
            raise HookdeckAPIError(
                401, method, path, "HOOKDECK_API_KEY is not set"
            )
        client = self._ensure_client()
        response = await client.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            params=_clean_params(params),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise HookdeckAPIError(
                response.status_code, method, path, response.text
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    async def upsert_connection(self, payload: Mapping[str, Any]) -> Any:
        """PUT /connections — create or update by name, with inline source and
        destination. Idempotent, which is what makes ``setup`` re-runnable."""
        return await self.request("PUT", "/connections", json=dict(payload))

    async def list_connections(self, **params: Any) -> Any:
        return await self.request("GET", "/connections", params=params)

    async def pause_connection(self, connection_id: str) -> Any:
        return await self.request("PUT", f"/connections/{connection_id}/pause")

    async def unpause_connection(self, connection_id: str) -> Any:
        return await self.request("PUT", f"/connections/{connection_id}/unpause")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def list_events(self, **params: Any) -> Any:
        return await self.request("GET", "/events", params=params)

    async def get_event(self, event_id: str) -> Any:
        return await self.request("GET", f"/events/{event_id}")

    async def get_event_raw_body(self, event_id: str) -> Any:
        return await self.request("GET", f"/events/{event_id}/raw_body")

    async def retry_event(self, event_id: str) -> Any:
        """POST /events/{id}/retry — hand a failed agent run back to Hookdeck."""
        return await self.request("POST", f"/events/{event_id}/retry")

    async def bulk_retry_events(self, query: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/bulk/events/retry", json=dict(query))

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def queue_depth(
        self, *, hours: int = 24, measures: Optional[list[str]] = None
    ) -> Any:
        """GET /metrics/queue-depth over the last *hours*.

        The endpoint looks parameterless and is not: without ``date_range`` and
        ``measures`` it answers 422. Both are nested, and Hookdeck wants them
        bracket-encoded (``date_range[start]``, repeated ``measures[]``) — a
        JSON-encoded object is rejected with "must be of type object", which
        reads like the opposite of what it means.

        Valid measures are ``max_depth`` and ``max_age`` only.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        query = [
            ("date_range[start]", start.isoformat().replace("+00:00", "Z")),
            ("date_range[end]", now.isoformat().replace("+00:00", "Z")),
        ]
        query += [("measures[]", m) for m in (measures or ["max_depth", "max_age"])]
        return await self.request("GET", "/metrics/queue-depth", params=query)

    async def list_issues(self, **params: Any) -> Any:
        return await self.request("GET", "/issues", params=params)


def run_sync(coro: Any) -> Any:
    """Run *coro* from synchronous CLI code."""
    return asyncio.run(coro)
