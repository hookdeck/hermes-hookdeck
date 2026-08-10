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
from typing import Any, Optional

from .api import HookdeckAPI, HookdeckAPIError, run_sync
from .constants import DEFAULT_PATH, DEFAULT_PORT
from .provision import (
    build_connection_payload,
    routes_from_config,
    summarise_payload,
    uncovered_statuses,
)
from .state import DeliveryLedger, default_state_path

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
    except Exception:
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


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _load_hermes_config() -> dict:
    path = _hermes_home() / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print(f"! Cannot read {path}: PyYAML is not installed")
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        print(f"! Cannot parse {path}: {exc}")
        return {}


def _platform_extra() -> dict:
    config = _load_hermes_config()
    gateway = config.get("gateway") or {}
    platforms = gateway.get("platforms") or config.get("platforms") or {}
    return ((platforms.get("hookdeck") or {}).get("extra")) or {}


def _ledger_path() -> Path:
    extra = _platform_extra()
    return Path(extra.get("state_path") or default_state_path())


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

    replay = subs.add_parser("replay", help="Ask Hookdeck to redeliver events")
    replay.add_argument("event_id", nargs="?", default="", help="A single event id")
    replay.add_argument("--failed", action="store_true", help="Replay all failed events")
    replay.add_argument("--since", default="", help="ISO 8601 lower bound for --failed")
    replay.add_argument("--connection", default="", help="Restrict --failed to a connection id")

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


def _cmd_setup(args: argparse.Namespace) -> int:
    routes = routes_from_config(_load_hermes_config())
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

    payloads = {
        name: _payload_for_route(args, name, route) for name, route in targets.items()
    }

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
            "\nNext: set HOOKDECK_WEBHOOK_SECRET to your project's signing "
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
    ledger = DeliveryLedger(path)
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


async def _resolve_connection_id(api: HookdeckAPI, value: str) -> Optional[str]:
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


def _cmd_replay(args: argparse.Namespace) -> int:
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
                print("Usage: hermes hookdeck replay <event_id> | --failed [--since ISO]")
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
    return [
        Check(
            bool(os.getenv("HOOKDECK_API_KEY")),
            "HOOKDECK_API_KEY is set"
            if os.getenv("HOOKDECK_API_KEY")
            else "HOOKDECK_API_KEY is not set — setup, status and replay will not work",
        ),
        Check(
            bool(extra.get("secret") or os.getenv("HOOKDECK_WEBHOOK_SECRET")),
            "Signing secret is configured"
            if (extra.get("secret") or os.getenv("HOOKDECK_WEBHOOK_SECRET"))
            else "No signing secret — the adapter will refuse to start. Set "
            "HOOKDECK_WEBHOOK_SECRET to your project's signing secret.",
        ),
    ]


def _check_routes(routes: dict) -> Check:
    return Check(
        bool(routes),
        f"{len(routes)} route(s) configured: {', '.join(routes)}"
        if routes
        else "No routes under platforms.hookdeck.extra.routes",
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
    ledger = DeliveryLedger(path)
    try:
        stale = ledger.stale_running(3600)
        if not stale:
            return
        print(
            f"\n! {len(stale)} delivery(ies) still marked running after an hour "
            "— a crash probably lost their outcome. Replay them with:"
        )
        for row in stale[:10]:
            print(f"    hermes hookdeck replay {row['event_id']}")
    finally:
        ledger.close()


async def _check_live_connections(routes: dict) -> list[Check]:
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
    checks.append(Check(True, "Hookdeck API reachable and the key is accepted"))
    return checks


def _cmd_doctor(_args: argparse.Namespace) -> int:
    extra = _platform_extra()
    mode = extra.get("mode") or os.getenv("HOOKDECK_MODE") or "cli"
    routes = routes_from_config(_load_hermes_config())

    checks = [*_check_credentials(extra), _check_routes(routes)]
    if mode == "cli":
        checks += _check_cli(extra)
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

    if os.getenv("HOOKDECK_API_KEY"):
        print()
        try:
            live = run_sync(_check_live_connections(routes))
        except HookdeckAPIError as exc:
            live = [Check(False, f"Hookdeck API check failed: {exc}")]
        for check in live:
            check.render()
        checks += live

    return 1 if any(not check.ok for check in checks) else 0


def hookdeck_command(args: argparse.Namespace) -> int:
    action = getattr(args, "hookdeck_action", None)
    if not action:
        print("Usage: hermes hookdeck {setup|status|pause|resume|replay|doctor}")
        return 2
    if action == "setup":
        return _cmd_setup(args)
    if action == "status":
        return _cmd_status(args)
    if action == "pause":
        return _cmd_pause(args, pause=True)
    if action == "resume":
        return _cmd_pause(args, pause=False)
    if action == "replay":
        return _cmd_replay(args)
    if action == "doctor":
        return _cmd_doctor(args)
    print(f"Unknown action: {action}")
    return 2
