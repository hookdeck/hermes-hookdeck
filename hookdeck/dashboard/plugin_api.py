"""Backend routes for the Hookdeck dashboard tab.

Mounted by the web server at ``/api/plugins/hookdeck/``. Everything here is a
thin read or a single Hookdeck call — the reasoning lives in the adapter and the
CLI, and this deliberately reuses both rather than restating either.

Two shapes of state are on show, and they answer different questions:

* Hookdeck's view — queue depth, failed events, open issues — is what is still
  owed to this gateway.
* the local ledger — what this gateway actually did with each delivery — is the
  only place a *run* failure is visible, since a run that fails after the 202
  looks perfectly delivered from Hookdeck's side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

# The plugin package sits two levels up (``hookdeck/dashboard/plugin_api.py``)
# and the web server imports this file by path, not as part of the package, so
# it is not importable by name without help.
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hookdeck.api import HookdeckAPI, HookdeckAPIError  # noqa: E402
from hookdeck.state import DeliveryLedger  # noqa: E402

router = APIRouter()


def _ledger_path() -> Path:
    home = os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "hookdeck" / "state.db"


def _models(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    models = result.get("models") or result.get("data") or []
    return models if isinstance(models, list) else []


async def _call(fn, *args, **kwargs):
    """Turn an API error into a 502 the tab can render instead of a blank page."""
    try:
        return await fn(*args, **kwargs)
    except HookdeckAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/overview")
async def overview() -> dict:
    """Everything the tab renders on load, in one round trip."""
    hookdeck: dict[str, Any] = {"configured": bool(os.getenv("HOOKDECK_API_KEY"))}

    if hookdeck["configured"]:
        async with HookdeckAPI() as api:
            hookdeck["queue_depth"] = await _call(api.queue_depth)
            failed = await _call(api.list_events, status="FAILED", limit=25)
            hookdeck["failed"] = [
                {
                    "id": e.get("id"),
                    "error_code": e.get("error_code"),
                    "response_status": e.get("response_status"),
                    "attempts": e.get("attempts"),
                    "created_at": e.get("created_at"),
                }
                for e in _models(failed)
            ]
            issues = await _call(api.list_issues, status="OPENED", limit=25)
            hookdeck["issues"] = [
                {
                    "id": i.get("id"),
                    "type": i.get("type"),
                    "error_code": i.get("error_code"),
                }
                for i in _models(issues)
            ]
            connections = await _call(api.list_connections, limit=100)
            hookdeck["connections"] = [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "paused": bool(c.get("paused_at")),
                }
                for c in _models(connections)
            ]

    local: dict[str, Any] = {"path": str(_ledger_path()), "exists": _ledger_path().exists()}
    if local["exists"]:
        ledger = DeliveryLedger(_ledger_path())
        try:
            local["counts"] = ledger.counts()
            local["failures"] = [
                {
                    "event_id": row["event_id"],
                    "route": row["route"],
                    "status": row["status"],
                    "agent_attempts": row["agent_attempts"],
                    "error": row["error"],
                }
                for row in ledger.recent_failures(25)
            ]
            # Runs whose outcome was never recorded. Normally empty; a non-empty
            # list means a crash lost the completion signal, and boot recovery
            # has not run since.
            local["stranded"] = [row["event_id"] for row in ledger.stale_running(3600)]
        finally:
            ledger.close()

    return {"hookdeck": hookdeck, "local": local}


@router.post("/events/{event_id}/retry")
async def retry_event(event_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _call(api.retry_event, event_id)
    return {"status": "queued", "event_id": event_id}


@router.post("/connections/{connection_id}/pause")
async def pause(connection_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _call(api.pause_connection, connection_id)
    return {"status": "paused", "connection_id": connection_id}


@router.post("/connections/{connection_id}/resume")
async def resume(connection_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _call(api.unpause_connection, connection_id)
    return {"status": "resumed", "connection_id": connection_id}


@router.get("/events/{event_id}/body")
async def event_body(event_id: str) -> dict:
    async with HookdeckAPI() as api:
        body: Optional[Any] = await _call(api.get_event_raw_body, event_id)
    return {"event_id": event_id, "body": body}
