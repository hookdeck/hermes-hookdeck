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
* admission control — an event arriving while ``max_concurrent`` runs are
  already in flight is deferred with 503 and stays in Hookdeck's queue, rather
  than being dropped by a fixed per-minute rate limit
* outcome reporting — the built-in adapter answers 202 and then forgets, so a
  failed run is lost. This one records the real outcome and hands failures back
  to Hookdeck for redelivery.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by installs without the extra
    # aiohttp is a Hermes *extra* (`messaging`, `slack`, …), not a core
    # dependency, and core's own webhook adapter guards it the same way. A bare
    # module-level import would raise during plugin discovery, and since
    # `register()` degrades rather than crashes, the platform would simply
    # never appear — a silent absence with no error to debug.
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.webhook import WebhookAdapter

from . import payload as payload_mod
from . import routing
from .api import HookdeckAPI, HookdeckAPIError
from .constants import (
    ATTEMPT_COUNT,
    ATTEMPT_TRIGGER,
    EVENT_ID,
    OPERATOR_TRIGGERS,
    PLATFORM_NAME,
    SOURCE_NAME,
    WILL_RETRY_AFTER,
    assert_declared_status,
    header_name,
)
from .settings import AdapterSettings
from .state import DeliveryLedger
from .tunnel import HookdeckCLIMissing, HookdeckTunnel

logger = logging.getLogger(__name__)

# How often the in-flight sweep runs when driven by the request path, at most.
_SWEEP_INTERVAL_SECONDS = 30
# How often upkeep runs regardless of traffic.
_MAINTENANCE_INTERVAL_SECONDS = 30

# Handing a failed run back to Hookdeck is the last thing standing between it
# and a lost event, so a transient API failure is retried rather than logged.
_REDELIVERY_ATTEMPTS = 3
_REDELIVERY_RETRY_INITIAL_SECONDS = 1.0


@dataclass
class Delivery:
    """One Hookdeck delivery, after parsing and before dispatch."""

    event_id: str
    attempt: int
    source_name: str
    route_name: str
    route: dict
    payload: Any
    event_type: str
    is_last_attempt: bool
    #: True when a person asked for this delivery — a CLI replay, the
    #: dashboard's Replay button, or the agent's retry tool — rather than
    #: Hookdeck's own schedule.
    operator_initiated: bool = False
    surrogates_replaced: bool = False
    session_chat_id: str = ""
    prompt: str = ""

    @property
    def label(self) -> str:
        """Event id for logs, or a stand-in when the delivery carried none."""
        return self.event_id or "(no id)"


@dataclass
class InFlightRun:
    """Bookkeeping for a dispatched run, held until its outcome is known."""

    event_id: str
    route: str
    started: float
    is_last_attempt: bool = False
    #: Identifies which run is reporting, so a late completion cannot overwrite
    #: the status of a redelivery that has since taken over.
    session_chat_id: str = ""


