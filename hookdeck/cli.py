"""``hermes hookdeck …`` — operator commands for the Hookdeck integration.

Registered via ``ctx.register_cli_command``: ``register_cli`` builds the
subparsers, ``hookdeck_command`` dispatches them and returns a process exit
code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import HookdeckAPI, HookdeckAPIError, run_sync
from .constants import (
    API_KEY_ENV,
    CLI_API_KEY_ENV,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    MODE_ENV,
    WEBHOOK_SECRET_ENV,
    api_key,
)
from .ledger import RunLedger
from .provision import (
    build_connection_payload,
    routes_from_config,
    summarise_payload,
    uncovered_statuses,
)
from .settings import (
    configured_state_path,
    default_cli_config_path,
    load_hermes_config,
    platform_extra,
)

# ----------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------


# Below this, the CLI silently stops delivering once a listen session expires,
# which looks exactly like "no events are arriving" from the gateway's side.
MIN_CLI_VERSION = (2, 3, 2)
MIN_CLI_VERSION_TEXT = ".".join(str(part) for part in MIN_CLI_VERSION)


def _cli_version(binary: str) -> str:
    try:
        out = subprocess.run(
            [binary, "version"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001 - an unknown version is reported, not raised
        return ""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", out)
    return match.group(0) if match else ""


def _version_at_least(version: str, minimum: tuple[int, ...]) -> bool:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-(.+))?", version or "")
    if not match:
        return False
    numbers = tuple(int(match.group(i)) for i in (1, 2, 3))
    if numbers != minimum:
        return numbers > minimum
    # Same numbers, but a pre-release of X.Y.Z precedes the release itself.
    return match.group(4) is None


def _other_hookdeck_binaries(chosen: str) -> list[str]:
    """Other `hookdeck` executables on PATH that *chosen* shadows."""
    found = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "hookdeck"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = str(candidate)
            if resolved != chosen and resolved not in found:
                found.append(resolved)
    return found


# Re-exported under their historical names; the definitions live in settings so
# the adapter, CLI, dashboard and agent tools cannot drift apart on where the
# config and the ledger are.
_load_hermes_config = load_hermes_config
_platform_extra = platform_extra
_ledger_path = configured_state_path


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="hookdeck_action")

    setup = subs.add_parser(
        "setup", help="Create or update the Hookdeck connection for a route"
    )
    setup.add_argument("route", nargs="?", default="", help="Route name from config.yaml")
    setup.add_argument("--all", action="store_true", help="Set up every configured route")
    setup.add_argument("--source", default="", help="Hookdeck source name")
    setup.add_argument(
        "--source-type",
        default="",
        help="Hookdeck source type (STRIPE, GITHUB, SHOPIFY, …). Default: WEBHOOK",
    )
    setup.add_argument("--mode", default="", choices=["", "cli", "push"])
    setup.add_argument("--url", default="", help="Destination URL (push mode)")
    setup.add_argument("--path", default="", help=f"Destination path (default {DEFAULT_PATH})")
    setup.add_argument(
        "--rate-limit", type=int, default=None, help="Max deliveries per period (push mode)"
    )
    setup.add_argument(
        "--rate-limit-period",
        default="concurrent",
        choices=["second", "minute", "hour", "concurrent"],
    )
    setup.add_argument(
        "--group-key",
        default="",
        help="Payload path to throttle per, e.g. body.repository.full_name",
    )
    setup.add_argument("--group-rate", type=int, default=1, help="Deliveries per group period")
    setup.add_argument(
        "--group-period",
        default="minute",
        # Hookdeck's delivery groups take no `concurrent` period, so per-group
        # serialisation is not available — only per-group rate.
        choices=["second", "minute", "hour"],
    )
    setup.add_argument("--dry-run", action="store_true", help="Print the payload, send nothing")

    status = subs.add_parser("status", help="Queue depth, recent failures, open issues")
    status.add_argument("--limit", type=int, default=10)

    pause = subs.add_parser("pause", help="Hold events in Hookdeck (safe restarts)")
    pause.add_argument("connection", help="Connection name or id")

    resume = subs.add_parser("resume", help="Resume a paused connection and drain it")
    resume.add_argument("connection", help="Connection name or id")

    # `retry`, not `replay`: these call POST /events/{id}/retry, which makes a
    # new delivery attempt for the same event. Hookdeck's *replay* re-ingests
    # the original request and creates new events — a different tool, for when
    # the connection's rules have changed.
    retry = subs.add_parser(
        "retry", help="Ask Hookdeck to re-attempt delivery of events"
    )
    retry.add_argument("event_id", nargs="?", default="", help="A single event id")
    retry.add_argument("--failed", action="store_true", help="Retry all failed events")
    retry.add_argument("--since", default="", help="ISO 8601 lower bound for --failed")
    retry.add_argument("--connection", default="", help="Restrict --failed to a connection id")

    subs.add_parser("doctor", help="Check credentials, CLI, config and local state")


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def _api() -> HookdeckAPI:
    return HookdeckAPI()


def _payload_for_route(args: argparse.Namespace, name: str, route: dict) -> dict:
    extra = _platform_extra()
    mode = args.mode or route.get("mode") or extra.get("mode") or "cli"
    path = args.path or extra.get("path") or DEFAULT_PATH
    url = args.url or route.get("url") or ""
    if mode == "push" and not url:
        host = extra.get("public_url") or ""
        if host:
            url = f"{host.rstrip('/')}{path}/{name}"
    return build_connection_payload(
        name=name,
        source_name=args.source or route.get("source") or name,
        source_type=args.source_type or route.get("source_type") or "WEBHOOK",
        mode=mode,
        path=f"{path}/{name}",
        url=url,
        events=list(route.get("events") or []),
        event_path=str(route.get("event_path") or ""),
        rate_limit=args.rate_limit if args.rate_limit is not None else route.get("rate_limit"),
        rate_limit_period=args.rate_limit_period,
        delivery_group_key=args.group_key or str(route.get("delivery_group_key") or ""),
        group_rate=getattr(args, "group_rate", 1),
        group_rate_period=getattr(args, "group_period", "minute"),
    )


def _warn_inert_concurrency(args: argparse.Namespace, config: dict) -> None:
    """A destination concurrency cap does nothing under an early ack.

    Hookdeck counts delivery attempts open to the destination, and in
    async_retry the delivery ends at the 202 — milliseconds in, while the run
    continues. The setting is accepted and simply never engages, which is worth
    saying at the moment someone types the flag.
    """
    if args.rate_limit is None or args.rate_limit_period != "concurrent":
        return
    ack_mode = platform_extra(config).get("ack_mode") or "async_retry"
    if ack_mode != "async_retry":
        return
    print(
        "! --rate-limit-period concurrent has no effect with "
        "ack_mode: async_retry. Hookdeck counts deliveries still open to the "
        "destination, and this adapter answers 202 in milliseconds, so the "
        "limit never engages. Cap runs with "
        "platforms.hookdeck.extra.max_concurrent, or switch to ack_mode: sync "
        "where the delivery stays open for the run.\n"
    )


def _cmd_setup(args: argparse.Namespace) -> int:
    # Read once and pass it down: `_platform_extra()` with no argument re-reads
    # the file, and two reads in one command can disagree.
    config = _load_hermes_config()
    routes = routes_from_config(config)
    _warn_inert_concurrency(args, config)
    if args.all:
        targets = routes
    elif args.route:
        if args.route not in routes:
            # Allow setting up a connection for a route that only exists on the
            # command line — useful before the config has been written.
            targets = {args.route: {}}
        else:
            targets = {args.route: routes[args.route]}
    else:
        print("Usage: hermes hookdeck setup <route> [--all]")
        if routes:
            print("Configured routes: " + ", ".join(routes))
        return 2

    if not targets:
        print("No routes configured under platforms.hookdeck.extra.routes")
        return 1

    try:
        payloads = {
            name: _payload_for_route(args, name, route)
            for name, route in targets.items()
        }
    except ValueError as exc:
        print(f"✗ {exc}")
        return 2

    if args.dry_run:
        for payload in payloads.values():
            print(json.dumps(payload, indent=2))
        return 0

    async def _apply() -> int:
        failures = 0
        async with _api() as api:
            for name, payload in payloads.items():
                try:
                    await api.upsert_connection(payload)
                    print(f"✓ {summarise_payload(payload)}")
                except HookdeckAPIError as exc:
                    failures += 1
                    print(f"✗ {name}: {exc}")
        return 1 if failures else 0

    result = run_sync(_apply())
    if result == 0:
        print(
            f"\nNext: set {WEBHOOK_SECRET_ENV} to your project's signing "
            "secret, then start the gateway. Named source types (STRIPE, "
            "GITHUB, …) still need the provider's own signing secret entered "
            "on the source in the Hookdeck dashboard."
        )
    return result


def _cmd_status(args: argparse.Namespace) -> int:
    async def _run() -> int:
        async with _api() as api:
            try:
                depth = await api.queue_depth()
                print(f"Queue depth: {json.dumps(depth)}")
            except HookdeckAPIError as exc:
                print(f"! Queue depth unavailable: {exc}")

            try:
                failed = await api.list_events(status="FAILED", limit=args.limit)
                models = (failed or {}).get("models") or (failed or {}).get("data") or []
                print(f"\nFailed events in Hookdeck ({len(models)} shown):")
                for event in models:
                    print(
                        f"  {event.get('id')}  {event.get('error_code') or ''}  "
                        f"status={event.get('response_status')}  "
                        f"attempts={event.get('attempts')}"
                    )
                if not models:
                    print("  (none)")
            except HookdeckAPIError as exc:
                print(f"! Event list unavailable: {exc}")

            try:
                issues = await api.list_issues(status="OPENED", limit=args.limit)
                models = (issues or {}).get("models") or (issues or {}).get("data") or []
                print(f"\nOpen issues ({len(models)}):")
                for issue in models:
                    print(f"  {issue.get('id')}  {issue.get('type')}  {issue.get('error_code') or ''}")
                if not models:
                    print("  (none)")
            except HookdeckAPIError as exc:
                print(f"! Issue list unavailable: {exc}")
        return 0

    result = run_sync(_run())
    _print_local_state(args.limit)
    return result


def _print_local_state(limit: int) -> None:
    path = _ledger_path()
    if not path.exists():
        print("\nLocal delivery ledger: not created yet")
        return
    ledger = RunLedger(path)
    try:
        print(f"\nLocal delivery ledger ({path}):")
        counts = ledger.counts()
        print("  " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(empty)"))
        failures = ledger.recent_failures(limit)
        if failures:
            print("  Recent agent-run failures:")
            for row in failures:
                print(
                    f"    {row['event_id']}  route={row['route']}  "
                    f"agent_attempts={row['agent_attempts']}  {row['error']}"
                )
    finally:
        ledger.close()


async def _resolve_connection_id(api: HookdeckAPI, value: str) -> str | None:
    if value.startswith("web_") or value.startswith("con_"):
        return value
    result = await api.list_connections(name=value)
    models = (result or {}).get("models") or (result or {}).get("data") or []
    if not models:
        return None
    return models[0].get("id")


def _cmd_pause(args: argparse.Namespace, pause: bool) -> int:
    async def _run() -> int:
        async with _api() as api:
            connection_id = await _resolve_connection_id(api, args.connection)
            if not connection_id:
                print(f"✗ No connection named '{args.connection}'")
                return 1
            try:
                if pause:
                    await api.pause_connection(connection_id)
                    print(
                        f"✓ Paused {args.connection}. Events are held in Hookdeck "
                        "until you resume — safe to restart the gateway now."
                    )
                else:
                    await api.unpause_connection(connection_id)
                    print(f"✓ Resumed {args.connection}. Held events will drain.")
            except HookdeckAPIError as exc:
                print(f"✗ {exc}")
                return 1
        return 0

    return run_sync(_run())


def _cmd_retry(args: argparse.Namespace) -> int:
    async def _run() -> int:
        async with _api() as api:
            if args.event_id:
                try:
                    await api.retry_event(args.event_id)
                    print(f"✓ Requested redelivery of {args.event_id}")
                    return 0
                except HookdeckAPIError as exc:
                    print(f"✗ {exc}")
                    return 1

            if not args.failed:
                print("Usage: hermes hookdeck retry <event_id> | --failed [--since ISO]")
                return 2

            query: dict[str, Any] = {"status": "FAILED"}
            if args.since:
                query["created_at"] = {"gte": args.since}
            if args.connection:
                query["webhook_id"] = args.connection
            try:
                result = await api.bulk_retry_events(query)
                print(f"✓ Bulk retry queued: {json.dumps(result)[:400]}")
                return 0
            except HookdeckAPIError as exc:
                print(f"✗ {exc}")
                return 1

    return run_sync(_run())


@dataclass
class Check:
    """One diagnostic result, rendered as a tick or a cross."""

    ok: bool
    message: str
    #: Printed underneath, for context that is useful but not itself a failure.
    note: str = ""

    def render(self) -> None:
        print(("✓ " if self.ok else "✗ ") + self.message)
        if self.note:
            print(f"  note: {self.note}")


def _check_credentials(extra: dict) -> list[Check]:
    key = api_key()
    secret = extra.get("secret") or os.getenv(WEBHOOK_SECRET_ENV)
    return [
        Check(
            bool(key),
            f"API key is set ({API_KEY_ENV})"
            if key
            else f"No API key — setup, status and retry will not work. Set "
            f"{API_KEY_ENV} (or {CLI_API_KEY_ENV}, which the Hookdeck CLI "
            "reads too).",
        ),
        Check(
            bool(secret),
            "Signing secret is configured"
            if secret
            else "No signing secret — the adapter will refuse to start. Set "
            f"{WEBHOOK_SECRET_ENV} to your project's signing secret.",
        ),
    ]


def _check_routes(routes: dict) -> Check:
    return Check(
        bool(routes),
        f"{len(routes)} route(s) configured: {', '.join(routes)}"
        if routes
        else "No routes under platforms.hookdeck.extra.routes",
    )


def _cli_config_project(path: Path) -> tuple[str, bool]:
    """The active profile's project id, and whether the file could be read.

    A Hookdeck CLI config is multi-section with a top-level ``profile`` key
    selecting the active one, so the first ``project_id`` in the file is not
    necessarily the one the CLI will use. Reading the wrong section reports a
    mismatch that is not real, which is worse than not checking at all.

    The bool distinguishes "no such file" from "file present, no project in
    it" — the caller says something different about each.
    """
    try:
        text = path.read_text()
    except OSError:
        return "", False

    named = re.search(r"^\s*profile\s*=\s*['\"]?([^'\"\s]+)", text, re.M)
    profile = named.group(1) if named else "default"
    section = re.search(
        rf"^\[{re.escape(profile)}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S
    )
    body = section.group(1) if section else text
    found = re.search(r"^\s*project_id\s*=\s*['\"]?([^'\"\s]+)", body, re.M)
    return (found.group(1) if found else ""), True


def _api_key_project() -> str:
    """Which project the API key belongs to, read off anything it can see."""
    async def _go() -> str:
        async with HookdeckAPI() as api:
            for fetch in (api.list_connections, api.list_sources):
                try:
                    result = await fetch(limit=1)
                except (HookdeckAPIError, AttributeError):
                    continue
                models = (result or {}).get("models") or []
                if models and models[0].get("team_id"):
                    return str(models[0]["team_id"])
        return ""

    try:
        return run_sync(_go())
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return ""


async def _check_source_verification(api: HookdeckAPI, routes: dict) -> list[Check]:
    """Whether a provider-typed source is actually verifying anything.

    Setting a source's type to STRIPE or GITHUB does *not* switch verification
    on. The provider's own signing secret has to be set on the source, and
    until it is the source accepts anything — measured: an unsigned request and
    one carrying `sha256=deadbeef` were both accepted by a GITHUB source with
    no secret, and both produced events.

    The source's own record does not say whether a secret is configured; a
    source with one set is byte-identical to one without over the API. So the
    only signal is observed traffic, where each request carries `verified`.
    That means this can confirm a problem but never confirm its absence, and it
    says which of the two it is doing rather than implying the stronger one.
    """
    typed = {
        name: route
        for name, route in routes.items()
        if str(route.get("source_type") or "WEBHOOK").upper() != "WEBHOOK"
    }
    if not typed:
        return []

    checks: list[Check] = []
    for route_name, route in typed.items():
        source_type = str(route["source_type"]).upper()
        source_name = route.get("source") or route_name
        found = _models(await api.list_sources(name=source_name))
        if not found:
            continue
        source = found[0]
        requests = _models(
            await api.list_requests(source_id=source.get("id"), limit=10)
        )
        if not requests:
            checks.append(
                Check(
                    True,
                    f"source '{source.get('name')}' is type {source_type} — no "
                    "traffic yet, so verification is unconfirmed",
                    note=(
                        f"A {source_type} source verifies nothing until the "
                        "provider's signing secret is set on it in the Hookdeck "
                        "dashboard, and the API does not report whether it is. "
                        "Until then the source accepts forged payloads."
                    ),
                )
            )
            continue

        unverified = [r for r in requests if not r.get("verified")]
        if unverified:
            checks.append(
                Check(
                    False,
                    f"source '{source.get('name')}' is type {source_type} but "
                    f"{len(unverified)} of its last {len(requests)} requests "
                    "were not verified — its signing secret is missing or does "
                    "not match the sender's.",
                    note=(
                        "Set the provider's signing secret on the source in the "
                        "Hookdeck dashboard. Without it the source accepts "
                        "anything, including forged payloads."
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    True,
                    f"source '{source.get('name')}' verified all of its last "
                    f"{len(requests)} requests as {source_type}",
                )
            )
    return checks


def _models(result: Any) -> list[dict]:
    """The list out of a paginated response, whichever key it used."""
    if not isinstance(result, dict):
        return []
    models = result.get("models") or result.get("data") or []
    return models if isinstance(models, list) else []


def _burst_headroom(connection: dict, retry_rule: dict, extra: dict) -> Check:
    """How large a burst this connection absorbs before events start dying.

    An event deferred with 503 gets back in only on a later retry attempt, so
    the queue drains at roughly `max_concurrent` events per round and each
    event has `count` rounds before Hookdeck gives up. Their product is the
    burst that survives; past it the tail exhausts its retries while waiting.

    Reported rather than judged: the number that matters is the burst this
    gateway actually sees, and only the operator knows that.
    """
    concurrent = int(extra.get("max_concurrent", DEFAULT_MAX_CONCURRENT) or 0)
    count = int(retry_rule.get("count") or 0)
    name = connection.get("name")

    if not concurrent:
        return Check(
            True,
            f"connection '{name}': max_concurrent is unlimited, so nothing is "
            "deferred for capacity",
        )
    if not count:
        return Check(
            True, f"connection '{name}': retry rule has no count to reason about"
        )
    return Check(
        True,
        f"connection '{name}' absorbs a burst of about {concurrent * count} "
        f"events (max_concurrent {concurrent} x {count} retries)",
        note=(
            "A larger simultaneous burst drains at max_concurrent per retry "
            "round, and the tail runs out of retries before it is admitted. "
            "Raise max_concurrent to spend more on parallel runs, or the "
            "rule's count to wait longer."
        ),
    )


def _check_cli_project(extra: dict) -> Check:
    """The two projects in play must be the same one.

    An API key decides which project `setup`, `status` and the retry hand-back
    act on. The Hookdeck CLI's own config decides which project `hookdeck
    listen` forwards from. Nothing reconciles them, and when they differ every
    visible signal says the gateway is fine: `setup` succeeds, the adapter logs
    that it is listening, and only the tunnel's restart loop — "no connection
    found matching filter" — says otherwise, while events accumulate as
    CLI_DISCONNECTED ignored events.
    """
    configured = extra.get("cli_config_path")
    if configured == "":
        path = Path.home() / ".config" / "hookdeck" / "config.toml"
        source = f"your own session ({path})"
    else:
        # Matches AdapterSettings: absent or None means the gateway's own.
        path = (
            Path(str(configured)).expanduser()
            if configured
            else default_cli_config_path()
        )
        source = f"the gateway's own session ({path})"

    cli_project, readable = _cli_config_project(path)
    if configured != "" and not readable:
        return Check(
            True,
            "The gateway will authenticate its own CLI session on start",
            note=f"{path} does not exist yet; it is created from the API key, "
            "so it cannot point at the wrong project.",
        )

    key_project = _api_key_project()
    if not key_project:
        return Check(
            True,
            "Could not determine the API key's project — nothing provisioned yet",
            note="Re-run doctor after `hermes hookdeck setup`.",
        )
    if not cli_project:
        return Check(
            False,
            f"No project recorded in {source}",
            note="The file exists but names no project for its active profile. "
            "Re-authenticate the CLI, or delete the file and let the gateway "
            "create it.",
        )
    if cli_project == key_project:
        return Check(True, f"CLI and API key agree on project {key_project}")

    fix = (
        "Remove platforms.hookdeck.extra.cli_config_path so the gateway pins "
        "its own CLI session from the API key."
        if configured == ""
        else "Delete the file and let the gateway re-create it from the API key."
    )
    return Check(
        False,
        f"Project mismatch: the API key manages {key_project} but the CLI "
        f"forwards from {cli_project}",
        note="setup provisions one project while `hookdeck listen` forwards "
        f"from the other. The gateway will look healthy and every event will "
        f"become a CLI_DISCONNECTED ignored event. {fix}",
    )


def _check_cli(extra: dict) -> list[Check]:
    """The CLI is only reachable in cli mode, and only the resolved one matters."""
    configured = extra.get("cli_binary") or "hookdeck"
    resolved = shutil.which(configured)
    if not resolved:
        return [
            Check(
                False,
                f"Hookdeck CLI '{configured}' not found — install it, set "
                "platforms.hookdeck.extra.cli_binary, or switch to mode: push",
            )
        ]

    # Report the path, not just the name: a shadowed install is how you end up
    # version-checking one binary and launching another.
    shadowed = _other_hookdeck_binaries(resolved)
    found = Check(
        True,
        f"Hookdeck CLI found: {resolved}",
        note=(
            f"{len(shadowed)} other hookdeck binary(ies) shadowed by it: "
            f"{', '.join(shadowed)}"
            if shadowed
            else ""
        ),
    )

    version = _cli_version(resolved)
    current = _version_at_least(version, MIN_CLI_VERSION)
    return [
        found,
        Check(
            current,
            f"Hookdeck CLI {version or 'unknown'} meets the {MIN_CLI_VERSION_TEXT} minimum"
            if current
            else f"Hookdeck CLI {version or 'unknown'} at {resolved} is below "
            f"{MIN_CLI_VERSION_TEXT}. Earlier versions stop delivering after a "
            "session expires, without saying so. Upgrade, or point "
            "platforms.hookdeck.extra.cli_binary at a newer install.",
        ),
    ]


def _report_stranded_runs() -> None:
    """Deliveries whose outcome was never recorded — usually none."""
    path = _ledger_path()
    if not path.exists():
        return
    ledger = RunLedger(path)
    try:
        stale = ledger.stale_running(3600)
        if not stale:
            return
        print(
            f"\n! {len(stale)} delivery(ies) still marked running after an hour "
            "— a crash probably lost their outcome. Replay them with:"
        )
        for row in stale[:10]:
            print(f"    hermes hookdeck retry {row['event_id']}")
    finally:
        ledger.close()


async def _check_live_connections(routes: dict, extra: dict) -> list[Check]:
    """Reachability, plus whether each retry rule covers what the adapter emits.

    A rule narrower than the emitted statuses is silent data loss — a deferred
    or failed event that Hookdeck never brings back — so it is worth a network
    round trip to catch.
    """
    checks: list[Check] = []
    async with _api() as api:
        # By name, one request per configured route. Listing a page and
        # filtering would silently skip routes in a project with more
        # connections than fit — which is precisely the drift this check exists
        # to catch.
        found: list[dict] = []
        for route_name in routes:
            result = await api.list_connections(name=route_name, limit=10)
            found += (result or {}).get("models") or (result or {}).get("data") or []

        for connection in found:
            if connection.get("name") not in routes:
                continue
            for rule in connection.get("rules") or []:
                if rule.get("type") != "retry":
                    continue
                missing = uncovered_statuses(rule.get("response_status_codes"))
                if missing:
                    checks.append(
                        Check(
                            False,
                            f"connection '{connection.get('name')}' has a retry "
                            f"rule that does not cover {missing} — the adapter "
                            "emits those, so deferred and failed events would "
                            "never come back. Re-run `hermes hookdeck setup`.",
                        )
                    )
                checks.append(_burst_headroom(connection, rule, extra))
        checks += await _check_source_verification(api, routes)
    checks.append(Check(True, "Hookdeck API reachable and the key is accepted"))
    return checks


def _cmd_doctor(_args: argparse.Namespace) -> int:
    extra = _platform_extra()
    mode = extra.get("mode") or os.getenv(MODE_ENV) or "cli"
    routes = routes_from_config(_load_hermes_config())

    checks = [*_check_credentials(extra), _check_routes(routes)]
    if mode == "cli":
        checks += _check_cli(extra)
        checks.append(_check_cli_project(extra))
    else:
        checks.append(
            Check(
                bool(extra.get("public_url")),
                "public_url is set for push mode"
                if extra.get("public_url")
                else "push mode with no public_url — setup cannot build a "
                "destination URL",
            )
        )

    for check in checks:
        check.render()

    print(
        f"\nMode: {mode}  port: {extra.get('port', DEFAULT_PORT)}  "
        f"path: {extra.get('path', DEFAULT_PATH)}"
    )
    _report_stranded_runs()

    if api_key():
        print()
        try:
            live = run_sync(_check_live_connections(routes, extra))
        except HookdeckAPIError as exc:
            live = [Check(False, f"Hookdeck API check failed: {exc}")]
        for check in live:
            check.render()
        checks += live

    return 1 if any(not check.ok for check in checks) else 0


def hookdeck_command(args: argparse.Namespace) -> int:
    action = getattr(args, "hookdeck_action", None)
    if not action:
        print("Usage: hermes hookdeck {setup|status|pause|resume|retry|doctor}")
        return 2
    if action == "setup":
        return _cmd_setup(args)
    if action == "status":
        return _cmd_status(args)
    if action == "pause":
        return _cmd_pause(args, pause=True)
    if action == "resume":
        return _cmd_pause(args, pause=False)
    if action == "retry":
        return _cmd_retry(args)
    if action == "doctor":
        return _cmd_doctor(args)
    print(f"Unknown action: {action}")
    return 2
