"""The Hookdeck platform adapter.

Hermes already knows how to turn a webhook payload into an agent run: match a
route, render a prompt template, dispatch, deliver the response. This adapter
subclasses the built-in :class:`WebhookAdapter` so all of that — template
rendering, payload filters, route scripts, ``deliver_only`` direct delivery,
cross-platform response delivery — is inherited rather than reimplemented, and
keeps working as core evolves.

What it replaces is the ingest half:

* one signature scheme (Hookdeck's) instead of a per-provider zoo
* deduplication keyed on the Hookdeck event id and persisted to SQLite, so a
  gateway restart cannot cause a second agent run for the same event
* admission control — an event that arrives while ``max_concurrent`` runs are
  already in flight gets 503 + ``Retry-After`` and stays in Hookdeck's queue
  instead of being dropped by a fixed per-minute rate limit
* outcome reporting — the built-in adapter returns 202 and then forgets, so a
  failed run is lost. This one records the real outcome and hands failures back
  to Hookdeck for redelivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.platforms.webhook import WebhookAdapter

from .api import HookdeckAPI, HookdeckAPIError
from .constants import (
    ACK_MODES,
    ATTEMPT_COUNT,
    DEFAULT_ACK_MODE,
    DEFAULT_HEADER_PREFIX,
    DEFAULT_LEDGER_TTL_SECONDS,
    DEFAULT_MAX_AGENT_RETRIES,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEFAULT_RETRY_AFTER_SECONDS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    DEFAULT_SYNC_TIMEOUT_SECONDS,
    EVENT_ID,
    INSECURE_NO_AUTH,
    PLATFORM_NAME,
    SOURCE_NAME,
    WILL_RETRY_AFTER,
    header_name,
)
from .state import DeliveryLedger
from .tunnel import HookdeckCLIMissing, HookdeckTunnel
from .verify import verify_signature

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host: Optional[str]) -> bool:
    return bool(host) and host in _LOOPBACK_HOSTS


def _default_state_path() -> Path:
    home = os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "hookdeck" / "state.db"


def _dig(payload: Any, dotted: str) -> Any:
    value = payload
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


class HookdeckAdapter(WebhookAdapter):
    """Receives Hookdeck deliveries and turns them into agent runs."""

    # A webhook run has no human waiting to answer "session restored — what
    # next?", so a resumed run must finish the work rather than ask.
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        # WebhookAdapter pins Platform.WEBHOOK; this adapter is registered under
        # its own name so the gateway can run both side by side.
        self.platform = Platform(PLATFORM_NAME)

        extra = config.extra or {}
        self._mode = str(extra.get("mode") or os.getenv("HOOKDECK_MODE") or "cli").lower()
        self._port = int(extra.get("port") or os.getenv("HOOKDECK_PORT") or DEFAULT_PORT)
        raw_path = str(extra.get("path") or os.getenv("HOOKDECK_PATH") or DEFAULT_PATH)
        self._path = "/" + raw_path.strip("/")

        # cli mode is loopback-only by construction: the CLI is the only thing
        # that should be able to reach the listener.
        if self._mode == "cli":
            self._host = "127.0.0.1"
        else:
            self._host = extra.get("host") or None

        self._signing_secret = str(
            extra.get("secret") or os.getenv("HOOKDECK_WEBHOOK_SECRET") or ""
        )
        self._header_prefix = str(extra.get("header_prefix") or DEFAULT_HEADER_PREFIX)
        self._ack_mode = str(extra.get("ack_mode") or DEFAULT_ACK_MODE).lower()
        self._max_concurrent = int(extra.get("max_concurrent", DEFAULT_MAX_CONCURRENT))
        self._max_agent_retries = int(
            extra.get("max_agent_retries", DEFAULT_MAX_AGENT_RETRIES)
        )
        self._retry_after = int(
            extra.get("retry_after_seconds", DEFAULT_RETRY_AFTER_SECONDS)
        )
        self._sync_timeout = float(
            extra.get("sync_timeout_seconds", DEFAULT_SYNC_TIMEOUT_SECONDS)
        )
        self._run_timeout = float(
            extra.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
        )
        self._ledger_ttl = float(
            extra.get("ledger_ttl_seconds", DEFAULT_LEDGER_TTL_SECONDS)
        )
        self._source = str(extra.get("source") or os.getenv("HOOKDECK_SOURCE") or "")
        self._cancel_retries_on_unparseable = bool(
            extra.get("cancel_retries_on_unparseable", False)
        )
        self._recover_on_boot = bool(extra.get("recover_on_boot", True))
        # Explicit binary for setups with more than one hookdeck on PATH — an
        # npm global shim shadowing a Homebrew install is the common case, and
        # it silently picks the older one.
        self._cli_binary = str(extra.get("cli_binary") or "hookdeck")
        self._state_path = Path(
            extra.get("state_path") or _default_state_path()
        ).expanduser()

        self._ledger: Optional[DeliveryLedger] = None
        self._api: Optional[HookdeckAPI] = None
        self._tunnels: list[HookdeckTunnel] = []
        self._hd_runner = None

        # session chat_id -> in-flight run bookkeeping
        self._inflight: Dict[str, dict] = {}
        # session chat_id -> future resolved by on_processing_complete (sync mode)
        self._waiters: Dict[str, asyncio.Future] = {}
        self._last_prune = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _validate_startup(self) -> None:
        if self._ack_mode not in ACK_MODES:
            raise ValueError(
                f"[hookdeck] Unknown ack_mode {self._ack_mode!r}. "
                f"Expected one of: {', '.join(ACK_MODES)}."
            )
        if self._mode not in {"cli", "push"}:
            raise ValueError(
                f"[hookdeck] Unknown mode {self._mode!r}. Expected 'cli' or 'push'."
            )
        if not self._signing_secret:
            raise ValueError(
                "[hookdeck] No signing secret. Set HOOKDECK_WEBHOOK_SECRET (or "
                "platforms.hookdeck.extra.secret) to the signing secret from "
                "your Hookdeck project settings. For local testing only, set it "
                f"to '{INSECURE_NO_AUTH}' while bound to loopback."
            )
        if self._signing_secret == INSECURE_NO_AUTH and not _is_loopback(self._host):
            raise ValueError(
                f"[hookdeck] {INSECURE_NO_AUTH} is set but the listener is bound "
                f"to non-loopback host {self._host!r}. Refusing to start: that "
                "would expose an unauthenticated agent-dispatch endpoint."
            )
        for name, route in (self._routes or {}).items():
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    raise ValueError(
                        f"[hookdeck] Route '{name}' sets deliver_only but "
                        f"deliver is '{deliver}'. Direct delivery needs a real "
                        "target (telegram, slack, github_comment, …)."
                    )
        if self._mode == "cli" and not self._tunnel_plan():
            raise ValueError(
                "[hookdeck] cli mode needs a Hookdeck source to listen to, and "
                "no route declares one. Set `source` on each route, or "
                "platforms.hookdeck.extra.source for a single-route setup."
            )

    def _tunnel_plan(self) -> dict[str, str]:
        """Map route name -> Hookdeck source name for the CLI tunnels.

        ``hookdeck listen`` forwards exactly one source, so each route gets its
        own tunnel. Routes sharing a source still get one each, because the CLI
        path — and therefore the route the adapter resolves — differs per route.
        """
        plan: dict[str, str] = {}
        for name, route in (self._routes or {}).items():
            source = str(route.get("source") or "")
            if not source and self._source:
                source = self._source
            if source:
                plan[name] = source
        return plan

    def build_app(self) -> web.Application:
        """The aiohttp application serving Hookdeck deliveries.

        Split out from :meth:`connect` so it can be driven directly by tests
        without binding a port or starting the CLI tunnel.
        """
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get(f"{self._path}/health", self._handle_hookdeck_health)
        app.router.add_post(self._path, self._handle_hookdeck)
        app.router.add_post(f"{self._path}/{{route_name}}", self._handle_hookdeck)
        return app

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._validate_startup()

        self._ledger = DeliveryLedger(self._state_path)
        self._api = HookdeckAPI()

        app = self.build_app()
        self._hd_runner = web.AppRunner(app)
        await self._hd_runner.setup()
        if not await self._start_sites():
            await self._hd_runner.cleanup()
            self._hd_runner = None
            return False

        if self._mode == "cli":
            try:
                for route_name, source in self._tunnel_plan().items():
                    tunnel = HookdeckTunnel(
                        port=self._port,
                        path=f"{self._path}/{route_name}",
                        source=source,
                        connection_name=route_name,
                        binary=self._cli_binary,
                    )
                    await tunnel.start()
                    self._tunnels.append(tunnel)
            except HookdeckCLIMissing as exc:
                logger.error("[hookdeck] %s", exc)
                await self._stop_tunnels()
                await self._hd_runner.cleanup()
                self._hd_runner = None
                return False

        await self._recover_orphaned_runs()

        self._mark_connected()
        logger.info(
            "[hookdeck] mode=%s listening on %s:%d%s — ack_mode=%s "
            "max_concurrent=%s routes=%s",
            self._mode,
            self._host or "*",
            self._port,
            self._path,
            self._ack_mode,
            self._max_concurrent or "unlimited",
            ", ".join(self._routes.keys()) or "(none)",
        )
        return True

    def _bind_hosts(self) -> list[Optional[str]]:
        """Addresses to listen on.

        In cli mode this is both loopback families, not just ``127.0.0.1``.
        The Hookdeck CLI forwards to ``http://localhost:<port>``, and on a
        dual-stack machine that resolves to ``::1`` first — against an
        IPv4-only listener every delivery fails with ECONNREFUSED while the
        tunnel itself looks perfectly healthy. Binding a wildcard would fix it
        too, and would also expose an agent-dispatch endpoint to the network,
        so it binds each loopback address instead.
        """
        if self._mode == "cli":
            return ["127.0.0.1", "::1"]
        return [self._host]

    async def _start_sites(self) -> bool:
        """Start a listener per address. One family may be absent; both failing is fatal."""
        assert self._hd_runner is not None
        started: list[str] = []
        last_error: Optional[OSError] = None
        for host in self._bind_hosts():
            try:
                await web.TCPSite(self._hd_runner, host, self._port).start()
                started.append(host or "*")
            except OSError as exc:
                last_error = exc
                logger.debug(
                    "[hookdeck] Could not bind %s:%d: %s", host, self._port, exc
                )
        if not started:
            logger.error(
                "[hookdeck] Could not bind port %d on %s: %s. Change "
                "platforms.hookdeck.extra.port in config.yaml.",
                self._port,
                ", ".join(str(h) for h in self._bind_hosts()),
                last_error,
            )
            return False
        logger.debug("[hookdeck] Listening on %s", ", ".join(started))
        return True

    async def _recover_orphaned_runs(self) -> int:
        """Ask Hookdeck to redeliver runs the previous process died holding.

        A ledger row still marked ``running`` at startup is an orphan by
        definition: the process that owned it is gone, and because the adapter
        acked 202 the moment it admitted the event, Hookdeck considers that
        delivery successful and will never bring it back on its own.

        This is the second half of what makes Hookdeck the durable work queue
        and lets the plugin skip owning a local one — the first half being the
        failed-run retry. It works because a successful event can still be
        retried manually. It is also why terminal rows are pruned on a TTL but
        ``running`` rows never are: pruning one would silently drop the work.

        Recovery re-runs an event whose run may in fact have completed just
        before the crash, which is the at-least-once contract the whole design
        already assumes. Set ``recover_on_boot: false`` if that is wrong for
        your routes.
        """
        if not self._recover_on_boot or self._ledger is None or self._api is None:
            return 0

        orphans = self._ledger.all_running()
        if not orphans:
            return 0

        recovered = 0
        for row in orphans:
            event_id = row["event_id"]
            attempts = int(row["agent_attempts"])
            if attempts - 1 >= self._max_agent_retries:
                self._ledger.mark_exhausted(event_id, "interrupted; retry budget spent")
                continue
            self._ledger.mark_failed(event_id, "gateway stopped mid-run")
            try:
                await self._api.retry_event(event_id)
                recovered += 1
            except HookdeckAPIError as exc:
                logger.error(
                    "[hookdeck] Could not recover interrupted event %s: %s",
                    event_id,
                    exc,
                )

        logger.warning(
            "[hookdeck] Found %d run(s) interrupted by a previous shutdown; "
            "asked Hookdeck to redeliver %d of them",
            len(orphans),
            recovered,
        )
        return recovered

    async def _stop_tunnels(self) -> None:
        for tunnel in self._tunnels:
            await tunnel.stop()
        self._tunnels = []

    async def disconnect(self) -> None:
        await self._stop_tunnels()
        if self._hd_runner is not None:
            await self._hd_runner.cleanup()
            self._hd_runner = None
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None
        self._mark_disconnected()
        logger.info("[hookdeck] Disconnected")

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def _handle_hookdeck_health(self, request: web.Request) -> web.Response:
        counts = self._ledger.counts() if self._ledger else {}
        return web.json_response(
            {
                "status": "ok",
                "mode": self._mode,
                "ack_mode": self._ack_mode,
                "in_flight": len(self._inflight),
                "max_concurrent": self._max_concurrent,
                "deliveries": counts,
            }
        )

    def _header(self, request: web.Request, suffix: str) -> str:
        return request.headers.get(header_name(suffix, self._header_prefix), "")

    def _resolve_route(
        self, request: web.Request, source_name: str
    ) -> tuple[str, Optional[dict]]:
        """Pick the route for this delivery.

        Explicit path segment wins. Otherwise a route may claim a Hookdeck
        source by name (``source: stripe``), then a route named after the
        source, then a route literally named ``default``, and finally — when
        only one route is configured — that one.
        """
        explicit = request.match_info.get("route_name", "")
        if explicit:
            return explicit, self._routes.get(explicit)

        if source_name:
            for name, route in self._routes.items():
                if str(route.get("source", "")) == source_name:
                    return name, route
            if source_name in self._routes:
                return source_name, self._routes[source_name]

        if "default" in self._routes:
            return "default", self._routes["default"]
        if len(self._routes) == 1:
            name = next(iter(self._routes))
            return name, self._routes[name]
        return source_name or "(unmatched)", None

    def _event_type(self, request: web.Request, route: dict, payload: Any) -> str:
        """Derive the event type used by the route's ``events`` filter.

        Hookdeck forwards the provider's original headers, so the header-based
        detection Hermes already does still works. ``event_path`` covers the
        providers that put the type in the body under a non-standard key.
        """
        event_path = route.get("event_path")
        if event_path:
            value = _dig(payload, str(event_path))
            if value is not None:
                return str(value)
        header_value = (
            request.headers.get("X-GitHub-Event")
            or request.headers.get("X-GitLab-Event")
            or request.headers.get("X-Shopify-Topic")
            or ""
        )
        if header_value:
            return header_value
        if isinstance(payload, dict):
            for key in ("event_type", "type", "event", "topic"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        return "unknown"

    def _sweep_inflight(self) -> None:
        """Release slots whose completion hook never fired.

        ``on_processing_complete`` runs on the success, failure and cancellation
        paths, so this should stay empty — but a slot leaked by an unexpected
        crash would silently wedge admission control at zero capacity, which is
        worse than the small chance of releasing a slot early.
        """
        now = time.time()
        if now - self._last_prune < 30:
            return
        self._last_prune = now
        for chat_id, info in list(self._inflight.items()):
            if now - info["started"] > self._run_timeout:
                logger.warning(
                    "[hookdeck] Run for %s exceeded %.0fs with no completion "
                    "signal — releasing its slot",
                    chat_id,
                    self._run_timeout,
                )
                self._inflight.pop(chat_id, None)
        if self._ledger is not None:
            self._ledger.prune(self._ledger_ttl)

    async def _handle_hookdeck(self, request: web.Request) -> web.Response:
        self._sweep_inflight()

        if (request.content_length or 0) > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "Payload too large"}, status=413)
        except Exception as exc:
            logger.error("[hookdeck] Failed to read body: %s", exc)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)

        # ── Verify before anything else touches the payload ──────────
        if self._signing_secret != INSECURE_NO_AUTH:
            if not verify_signature(
                request.headers,
                raw_body,
                self._signing_secret,
                prefix=self._header_prefix,
            ):
                logger.warning(
                    "[hookdeck] Rejected delivery with an invalid signature "
                    "(source=%s)",
                    self._header(request, SOURCE_NAME) or "unknown",
                )
                return web.json_response({"error": "Invalid signature"}, status=401)

        source_name = self._header(request, SOURCE_NAME)
        event_id = self._header(request, EVENT_ID) or request.headers.get(
            "X-Request-ID", ""
        )
        try:
            attempt = int(self._header(request, ATTEMPT_COUNT) or 0)
        except ValueError:
            attempt = 0
        # Hookdeck omits (or empties) this header on the final automatic
        # attempt, which is the cleanest dead-letter signal available.
        is_last_attempt = not self._header(request, WILL_RETRY_AFTER).strip()

        route_name, route = self._resolve_route(request, source_name)
        if route is None:
            logger.warning(
                "[hookdeck] No route for delivery (source=%s, path=%s)",
                source_name or "unknown",
                request.path,
            )
            return web.json_response(
                {"error": f"No route matches source '{source_name}'"}, status=404
            )
        if route.get("enabled", True) is False:
            return web.json_response(
                {"status": "ignored", "reason": "route disabled", "route": route_name}
            )

        try:
            # Not just JSONDecodeError: json.loads sniffs the encoding and
            # raises UnicodeDecodeError on a non-UTF-8 body, which would
            # otherwise escape as a 500 and be retried 50 times.
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                import urllib.parse

                payload = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
            except Exception:
                return self._unparseable_response(event_id)

        event_type = self._event_type(request, route, payload)
        allowed = route.get("events", [])
        if allowed and event_type not in allowed:
            return web.json_response({"status": "ignored", "event": event_type})

        if not self._route_processor.route_filters_match(
            route, payload, event_type, request.headers
        ):
            return web.json_response(
                {"status": "ignored", "reason": "filter", "route": route_name}
            )

        if route.get("script"):
            keep, transformed = await asyncio.to_thread(
                self._route_processor.run_route_script, route.get("script"), payload
            )
            if not keep:
                return web.json_response(
                    {"status": "ignored", "reason": "script", "route": route_name}
                )
            payload = transformed or payload

        prompt = self._render_prompt(
            route.get("prompt", ""), payload, event_type, route_name
        )

        # ── Direct delivery: no agent, no queue slot ─────────────────
        if route.get("deliver_only"):
            delivery = {
                "deliver": route.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route.get("deliver_extra", {}), payload
                ),
            }
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[hookdeck] direct-deliver failed route=%s event=%s",
                    route_name,
                    event_id,
                )
                # 5xx so Hookdeck retries this delivery on its own schedule.
                return web.json_response(
                    {"status": "error", "error": "Delivery failed"}, status=502
                )
            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "event_id": event_id,
                    }
                )
            return web.json_response(
                {"status": "error", "error": "Delivery failed"}, status=502
            )

        # ── Admission control ────────────────────────────────────────
        # 503 leaves the event in Hookdeck's queue instead of dropping it, and
        # Retry-After tells Hookdeck when to come back. This is the difference
        # between a burst being absorbed and a burst being lost.
        if self._max_concurrent and len(self._inflight) >= self._max_concurrent:
            logger.info(
                "[hookdeck] At capacity (%d runs in flight) — deferring event %s",
                len(self._inflight),
                event_id or "(no id)",
            )
            return web.json_response(
                {
                    "status": "deferred",
                    "reason": "max_concurrent",
                    "in_flight": len(self._inflight),
                },
                status=503,
                headers={"Retry-After": str(self._retry_after)},
            )

        # ── Deduplication ────────────────────────────────────────────
        session_chat_id = f"hookdeck:{route_name}:{event_id or int(time.time() * 1000)}"
        if event_id and self._ledger is not None:
            admission = self._ledger.admit(
                event_id,
                route=route_name,
                attempt=attempt,
                session_chat_id=session_chat_id,
            )
            if not admission.admitted:
                logger.info(
                    "[hookdeck] Skipping %s: %s", event_id, admission.reason
                )
                return web.json_response(
                    {"status": "duplicate", "event_id": event_id}, status=200
                )

        prompt = self._apply_skills(route, prompt)

        self._delivery_info[session_chat_id] = {
            "deliver": route.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route.get("deliver_extra", {}), payload
            ),
        }
        now = time.time()
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"hookdeck/{route_name}",
            chat_type="webhook",
            user_id=f"hookdeck:{route_name}",
            user_name=route_name,
        )
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=event_id or None,
            metadata={
                "hookdeck_event_id": event_id,
                "hookdeck_source": source_name,
                "hookdeck_attempt": attempt,
                "hookdeck_route": route_name,
                "hookdeck_last_automatic_attempt": is_last_attempt,
            },
        )

        self._inflight[session_chat_id] = {
            "event_id": event_id,
            "route": route_name,
            "started": now,
            "last_attempt": is_last_attempt,
        }
        logger.info(
            "[hookdeck] dispatch route=%s event_type=%s event_id=%s attempt=%s "
            "prompt_len=%d",
            route_name,
            event_type,
            event_id or "(none)",
            attempt or "?",
            len(prompt),
        )

        if self._ack_mode == "sync":
            return await self._dispatch_sync(event, session_chat_id, route_name)

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "event_id": event_id,
            },
            status=202,
        )

    def _unparseable_response(self, event_id: str) -> web.Response:
        """400 for a body that is neither JSON nor form-encoded.

        Hookdeck honours ``Retry-After: -1`` as "cancel all further automatic
        retries", which turns 50 doomed attempts into one for a payload no
        retry can fix. It is off by default because the failure mode is
        severe and silent: an over-strict parser would discard live traffic,
        unrecoverable once retention lapses. Turn it on only once you have
        watched the counter in ``hermes hookdeck status`` and are satisfied
        nothing legitimate is landing here.
        """
        headers = {}
        if self._cancel_retries_on_unparseable:
            headers["Retry-After"] = "-1"
            logger.warning(
                "[hookdeck] Event %s has an unparseable body — cancelling its "
                "automatic retries (cancel_retries_on_unparseable is on)",
                event_id or "(no id)",
            )
            if self._ledger is not None and event_id:
                self._ledger.record_cancelled(event_id, "unparseable body")
        else:
            logger.warning(
                "[hookdeck] Event %s has an unparseable body", event_id or "(no id)"
            )
        return web.json_response(
            {"error": "Cannot parse body"}, status=400, headers=headers
        )

    def _apply_skills(self, route: dict, prompt: str) -> str:
        """Prepend the first configured skill's invocation to the prompt."""
        skills = route.get("skills", [])
        if not skills:
            return prompt
        try:
            from agent.skill_commands import (
                build_skill_invocation_message,
                get_skill_commands,
            )

            skill_cmds = get_skill_commands()
            for skill_name in skills:
                key = f"/{skill_name}"
                if key in skill_cmds:
                    content = build_skill_invocation_message(
                        key, user_instruction=prompt
                    )
                    if content:
                        return content
                else:
                    logger.warning("[hookdeck] Skill '%s' not found", skill_name)
        except Exception as exc:
            logger.warning("[hookdeck] Skill loading failed: %s", exc)
        return prompt

    async def _dispatch_sync(
        self, event: MessageEvent, session_chat_id: str, route_name: str
    ) -> web.Response:
        """Hold the response open until the run finishes, or time out.

        ``handle_message`` is fire-and-forget by design, so "synchronous" here
        means waiting on the completion hook rather than awaiting the dispatch
        call. On timeout the response degrades to 202: the run is still going,
        and answering 5xx would make Hookdeck redeliver work that is in flight.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._waiters[session_chat_id] = waiter

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        try:
            outcome = await asyncio.wait_for(waiter, timeout=self._sync_timeout)
        except asyncio.TimeoutError:
            logger.info(
                "[hookdeck] Run for %s still going after %.0fs — acking 202",
                session_chat_id,
                self._sync_timeout,
            )
            return web.json_response(
                {"status": "accepted", "reason": "still running", "route": route_name},
                status=202,
            )
        finally:
            self._waiters.pop(session_chat_id, None)

        if outcome == ProcessingOutcome.SUCCESS:
            return web.json_response({"status": "processed", "route": route_name})
        # 5xx puts the event back on Hookdeck's retry schedule.
        return web.json_response(
            {"status": "failed", "route": route_name, "outcome": str(outcome)},
            status=500,
        )

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    async def on_processing_complete(
        self, event: MessageEvent, outcome: Any
    ) -> None:
        """Record the run's true outcome, then let the base class close the session.

        This is the seam the built-in adapter leaves unused for reliability. It
        fires at the real end of the run — success, failure and cancellation
        alike — which is the only point at which we know whether the event was
        actually handled.
        """
        chat_id = getattr(getattr(event, "source", None), "chat_id", "") or ""
        info = self._inflight.pop(chat_id, None)

        waiter = self._waiters.get(chat_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)

        event_id = (info or {}).get("event_id") or ""
        if event_id and self._ledger is not None:
            try:
                await self._record_outcome(
                    event_id, outcome, last_attempt=(info or {}).get("last_attempt", False)
                )
            except Exception:
                logger.exception(
                    "[hookdeck] Failed to record outcome for event %s", event_id
                )

        try:
            await super().on_processing_complete(event, outcome)
        except Exception:
            logger.debug("[hookdeck] Base completion hook failed", exc_info=True)

    async def _record_outcome(
        self, event_id: str, outcome: Any, *, last_attempt: bool = False
    ) -> None:
        assert self._ledger is not None
        if outcome == ProcessingOutcome.SUCCESS:
            self._ledger.mark_succeeded(event_id)
            return

        attempts = self._ledger.agent_attempts(event_id)
        reason = str(getattr(outcome, "value", outcome))
        # ``attempts`` counts runs, so retries already requested is one less.
        # max_agent_retries=2 means the first run plus two more.
        if attempts - 1 >= self._max_agent_retries:
            self._ledger.mark_exhausted(event_id, reason)
            logger.error(
                "[hookdeck] Event %s failed %d times — giving up. Inspect it in "
                "Hookdeck and replay with `hermes hookdeck replay %s`.",
                event_id,
                attempts,
                event_id,
            )
            return

        if last_attempt:
            # Hookdeck's own automatic retries are done for this event, so
            # nothing else will bring it back on its own. Manual retries are
            # unlimited, which is what the request below is — but say so
            # loudly, because this is the dead-letter moment.
            logger.warning(
                "[hookdeck] Event %s failed on its last automatic attempt — "
                "requesting a manual redelivery; if that fails too, only "
                "`hermes hookdeck replay %s` will bring it back",
                event_id,
                event_id,
            )

        self._ledger.mark_failed(event_id, reason)
        if self._ack_mode != "async_retry" or self._api is None:
            return
        try:
            await self._api.retry_event(event_id)
            logger.info(
                "[hookdeck] Agent run failed for event %s (attempt %d/%d) — "
                "asked Hookdeck to redeliver",
                event_id,
                attempts,
                self._max_agent_retries,
            )
        except HookdeckAPIError as exc:
            logger.error("[hookdeck] Could not request redelivery: %s", exc)


# ----------------------------------------------------------------------
# Registry hooks
# ----------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive dependency probe — must not install anything.

    ``find_spec`` rather than a real import: the registry calls this from status
    displays, and importing here would drag the HTTP stack into every
    ``hermes status``.
    """
    from importlib.util import find_spec

    return all(find_spec(module) is not None for module in ("aiohttp", "httpx"))


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", None) or {}
    has_secret = bool(extra.get("secret") or os.getenv("HOOKDECK_WEBHOOK_SECRET"))
    has_routes = bool(extra.get("routes"))
    return has_secret and has_routes


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env so ``hermes gateway status`` can
    report an env-only setup without constructing the adapter."""
    if not os.getenv("HOOKDECK_WEBHOOK_SECRET"):
        return None
    seeded: dict[str, Any] = {"secret": os.getenv("HOOKDECK_WEBHOOK_SECRET", "")}
    for env_var, key, cast in (
        ("HOOKDECK_MODE", "mode", str),
        ("HOOKDECK_PORT", "port", int),
        ("HOOKDECK_PATH", "path", str),
        ("HOOKDECK_SOURCE", "source", str),
    ):
        value = os.getenv(env_var)
        if value:
            try:
                seeded[key] = cast(value)
            except ValueError:
                logger.warning("[hookdeck] Ignoring invalid %s=%r", env_var, value)
    return seeded