class HookdeckAdapter(WebhookAdapter):
    """Receives Hookdeck deliveries and turns them into agent runs."""

    # A webhook run has no human waiting to answer "session restored — what
    # next?", so a resumed run must finish the work rather than ask.
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self.platform = self._resolve_platform()

        self.settings = AdapterSettings.from_extra(config.extra)
        # WebhookAdapter's own fields, kept in step so inherited helpers
        # (delivery-info pruning, body limits) see the same configuration.
        self._host = self.settings.host
        self._port = self.settings.port
        self._path = self.settings.path
        self._routes = self.settings.routes
        self._max_body_bytes = self.settings.max_body_bytes

        self._ledger: Optional[DeliveryLedger] = None
        self._api: Optional[HookdeckAPI] = None
        self._tunnels: list[HookdeckTunnel] = []
        self._site_runner = None

        self._inflight: dict[str, InFlightRun] = {}
        # Resolved by on_processing_complete; only `sync` mode waits on these.
        self._waiters: dict[str, asyncio.Future] = {}
        # sync-mode runs that outlasted their timeout and were acked 202. Their
        # failures need the same explicit hand-back an async_retry run gets,
        # because Hookdeck has already recorded the delivery as successful.
        self._acked_before_completion: set[str] = set()
        self._maintenance: Optional[asyncio.Task] = None
        self._last_sweep = 0.0

    @staticmethod
    def _resolve_platform() -> Platform:
        """This adapter's ``Platform`` member, which registration mints.

        ``WebhookAdapter`` pins ``Platform.WEBHOOK``; running alongside it
        requires a distinct member. Hermes only creates one for a name its
        registry already knows, which the real path satisfies because the
        gateway builds adapters through ``platform_registry.create_adapter``.
        Constructing the class directly beforehand fails here, and should:
        silently staying ``Platform.WEBHOOK`` would collide with the built-in.
        """
        try:
            return Platform(PLATFORM_NAME)
        except ValueError as exc:
            raise RuntimeError(
                f"[hookdeck] Platform({PLATFORM_NAME!r}) is not registered. "
                "Build the adapter via platform_registry.create_adapter (or "
                "call hookdeck.register(ctx) first) — Hermes only mints a "
                "Platform member for names the registry already knows."
            ) from exc

    @property
    def authorization_is_upstream(self) -> bool:
        """Inbound events were authorized before they got here.

        Without this every delivery is refused as ``Unauthorized user:
        hookdeck:<route>``. Core exempts its own webhook platform from the user
        allowlist by enum member, reasoning that HMAC verification in the
        adapter *is* the authorization. That reasoning applies here exactly,
        but the membership test cannot, since this platform is
        ``Platform.HOOKDECK``. ``authorization_is_upstream`` is the sanctioned
        route to the same outcome, and its contract fits: authorization
        performed by a trusted upstream over an authenticated transport, with
        no local policy to consult, because a Hookdeck source is not an account
        an operator configures in ``HOOKDECK_ALLOWED_USERS``.

        Not a fail-open — false whenever verification is off, so the local
        allowlist still applies to an ``INSECURE_NO_AUTH`` route. That makes it
        narrower than core's exemption, which covers built-in webhook routes
        even unverified.
        """
        return self.settings.verifies_signatures

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        """The aiohttp application serving Hookdeck deliveries.

        Split out from :meth:`connect` so tests can drive it without binding a
        port or starting a tunnel.
        """
        app = web.Application(client_max_size=self.settings.max_body_bytes)
        app.router.add_get(f"{self._path}/health", self._handle_health)
        app.router.add_post(self._path, self._handle_delivery)
        # A tail, not a single segment: Hookdeck appends the source request's
        # own path unless `path_forwarding_disabled` is set, so a provider
        # POSTing to <source-url>/events arrives as /hookdeck/<route>/events.
        app.router.add_post(f"{self._path}/{{route_tail:.*}}", self._handle_delivery)
        return app

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.settings.validate()

        self._ledger = DeliveryLedger(self.settings.state_path)
        self._api = HookdeckAPI()

        self._site_runner = web.AppRunner(self.build_app())
        await self._site_runner.setup()
        if not await self._start_sites():
            await self._abandon_connect()
            return False

        if self.settings.mode == "cli" and not await self._start_tunnels():
            await self._abandon_connect()
            return False

        await self._recover_orphaned_runs()
        await self._resume_due_connections()
        self._maintenance = asyncio.create_task(self._maintain())

        self._mark_connected()
        logger.info(
            "[hookdeck] mode=%s listening on %s:%d%s — ack_mode=%s "
            "max_concurrent=%s routes=%s",
            self.settings.mode,
            self._host or "*",
            self._port,
            self._path,
            self.settings.ack_mode,
            self.settings.max_concurrent or "unlimited",
            ", ".join(self._routes) or "(none)",
        )
        return True

    async def disconnect(self) -> None:
        await self._stop_maintenance()
        await self._stop_tunnels()
        await self._teardown_server()
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None
        self._mark_disconnected()
        logger.info("[hookdeck] Disconnected")

    async def _start_sites(self) -> bool:
        """Start a listener per address. One family may be absent; both failing is fatal."""
        assert self._site_runner is not None
        started: list[str] = []
        last_error: Optional[OSError] = None
        for host in self.settings.bind_hosts:
            try:
                await web.TCPSite(self._site_runner, host, self._port).start()
                started.append(host or "*")
            except OSError as exc:
                last_error = exc
                logger.debug("[hookdeck] Could not bind %s:%d: %s", host, self._port, exc)

        if not started:
            logger.error(
                "[hookdeck] Could not bind port %d on %s: %s. Change "
                "platforms.hookdeck.extra.port in config.yaml.",
                self._port,
                ", ".join(str(h) for h in self.settings.bind_hosts),
                last_error,
            )
            return False
        logger.debug("[hookdeck] Listening on %s", ", ".join(started))
        return True

    async def _start_tunnels(self) -> bool:
        try:
            for route_name, source in self.settings.tunnels.items():
                tunnel = HookdeckTunnel(
                    port=self._port,
                    path=f"{self._path}/{route_name}",
                    source=source,
                    connection_name=route_name,
                    binary=self.settings.cli_binary,
                    login=self.settings.cli_login,
                )
                await tunnel.start()
                self._tunnels.append(tunnel)
        except HookdeckCLIMissing as exc:
            logger.error("[hookdeck] %s", exc)
            await self._stop_tunnels()
            return False
        return True

    async def _maintain(self) -> None:
        """Periodic upkeep, independent of traffic.

        Both things this does are needed precisely when nothing is arriving. A
        paused connection produces no deliveries, so a resume deadline driven
        off the request path would never come due — the tool promises "it will
        resume automatically in N minutes" and it would not. A wedged run holds
        its slot on a quiet gateway for the same reason.
        """
        while True:
            await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)
            try:
                self._sweep_inflight(force=True)
                await self._resume_due_connections()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Upkeep must not be the thing that stops the adapter.
                logger.exception("[hookdeck] Maintenance tick failed")

    async def _stop_maintenance(self) -> None:
        if self._maintenance is None:
            return
        self._maintenance.cancel()
        try:
            await self._maintenance
        except asyncio.CancelledError:
            pass
        self._maintenance = None

    async def _resume_due_connections(self) -> int:
        """Unpause anything whose scheduled pause has expired.

        The agent's pause tool records a deadline rather than holding a timer,
        precisely so a restart cannot lose it. This is the other half.
        """
        if self._ledger is None or self._api is None:
            return 0

        resumed = 0
        for row in self._ledger.due_resumes():
            try:
                await self._api.unpause_connection(row["connection_id"])
                self._ledger.cancel_scheduled_resume(row["connection_id"])
                resumed += 1
                logger.info(
                    "[hookdeck] Auto-resumed %s — its scheduled pause expired",
                    row["name"] or row["connection_id"],
                )
            except HookdeckAPIError as exc:
                logger.error(
                    "[hookdeck] Could not auto-resume %s: %s. Resume it with "
                    "`hermes hookdeck resume %s`.",
                    row["name"] or row["connection_id"],
                    exc,
                    row["name"] or row["connection_id"],
                )
        return resumed

    async def _stop_tunnels(self) -> None:
        for tunnel in self._tunnels:
            await tunnel.stop()
        self._tunnels = []

    async def _abandon_connect(self) -> None:
        """Undo a partial connect, so a failed start leaks nothing.

        The gateway may retry ``connect``; a leaked SQLite handle or HTTP
        client per attempt is how a retry loop becomes a resource leak.
        """
        await self._teardown_server()
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None

    async def _teardown_server(self) -> None:
        if self._site_runner is not None:
            await self._site_runner.cleanup()
            self._site_runner = None

    async def _recover_orphaned_runs(self) -> int:
        """Ask Hookdeck to redeliver runs the previous process died holding.

        A ledger row still marked ``running`` at startup is an orphan by
        definition: the process that owned it is gone, and because the adapter
        acked 202 the moment it admitted the event, Hookdeck considers that
        delivery successful and will never bring it back on its own.

        This is the second half of what makes Hookdeck the durable work queue
        and lets the plugin skip owning a local one — the first half being the
        failed-run retry. Both rest on a successful event still being
        retryable. It is also why terminal rows are pruned on a TTL and
        ``running`` rows never are: pruning one would silently drop the work.

        Recovery re-runs an event whose run may in fact have completed in the
        instant before the crash, which is the at-least-once contract the whole
        design already assumes. Set ``recover_on_boot: false`` if that is wrong
        for your routes.
        """
        if not self.settings.recover_on_boot or not (self._ledger and self._api):
            return 0

        orphans = self._ledger.all_running()
        if not orphans:
            return 0

        recovered = 0
        for row in orphans:
            event_id = row["event_id"]
            if int(row["agent_attempts"]) - 1 >= self.settings.max_agent_retries:
                self._ledger.mark_exhausted(event_id, "interrupted; retry budget spent")
                continue
            self._ledger.mark_failed(event_id, "gateway stopped mid-run")
            try:
                await self._api.retry_event(event_id)
                recovered += 1
            except HookdeckAPIError as exc:
                # Degrade, never abort: the API being briefly unreachable at
                # boot is no reason to refuse to start, when the tunnel and the
                # queue are fine. The row stays `failed` and the next restart
                # tries again.
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

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "mode": self.settings.mode,
                "ack_mode": self.settings.ack_mode,
                "in_flight": len(self._inflight),
                "max_concurrent": self.settings.max_concurrent,
                "deliveries": self._ledger.counts() if self._ledger else {},
            }
        )

    async def _handle_delivery(self, request: web.Request) -> web.Response:
        """One Hookdeck delivery, from bytes on the wire to a dispatched run.

        Each step either produces a response and stops, or hands the delivery
        to the next. The order matters and is the security-relevant part: the
        signature is checked before anything reads the payload, and admission
        control runs before the ledger records anything, so a deferred event is
        not mistaken for a duplicate when Hookdeck brings it back.
        """
        self._sweep_inflight()

        raw_body, response = await self._read_verified_body(request)
        if response is not None:
            return response

        delivery, response = self._parse_delivery(request, raw_body)
        if response is not None:
            return response

        response = await self._reject_if_filtered(request, delivery)
        if response is not None:
            return response

        delivery.prompt = self._render_prompt(
            delivery.route.get("prompt", ""),
            delivery.payload,
            delivery.event_type,
            delivery.route_name,
        )

        # Before anything acts on the delivery, and before capacity is
        # considered: a duplicate is a duplicate whether or not there is room
        # to run it, and deliver_only posts to a channel where a repeat is
        # visible to a human.
        already_handled = self._already_handled(delivery)
        if already_handled:
            return self._respond(
                {
                    "status": "duplicate",
                    "reason": already_handled,
                    "event_id": delivery.event_id,
                },
                status=200,
            )

        if delivery.route.get("deliver_only"):
            return await self._deliver_without_agent(delivery)

        response = self._admit(delivery)
        if response is not None:
            return response

        return await self._dispatch(delivery)

    async def _read_verified_body(
        self, request: web.Request
    ) -> tuple[bytes, Optional[web.Response]]:
        """Read the body within limits and verify it, before anything parses it."""
        too_large = self._respond({"error": "Payload too large"}, status=413)

        if (request.content_length or 0) > self.settings.max_body_bytes:
            return b"", too_large
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return b"", too_large
        except Exception as exc:
            logger.error("[hookdeck] Failed to read body: %s", exc)
            return b"", self._respond({"error": "Bad request"}, status=400)
        if len(raw_body) > self.settings.max_body_bytes:
            return b"", too_large

        if self.settings.verifies_signatures and not self._signature_valid(
            request, raw_body
        ):
            logger.warning(
                "[hookdeck] Rejected delivery with an invalid signature (source=%s)",
                self._header(request, SOURCE_NAME) or "unknown",
            )
            return b"", self._respond({"error": "Invalid signature"}, status=401)

        return raw_body, None

    def _signature_valid(self, request: web.Request, raw_body: bytes) -> bool:
        from .verify import verify_signature

        return verify_signature(
            request.headers,
            raw_body,
            self.settings.signing_secret,
            prefix=self.settings.header_prefix,
        )

    def _parse_delivery(
        self, request: web.Request, raw_body: bytes
    ) -> tuple[Delivery, Optional[web.Response]]:
        """Build a :class:`Delivery` from a verified request."""
        source_name = self._header(request, SOURCE_NAME)
        # No fallback. An id that did not come from Hookdeck is worse than
        # none: `_admit` has a deliberate branch for a delivery with no event
        # id, which warns loudly and names `header_prefix` as the likely cause,
        # and a substitute id silences exactly that warning while breaking both
        # things the id is for. Dedup keyed on a value Hookdeck did not mint is
        # dedup on the wrong thing, and `POST /events/{id}/retry` with it 404s,
        # so the failed run is never handed back.
        event_id = self._header(request, EVENT_ID)
        try:
            attempt = int(self._header(request, ATTEMPT_COUNT) or 0)
        except ValueError:
            attempt = 0

        route_name, route = routing.resolve(
            self._routes,
            path_tail=request.match_info.get("route_tail", ""),
            source_name=source_name,
        )
        if route is None:
            logger.warning(
                "[hookdeck] No route for delivery (source=%s, path=%s)",
                source_name or "unknown",
                request.path,
            )
            return _no_delivery(), self._respond(
                {"error": f"No route matches source '{source_name}'"}, status=404
            )

        try:
            body_text = payload_mod.decode(raw_body)
            parsed = payload_mod.parse(body_text)
        except payload_mod.UndecodablePayload:
            return _no_delivery(), self._reject_unparseable(event_id)

        surrogates_replaced = payload_mod.has_lone_surrogates(parsed)
        if surrogates_replaced:
            # Reachable even after a strict decode: a lone surrogate written as
            # a JSON escape is pure ASCII on the wire.
            parsed = payload_mod.replace_lone_surrogates(parsed)
            logger.warning(
                "[hookdeck] Event %s contains unpaired surrogates; replaced "
                "them with U+FFFD so the payload can be encoded",
                event_id or "(no id)",
            )

        delivery = Delivery(
            event_id=event_id,
            attempt=attempt,
            source_name=source_name,
            route_name=route_name,
            route=route,
            payload=parsed,
            event_type=routing.event_type(
                parsed, route=route, headers=request.headers
            ),
            # Hookdeck omits this header on the final automatic attempt, which
            # is the cleanest dead-letter signal available.
            is_last_attempt=not self._header(request, WILL_RETRY_AFTER).strip(),
            operator_initiated=self._header(request, ATTEMPT_TRIGGER).strip().upper()
            in OPERATOR_TRIGGERS,
            surrogates_replaced=surrogates_replaced,
        )
        # Attempt-scoped, not just event-scoped. A redelivery arriving while
        # the previous run is still going — possible once the sweep releases a
        # timed-out slot — would otherwise reuse the same identity, overwrite
        # the in-flight entry, and have the two runs record each other's
        # outcomes.
        delivery.session_chat_id = (
            f"hookdeck:{route_name}:{event_id or int(time.time() * 1000)}"
            f":{attempt}"
        )
        return delivery, None

    async def _reject_if_filtered(
        self, request: web.Request, delivery: Delivery
    ) -> Optional[web.Response]:
        """Apply the route's own filters. Ignored events answer 200, not an error."""
        route = delivery.route

        if route.get("enabled", True) is False:
            return self._ignored(delivery, "route disabled")

        allowed = route.get("events", [])
        if allowed and delivery.event_type not in allowed:
            return self._ignored(delivery, "event type")

        if not self._route_processor.route_filters_match(
            route, delivery.payload, delivery.event_type, request.headers
        ):
            return self._ignored(delivery, "filter")

        if route.get("script"):
            keep, transformed = await asyncio.to_thread(
                self._route_processor.run_route_script, route["script"], delivery.payload
            )
            if not keep:
                return self._ignored(delivery, "script")
            delivery.payload = transformed or delivery.payload

        return None

    async def _deliver_without_agent(self, delivery: Delivery) -> web.Response:
        """``deliver_only``: render the template and push it, no run, no slot."""
        target = {
            "deliver": delivery.route.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                delivery.route.get("deliver_extra", {}), delivery.payload
            ),
        }
        try:
            result = await self._direct_deliver(delivery.prompt, target)
        except Exception:
            logger.exception(
                "[hookdeck] direct-deliver failed route=%s event=%s",
                delivery.route_name,
                delivery.label,
            )
            result = None

        if result is None or not result.success:
            # 5xx so Hookdeck retries on its own schedule rather than recording
            # the event as delivered.
            return self._respond({"status": "error", "error": "Delivery failed"}, status=502)

        # Recorded so a redelivery is recognised as a duplicate rather than
        # posting to the channel twice. Hookdeck's own dedupe rule only covers
        # a short window, and its signature carries no timestamp, so this is
        # the only replay protection these routes get.
        if delivery.event_id and self._ledger is not None:
            self._ledger.admit(
                delivery.event_id,
                route=delivery.route_name,
                attempt=delivery.attempt,
                session_chat_id=delivery.session_chat_id,
            )
            self._ledger.mark_succeeded(delivery.event_id)

        return self._respond(
            {
                "status": "delivered",
                "route": delivery.route_name,
                "target": target["deliver"],
                "event_id": delivery.event_id,
            }
        )

    def _already_handled(self, delivery: Delivery) -> Optional[str]:
        """Why this delivery needs no run, if it needs none.

        Read-only on purpose. Answering this *before* the capacity check is
        what stops a repeat arriving at a busy moment being deferred instead:
        Hookdeck would redeliver it with a higher attempt number, which then
        reads as new work and runs the agent a second time.
        """
        if not delivery.event_id or self._ledger is None:
            return None
        reason = self._ledger.rejection_reason(
            delivery.event_id,
            delivery.attempt,
            operator_initiated=delivery.operator_initiated,
        )
        if reason:
            logger.info("[hookdeck] Skipping %s: %s", delivery.event_id, reason)
        return reason

    def _admit(self, delivery: Delivery) -> Optional[web.Response]:
        """Claim a slot and a ledger entry, or defer.

        Nothing is recorded for a deferred event: a ledger entry would make
        Hookdeck's redelivery look like a duplicate, and the event would vanish.
        """
        at_capacity = (
            self.settings.max_concurrent
            and len(self._inflight) >= self.settings.max_concurrent
        )
        if at_capacity:
            return self._defer(delivery)

        if not delivery.event_id:
            # No id means no dedup and no outcome reporting: a redelivery would
            # run the agent twice, and a failure could not be handed back. Say
            # so rather than degrading quietly — it means the delivery did not
            # come from Hookdeck, or the project customised the header prefix.
            logger.warning(
                "[hookdeck] Delivery on route %s carries no %s header — "
                "processing it without deduplication or retry. Check "
                "platforms.hookdeck.extra.header_prefix if your project "
                "customised the x-hookdeck prefix.",
                delivery.route_name,
                header_name(EVENT_ID, self.settings.header_prefix),
            )
            return None

        if self._ledger is None:
            return None

        admission = self._ledger.admit(
            delivery.event_id,
            route=delivery.route_name,
            attempt=delivery.attempt,
            session_chat_id=delivery.session_chat_id,
            operator_initiated=delivery.operator_initiated,
        )
        if not admission.admitted:
            logger.info("[hookdeck] Skipping %s: %s", delivery.event_id, admission.reason)
            return self._respond(
                {"status": "duplicate", "event_id": delivery.event_id}, status=200
            )
        return None

    def _defer(self, delivery: Delivery) -> web.Response:
        """503 the delivery so Hookdeck keeps it queued and comes back.

        ``Retry-After`` overrides the connection's retry rule outright, so a
        fixed short interval spends the whole automatic budget at that
        interval: 50 attempts at 30s is 25 minutes, then the event is gone. A
        short value is right *because* capacity normally frees in seconds — so
        it is sent only while that premise holds. Past ``defer_attempt_limit``
        deferrals of one event the saturation is evidently not transient, and
        the 503 goes bare so exponential backoff spreads what budget remains.
        """
        logger.info(
            "[hookdeck] At capacity (%d runs in flight) — deferring event %s",
            len(self._inflight),
            delivery.label,
        )
        headers = {}
        if delivery.attempt <= self.settings.defer_attempt_limit:
            headers["Retry-After"] = str(self.settings.retry_after_seconds)
        else:
            logger.warning(
                "[hookdeck] Event %s deferred %s times — capacity is not "
                "recovering; dropping Retry-After so Hookdeck backs off "
                "exponentially instead of burning its retry budget",
                delivery.label,
                delivery.attempt,
            )
        return self._respond(
            {
                "status": "deferred",
                "reason": "max_concurrent",
                "in_flight": len(self._inflight),
            },
            status=503,
            headers=headers,
        )

    async def _dispatch(self, delivery: Delivery) -> web.Response:
        """Hand the event to the agent, and answer according to ``ack_mode``."""
        delivery.prompt = self._apply_skills(delivery.route, delivery.prompt)
        self._remember_delivery_target(delivery)

        event = MessageEvent(
            text=delivery.prompt,
            message_type=MessageType.TEXT,
            source=self.build_source(
                chat_id=delivery.session_chat_id,
                chat_name=f"hookdeck/{delivery.route_name}",
                chat_type="webhook",
                user_id=f"hookdeck:{delivery.route_name}",
                user_name=delivery.route_name,
            ),
            raw_message=delivery.payload,
            message_id=delivery.event_id or None,
            metadata={
                "hookdeck_event_id": delivery.event_id,
                "hookdeck_source": delivery.source_name,
                "hookdeck_attempt": delivery.attempt,
                "hookdeck_route": delivery.route_name,
                "hookdeck_last_automatic_attempt": delivery.is_last_attempt,
                "hookdeck_surrogates_replaced": delivery.surrogates_replaced,
            },
        )

        self._inflight[delivery.session_chat_id] = InFlightRun(
            event_id=delivery.event_id,
            route=delivery.route_name,
            started=time.time(),
            is_last_attempt=delivery.is_last_attempt,
            session_chat_id=delivery.session_chat_id,
        )
        logger.info(
            "[hookdeck] dispatch route=%s event_type=%s event_id=%s attempt=%s "
            "prompt_len=%d",
            delivery.route_name,
            delivery.event_type,
            delivery.event_id or "(none)",
            delivery.attempt or "?",
            len(delivery.prompt),
        )

        if self.settings.ack_mode == "sync":
            return await self._dispatch_and_wait(event, delivery)

        self._spawn(self.handle_message(event))
        return self._respond(
            {
                "status": "accepted",
                "route": delivery.route_name,
                "event": delivery.event_type,
                "event_id": delivery.event_id,
            },
            status=202,
        )

    async def _dispatch_and_wait(
        self, event: MessageEvent, delivery: Delivery
    ) -> web.Response:
        """``sync`` mode: hold the response until the run finishes, or time out.

        ``handle_message`` is fire-and-forget by design, so "synchronous" means
        waiting on the completion hook rather than awaiting the dispatch call.
        On timeout the response degrades to 202: the run is still going, and
        answering 5xx would have Hookdeck redeliver work that is in flight.
        """
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[delivery.session_chat_id] = waiter
        self._spawn(self.handle_message(event))

        try:
            outcome = await asyncio.wait_for(
                waiter, timeout=self.settings.sync_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.info(
                "[hookdeck] Run for %s still going after %.0fs — acking 202",
                delivery.session_chat_id,
                self.settings.sync_timeout_seconds,
            )
            if delivery.event_id:
                self._acked_before_completion.add(delivery.event_id)
            return self._respond(
                {
                    "status": "accepted",
                    "reason": "still running",
                    "route": delivery.route_name,
                },
                status=202,
            )
        finally:
            self._waiters.pop(delivery.session_chat_id, None)

        if outcome == ProcessingOutcome.SUCCESS:
            return self._respond({"status": "processed", "route": delivery.route_name})
        # 5xx puts the event back on Hookdeck's retry schedule.
        return self._respond(
            {
                "status": "failed",
                "route": delivery.route_name,
                "outcome": str(outcome),
            },
            status=500,
        )

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    async def on_processing_complete(self, event: MessageEvent, outcome: Any) -> None:
        """Record the run's true outcome, then let the base class close the session.

        This is the seam the built-in adapter leaves unused for reliability. It
        fires at the real end of a run — success, failure and cancellation
        alike — which is the only point at which the event's fate is known.
        """
        chat_id = getattr(getattr(event, "source", None), "chat_id", "") or ""
        run = self._inflight.pop(chat_id, None)

        waiter = self._waiters.get(chat_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)

        if run and run.event_id and self._ledger is not None:
            try:
                await self._record_outcome(
                    run.event_id,
                    outcome,
                    last_attempt=run.is_last_attempt,
                    session_chat_id=run.session_chat_id,
                )
            except Exception:
                logger.exception(
                    "[hookdeck] Failed to record outcome for event %s", run.event_id
                )

        try:
            await super().on_processing_complete(event, outcome)
        except Exception:
            logger.debug("[hookdeck] Base completion hook failed", exc_info=True)

    async def _record_outcome(
        self,
        event_id: str,
        outcome: Any,
        *,
        last_attempt: bool = False,
        session_chat_id: str = "",
    ) -> None:
        assert self._ledger is not None
        if outcome == ProcessingOutcome.SUCCESS:
            self._ledger.mark_succeeded(event_id, session_chat_id=session_chat_id)
            self._acked_before_completion.discard(event_id)
            return

        runs = self._ledger.agent_attempts(event_id)
        reason = str(getattr(outcome, "value", outcome))

        # `runs` counts runs, so retries already requested is one less:
        # max_agent_retries=2 means the first run plus two more.
        if runs - 1 >= self.settings.max_agent_retries:
            self._ledger.mark_exhausted(
                event_id, reason, session_chat_id=session_chat_id
            )
            logger.error(
                "[hookdeck] Event %s failed %d times — giving up. Inspect it in "
                "Hookdeck and replay with `hermes hookdeck replay %s`.",
                event_id,
                runs,
                event_id,
            )
            return

        if last_attempt:
            # Hookdeck's automatic retries are spent for this event, so nothing
            # will bring it back on its own. The request below is a manual
            # retry, which is unlimited — but this is the dead-letter moment
            # and deserves saying out loud.
            logger.warning(
                "[hookdeck] Event %s failed on its last automatic attempt — "
                "requesting a manual redelivery; if that fails too, only "
                "`hermes hookdeck replay %s` will bring it back",
                event_id,
                event_id,
            )

        self._ledger.mark_failed(event_id, reason, session_chat_id=session_chat_id)
        if not self._should_hand_back(event_id):
            return
        if await self._request_redelivery(event_id):
            logger.info(
                "[hookdeck] Agent run failed for event %s (run %d of %d) — "
                "asked Hookdeck to redeliver",
                event_id,
                runs,
                self.settings.max_agent_retries + 1,
            )

    def _should_hand_back(self, event_id: str) -> bool:
        """Whether this failure needs an explicit redelivery request.

        In ``sync`` mode the 5xx response is the request, so Hookdeck's own
        rules take over — unless the run outlasted ``sync_timeout_seconds`` and
        already degraded to 202, at which point Hookdeck believes the delivery
        succeeded and this *is* the async situation.
        """
        if self._api is None:
            return False
        if self.settings.ack_mode == "async_retry":
            return True
        return event_id in self._acked_before_completion

    async def _request_redelivery(self, event_id: str) -> bool:
        """Ask Hookdeck to redeliver, retrying a transient API failure.

        This is the only thing standing between a failed run and a lost event:
        the 202 has already gone, so nothing else will bring it back. A single
        un-retried call would strand the event on any network blip.
        """
        assert self._api is not None
        delay = _REDELIVERY_RETRY_INITIAL_SECONDS
        last: Optional[HookdeckAPIError] = None
        for attempt in range(1, _REDELIVERY_ATTEMPTS + 1):
            try:
                await self._api.retry_event(event_id)
                self._acked_before_completion.discard(event_id)
                return True
            except HookdeckAPIError as exc:
                last = exc
                if attempt == _REDELIVERY_ATTEMPTS:
                    break  # nothing left to wait for
                await asyncio.sleep(delay)
                delay *= 2

        logger.error(
            "[hookdeck] Could not hand event %s back to Hookdeck after %d "
            "attempts (%s). It is marked failed locally and will be picked up "
            "by boot recovery on the next restart; `hermes hookdeck replay %s` "
            "brings it back now.",
            event_id,
            _REDELIVERY_ATTEMPTS,
            last,
            event_id,
        )
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _respond(
        self,
        body: dict,
        *,
        status: int = 200,
        headers: Optional[dict] = None,
    ) -> web.Response:
        """Answer a delivery, refusing any status whose retryability is undeclared.

        Every status the adapter emits is listed in ``EMITTED_STATUS_RETRYABLE``
        alongside whether a redelivery of that event could succeed, and the
        connection's retry rule is derived from that list. Routing responses
        through here is what stops the two drifting apart.
        """
        return web.json_response(
            body, status=assert_declared_status(status), headers=headers
        )

    def _ignored(self, delivery: Delivery, reason: str) -> web.Response:
        """200 for an event the route deliberately does not want.

        Not an error: the delivery arrived correctly and was considered. A 4xx
        would have Hookdeck record a failure and, worse, retry it.
        """
        logger.debug(
            "[hookdeck] Ignoring %s on route %s (%s)",
            delivery.event_type,
            delivery.route_name,
            reason,
        )
        return self._respond(
            {
                "status": "ignored",
                "reason": reason,
                "route": delivery.route_name,
                "event": delivery.event_type,
            }
        )

    def _reject_unparseable(self, event_id: str) -> web.Response:
        """400 for a body that no retry can fix.

        ``Retry-After: -1`` asks Hookdeck to cancel the remaining automatic
        retries, turning 50 doomed attempts into one. It is off by default
        because the failure mode is severe and silent: an over-strict parser
        would discard live traffic, unrecoverable once retention lapses. Turn
        it on only after watching the counter in ``hermes hookdeck status``.
        """
        cancelling = self.settings.cancel_retries_on_unparseable
        headers = {"Retry-After": "-1"} if cancelling else {}

        if self._ledger is not None and event_id:
            # Recorded either way. With the flag off this is the counter the
            # advice above refers to: `hermes hookdeck status` shows how often
            # cancellation *would* have fired, which is the only way to judge
            # whether turning it on is safe.
            self._ledger.record_cancelled(
                event_id, "unparseable body", cancelled=cancelling
            )

        logger.warning(
            "[hookdeck] Event %s has an unparseable body%s",
            event_id or "(no id)",
            " — cancelling its automatic retries" if cancelling else "",
        )
        return self._respond({"error": "Cannot parse body"}, status=400, headers=headers)

    def _header(self, request: web.Request, suffix: str) -> str:
        return request.headers.get(
            header_name(suffix, self.settings.header_prefix), ""
        )

    def _remember_delivery_target(self, delivery: Delivery) -> None:
        """Record where this run's response goes, for the inherited ``send()``."""
        now = time.time()
        self._delivery_info[delivery.session_chat_id] = {
            "deliver": delivery.route.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                delivery.route.get("deliver_extra", {}), delivery.payload
            ),
        }
        self._delivery_info_created[delivery.session_chat_id] = now
        self._delivery_info_order.append((now, delivery.session_chat_id))
        self._prune_delivery_info(now)

    def _spawn(self, coro) -> None:
        """Run *coro* detached, keeping a reference so it is not GC'd mid-flight."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _apply_skills(self, route: dict, prompt: str) -> str:
        """Replace the prompt with the first configured skill's invocation."""
        skills = route.get("skills", [])
        if not skills:
            return prompt
        try:
            from agent.skill_commands import (
                build_skill_invocation_message,
                get_skill_commands,
            )

            available = get_skill_commands()
            for skill_name in skills:
                command = f"/{skill_name}"
                if command not in available:
                    logger.warning("[hookdeck] Skill '%s' not found", skill_name)
                    continue
                content = build_skill_invocation_message(command, user_instruction=prompt)
                if content:
                    return content
        except Exception as exc:
            logger.warning("[hookdeck] Skill loading failed: %s", exc)
        return prompt

    def _sweep_inflight(self, *, force: bool = False) -> None:
        """Release slots whose completion hook never fired.

        ``on_processing_complete`` runs on the success, failure and cancellation
        paths, so this should find nothing — but a slot leaked by an unexpected
        crash would wedge admission control at zero capacity, which is worse
        than the small chance of releasing a slot early.

        Called both from the request path (rate-limited, so a busy gateway
        reclaims slots promptly without sweeping on every delivery) and from
        the maintenance tick, which is what covers a gateway with no traffic.
        """
        now = time.time()
        if not force and now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now

        for chat_id, run in list(self._inflight.items()):
            if now - run.started <= self.settings.run_timeout_seconds:
                continue
            logger.warning(
                "[hookdeck] Run for %s exceeded %.0fs with no completion signal "
                "— releasing its slot and handing the event back",
                chat_id,
                self.settings.run_timeout_seconds,
            )
            self._inflight.pop(chat_id, None)
            # Leaving the ledger row `running` would strand the event until the
            # next restart, since boot recovery is the only other reader. Treat
            # it as a failure now, which routes it through the retry budget.
            if run.event_id:
                self._spawn(
                    self._record_outcome(
                        run.event_id,
                        ProcessingOutcome.FAILURE,
                        session_chat_id=run.session_chat_id,
                    )
                )

        if self._ledger is not None:
            self._ledger.prune(self.settings.ledger_ttl_seconds)

    # Kept for tests and callers that predate `settings`.
    def _validate_startup(self) -> None:
        self.settings.validate()

    def _bind_hosts(self) -> list[Optional[str]]:
        return self.settings.bind_hosts

    def _tunnel_plan(self) -> dict[str, str]:
        return self.settings.tunnels


def _no_delivery() -> Delivery:
    """Placeholder for the error paths, which return a response instead."""
    return Delivery(
        event_id="",
        attempt=0,
        source_name="",
        route_name="",
        route={},
        payload=None,
        event_type="",
        is_last_attempt=False,
    )


# ----------------------------------------------------------------------
# Registry hooks
# ----------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive dependency probe — must not install anything.

    Reports what the module-level guard found for aiohttp, so a missing extra
    surfaces as "platform not ready, here is the install hint" rather than as
    an absent platform. httpx is probed lazily since it is only needed once the
    adapter runs.
    """
    from importlib.util import find_spec

    return AIOHTTP_AVAILABLE and find_spec("httpx") is not None


def validate_config(config: PlatformConfig) -> bool:
    settings = AdapterSettings.from_extra(getattr(config, "extra", None))
    return bool(settings.signing_secret and settings.routes)


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from the environment.

    Lets ``hermes gateway status`` report an env-only setup without
    constructing the adapter.
    """
    import os

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
        if not value:
            continue
        try:
            seeded[key] = cast(value)
        except ValueError:
            logger.warning("[hookdeck] Ignoring invalid %s=%r", env_var, value)
    return seeded
