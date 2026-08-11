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

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

# The web server imports this file by path rather than as part of its package,
# so the sibling modules are not importable by name without help.
#
# The obvious help — inserting the repo root on sys.path — is wrong, and badly:
# this runs inside the host's web-server process, the repo root also holds
# `tests/` and `examples/`, and Hermes has its own top-level `tests` package.
# Prepending would shadow it process-wide. A plugin has no business rearranging
# the host's import path.
#
# So the package is loaded from its own directory under its own name, touching
# nothing else. If the process already imported it (the gateway does), that one
# is reused.
_PKG_DIR = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    if "hookdeck" in sys.modules:
        return sys.modules["hookdeck"]
    spec = importlib.util.spec_from_file_location(
        "hookdeck",
        _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the package's own relative imports resolve.
    sys.modules["hookdeck"] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_package()

from hookdeck.api import HookdeckAPI, HookdeckAPIError  # noqa: E402
from hookdeck.provision import routes_from_config  # noqa: E402
from hookdeck.settings import configured_state_path  # noqa: E402
from hookdeck.settings import load_hermes_config as _load_hermes_config  # noqa: E402
from hookdeck.ledger import RunLedger  # noqa: E402

router = APIRouter()


_ledger_path = configured_state_path


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


def _queue_depth(raw: Any) -> dict:
    """The one queue metric worth showing, lifted out of its bucketed series.

    ``max_age`` is deliberately not surfaced. Its unit is undocumented —
    neither the OpenAPI spec nor the metrics docs say whether it is seconds,
    minutes or hours — and it does not change over minutes of observation, so
    it is a window maximum rather than a live age and no delta reveals the unit
    either. A number whose meaning cannot be stated is worse on a dashboard
    than no number.
    """
    rows = (raw or {}).get("data") or []
    metrics = rows[0].get("metrics") if rows else {}
    return {"max_depth": (metrics or {}).get("max_depth")}


def _own_connections(raw: Any) -> tuple[list[dict], int]:
    """This gateway's connections, and a count of the ones left out.

    A project's other connections are someone else's production traffic, and a
    tab that puts a Pause button beside all of them is one misclick from an
    outage nobody would connect to this plugin. The count is reported so an
    empty list reads as "none of yours" rather than "the project is empty".
    """
    routes = set(routes_from_config(_load_hermes_config()))
    everything = _models(raw)
    mine = [
        {"id": c.get("id"), "name": c.get("name"), "paused": bool(c.get("paused_at"))}
        for c in everything
        if c.get("name") in routes
    ]
    return mine, len(everything) - len(mine)


async def _hookdeck_state() -> dict:
    """What Hookdeck still owes this gateway."""
    if not os.getenv("HOOKDECK_API_KEY"):
        return {"configured": False}

    async with HookdeckAPI() as api:
        # Four independent reads: gathered, so the tab costs one round trip's
        # latency rather than four.
        depth, failed, issues, connections = await asyncio.gather(
            _call(api.queue_depth),
            _call(api.list_events, status="FAILED", limit=25),
            _call(api.list_issues, status="OPENED", limit=25),
            _call(api.list_connections, limit=100),
        )

    mine, others = _own_connections(connections)
    return {
        "configured": True,
        "depth": _queue_depth(depth),
        "failed": [
            {
                "id": e.get("id"),
                "error_code": e.get("error_code"),
                "response_status": e.get("response_status"),
                "attempts": e.get("attempts"),
                "created_at": e.get("created_at"),
            }
            for e in _models(failed)
        ],
        "issues": [
            {"id": i.get("id"), "type": i.get("type"), "error_code": i.get("error_code")}
            for i in _models(issues)
        ],
        "connections": mine,
        "other_connection_count": others,
    }


def _local_state() -> dict:
    """What this gateway actually did with each delivery."""
    path = _ledger_path()
    state: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not state["exists"]:
        return state

    ledger = RunLedger(path)
    try:
        state["counts"] = ledger.counts()
        state["failures"] = [
            {
                "event_id": row["event_id"],
                "route": row["route"],
                "status": row["status"],
                "agent_attempts": row["agent_attempts"],
                "error": row["error"],
            }
            for row in ledger.recent_failures(25)
        ]
        # Runs whose outcome was never recorded. Normally empty; anything here
        # means a crash lost the completion signal and boot recovery has not
        # run since.
        state["stranded"] = [row["event_id"] for row in ledger.stale_running(3600)]
    finally:
        ledger.close()
    return state


@router.get("/overview")
async def overview() -> dict:
    """Everything the tab renders on load, in one round trip."""
    return {"hookdeck": await _hookdeck_state(), "local": _local_state()}


@router.post("/events/{event_id}/retry")
async def retry_event(event_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _call(api.retry_event, event_id)
    return {"status": "queued", "event_id": event_id}


async def _require_own_connection(api: HookdeckAPI, connection_id: str) -> None:
    """Refuse to act on a connection this gateway does not own.

    Filtering the *list* keeps other people's connections off the page, but the
    endpoint is reachable regardless — and pausing a stranger's production
    connection is exactly the outage the filtering exists to prevent. The check
    belongs on the action, not the display.
    """
    # By name, one request per configured route: filtering a capped page would
    # falsely refuse an owned connection that happened to fall past it.
    owned_ids = set()
    for route_name in routes_from_config(_load_hermes_config()):
        found = await _call(api.list_connections, name=route_name, limit=10)
        owned_ids.update(c.get("id") for c in _models(found))

    if connection_id not in owned_ids:
        raise HTTPException(
            status_code=403,
            detail=(
                "That connection does not belong to a configured route. This "
                "tab only controls connections named by "
                "platforms.hookdeck.extra.routes."
            ),
        )


@router.post("/connections/{connection_id}/pause")
async def pause(connection_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _require_own_connection(api, connection_id)
        await _call(api.pause_connection, connection_id)
    return {"status": "paused", "connection_id": connection_id}


@router.post("/connections/{connection_id}/resume")
async def resume(connection_id: str) -> dict:
    async with HookdeckAPI() as api:
        await _require_own_connection(api, connection_id)
        await _call(api.unpause_connection, connection_id)
    return {"status": "resumed", "connection_id": connection_id}
