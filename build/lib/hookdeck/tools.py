"""Agent-facing tools for inspecting and repairing the event queue.

The point of these is that the agent's inbox becomes something the agent can
reason about. "Three GitHub events failed while I was restarting — what were
they, and are they worth retrying?" is a question Hermes can answer for itself
once it can read the queue.

Handlers follow the Hermes convention: synchronous, take a single ``args``
dict, return a string for the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Optional

from .api import HookdeckAPI, HookdeckAPIError

TOOLSET = "hookdeck"


def _run(coro: Any) -> Any:
    """Run *coro* from a synchronous tool handler.

    Tool handlers may be dispatched from a thread that already has a running
    event loop. ``asyncio.run`` would raise there, so fall back to a private
    loop on a worker thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _pause_minutes(args: dict) -> int:
    """Clamp the requested pause to something a forgotten agent cannot hurt."""
    try:
        requested = int(args.get("minutes") or DEFAULT_PAUSE_MINUTES)
    except (TypeError, ValueError):
        requested = DEFAULT_PAUSE_MINUTES
    return max(1, min(requested, MAX_PAUSE_MINUTES))


def _schedule_resume(connection_id: str, name: str, minutes: int) -> None:
    """Record when this pause must end.

    Written to the ledger rather than held in a timer: the gateway may restart
    while paused — that being one of the main reasons to pause — and a deadline
    that dies with the process is not a guarantee. The adapter honours it on
    its sweep and at boot.
    """
    _with_ledger(
        lambda ledger: ledger.schedule_resume(
            connection_id, name, time.time() + minutes * 60
        )
    )


def _cancel_scheduled_resume(connection_id: str) -> None:
    """Drop the deadline after a manual resume.

    Otherwise a timer from an earlier short pause would unpause a later, longer
    one before its time.
    """
    _with_ledger(lambda ledger: ledger.cancel_scheduled_resume(connection_id))


def _with_ledger(action) -> None:
    from .settings import configured_state_path
    from .state import DeliveryLedger

    # The path the adapter actually reads, honouring a configured state_path.
    # Writing a pause deadline anywhere else records it where nothing will
    # honour it, and the pause simply never ends.
    path = configured_state_path()
    try:
        ledger = DeliveryLedger(path)
    except Exception as exc:  # noqa: BLE001 - observability, never fatal
        logging.getLogger(__name__).error(
            "[hookdeck] Could not open the ledger at %s: %s", path, exc
        )
        return
    try:
        action(ledger)
    finally:
        ledger.close()


def _models(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    models = result.get("models") or result.get("data") or []
    return models if isinstance(models, list) else []


def _guard(fn: Any) -> Any:
    """Turn API errors into a message the model can act on."""

    def wrapper(args: Optional[dict] = None, **_: Any) -> str:
        try:
            return fn(args or {})
        except HookdeckAPIError as exc:
            return f"Hookdeck API error: {exc}"
        except Exception as exc:
            return f"Hookdeck tool failed: {exc}"

    wrapper.__name__ = getattr(fn, "__name__", "hookdeck_tool")
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------


@_guard
def hookdeck_queue_status(_args: dict) -> str:
    """Pending and failed counts for the project's event queue."""

    async def _go() -> str:
        async with HookdeckAPI() as api:
            depth = await api.queue_depth()
            failed = await api.list_events(status="FAILED", limit=1)
            issues = await api.list_issues(status="OPENED", limit=1)
        return json.dumps(
            {
                "queue_depth": depth,
                "failed_events_page_count": len(_models(failed)),
                "open_issues_page_count": len(_models(issues)),
            }
        )

    return _run(_go())


@_guard
def hookdeck_list_failed_events(args: dict) -> str:
    """Recent failed events, newest first."""
    limit = min(int(args.get("limit", 20) or 20), 100)
    params: dict[str, Any] = {"status": "FAILED", "limit": limit}
    if args.get("connection_id"):
        params["webhook_id"] = args["connection_id"]
    if args.get("since"):
        params["created_at[gte]"] = args["since"]

    async def _go() -> str:
        async with HookdeckAPI() as api:
            result = await api.list_events(**params)
        rows = [
            {
                "id": e.get("id"),
                "error_code": e.get("error_code"),
                "response_status": e.get("response_status"),
                "attempts": e.get("attempts"),
                "created_at": e.get("created_at"),
            }
            for e in _models(result)
        ]
        if not rows:
            return "No failed events."
        return json.dumps(rows, indent=2)

    return _run(_go())


@_guard
def hookdeck_get_event_body(args: dict) -> str:
    """The raw payload of one event, for inspecting what actually arrived."""
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        return "event_id is required."

    async def _go() -> str:
        async with HookdeckAPI() as api:
            body = await api.get_event_raw_body(event_id)
        text = json.dumps(body) if not isinstance(body, str) else body
        return text[:8000]

    return _run(_go())


@_guard
def hookdeck_retry_event(args: dict) -> str:
    """Ask Hookdeck to redeliver one event."""
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        return "event_id is required."

    async def _go() -> str:
        async with HookdeckAPI() as api:
            await api.retry_event(event_id)
        return f"Requested redelivery of {event_id}."

    return _run(_go())


@_guard
def hookdeck_bulk_retry(args: dict) -> str:
    """Retry every failed event, optionally scoped by time or connection."""
    query: dict[str, Any] = {"status": "FAILED"}
    if args.get("since"):
        query["created_at"] = {"gte": args["since"]}
    if args.get("connection_id"):
        query["webhook_id"] = args["connection_id"]

    async def _go() -> str:
        async with HookdeckAPI() as api:
            result = await api.bulk_retry_events(query)
        return f"Bulk retry queued: {json.dumps(result)[:1000]}"

    return _run(_go())


#: Longest an agent may pause a connection for. Pausing is safe — events are
#: held rather than dropped — but only until someone resumes, and an agent that
#: pauses and then fails leaves the queue growing with nobody watching.
MAX_PAUSE_MINUTES = 60
DEFAULT_PAUSE_MINUTES = 15


def _connection_action(args: dict, pause: bool) -> str:
    name = str(args.get("connection") or "").strip()
    if not name:
        return "connection is required (name or id)."

    async def _go() -> str:
        async with HookdeckAPI() as api:
            connection_id = name
            if not name.startswith(("web_", "con_")):
                result = await api.list_connections(name=name)
                models = _models(result)
                if not models:
                    return f"No connection named '{name}'."
                connection_id = models[0].get("id")
            if not pause:
                await api.unpause_connection(connection_id)
                _cancel_scheduled_resume(connection_id)
                return f"Resumed {name}. Held events will be delivered."

            await api.pause_connection(connection_id)
            minutes = _pause_minutes(args)
            _schedule_resume(connection_id, name, minutes)
            return (
                f"Paused {name}. Events are queued in Hookdeck, and it will "
                f"resume automatically in {minutes} minutes — resume it sooner "
                "with hookdeck_resume_connection once the cause is fixed."
            )

    return _run(_go())


@_guard
def hookdeck_pause_connection(args: dict) -> str:
    """Hold events in Hookdeck instead of delivering them."""
    return _connection_action(args, pause=True)


@_guard
def hookdeck_resume_connection(args: dict) -> str:
    """Resume a paused connection and drain what accumulated."""
    return _connection_action(args, pause=False)


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_EVENT_ID_PROP = {
    "event_id": {"type": "string", "description": "Hookdeck event id (evt_…)."}
}
_SINCE_PROP = {
    "since": {
        "type": "string",
        "description": "ISO 8601 lower bound, e.g. 2026-08-10T00:00:00Z.",
    },
    "connection_id": {
        "type": "string",
        "description": "Restrict to one Hookdeck connection id.",
    },
}

SCHEMAS = {
    "hookdeck_queue_status": _schema(
        "hookdeck_queue_status",
        "Summarise the Hookdeck event queue: pending depth, failed events and "
        "open issues. Use this to find out whether events were missed while the "
        "gateway was down.",
        {},
        [],
    ),
    "hookdeck_list_failed_events": _schema(
        "hookdeck_list_failed_events",
        "List recent failed Hookdeck events with their error codes and attempt "
        "counts, newest first.",
        {
            "limit": {"type": "integer", "description": "Max events (default 20, cap 100)."},
            **_SINCE_PROP,
        },
        [],
    ),
    "hookdeck_get_event_body": _schema(
        "hookdeck_get_event_body",
        "Fetch the raw payload of a Hookdeck event so you can see exactly what "
        "the provider sent.",
        _EVENT_ID_PROP,
        ["event_id"],
    ),
    "hookdeck_retry_event": _schema(
        "hookdeck_retry_event",
        "Ask Hookdeck to redeliver a single event. Use after fixing whatever "
        "made the original run fail.",
        _EVENT_ID_PROP,
        ["event_id"],
    ),
    "hookdeck_bulk_retry": _schema(
        "hookdeck_bulk_retry",
        "Retry every failed event, optionally scoped by time or connection. "
        "Prefer retrying individually unless the failures share one cause.",
        dict(_SINCE_PROP),
        [],
    ),
    "hookdeck_pause_connection": _schema(
        "hookdeck_pause_connection",
        "Pause a Hookdeck connection so events queue up instead of being "
        "delivered. Use before a restart or when you are about to be unable to "
        "process events correctly.",
        {
            "connection": {"type": "string", "description": "Connection name or id."},
            "minutes": {
                "type": "integer",
                "description": (
                    f"Auto-resume after this many minutes (default "
                    f"{DEFAULT_PAUSE_MINUTES}, max {MAX_PAUSE_MINUTES}). The "
                    "pause always expires — resume sooner once fixed."
                ),
            },
        },
        ["connection"],
    ),
    "hookdeck_resume_connection": _schema(
        "hookdeck_resume_connection",
        "Resume a paused Hookdeck connection and drain the events that "
        "accumulated while it was paused.",
        {"connection": {"type": "string", "description": "Connection name or id."}},
        ["connection"],
    ),
}

HANDLERS = {
    "hookdeck_queue_status": hookdeck_queue_status,
    "hookdeck_list_failed_events": hookdeck_list_failed_events,
    "hookdeck_get_event_body": hookdeck_get_event_body,
    "hookdeck_retry_event": hookdeck_retry_event,
    "hookdeck_bulk_retry": hookdeck_bulk_retry,
    "hookdeck_pause_connection": hookdeck_pause_connection,
    "hookdeck_resume_connection": hookdeck_resume_connection,
}

EMOJI = {
    "hookdeck_queue_status": "📊",
    "hookdeck_list_failed_events": "🚨",
    "hookdeck_get_event_body": "📄",
    "hookdeck_retry_event": "🔁",
    "hookdeck_bulk_retry": "🔁",
    "hookdeck_pause_connection": "⏸️",
    "hookdeck_resume_connection": "▶️",
}


def register_tools(ctx: Any) -> None:
    for name, schema in SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=HANDLERS[name],
            description=schema["function"]["description"],
            emoji=EMOJI.get(name, "🪝"),
        )
