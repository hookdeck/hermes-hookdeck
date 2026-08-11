"""Every knob the adapter has, resolved once and checked before it starts.

Config arrives as an untyped ``dict`` from ``config.yaml`` with environment
variables layered on top. Collecting that into one frozen object keeps the
coercion in a single place and lets :meth:`AdapterSettings.validate` refuse a
configuration at startup rather than at the first delivery — a gateway that
declines to boot is far easier to diagnose than one that quietly rejects
traffic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .constants import (
    ACK_MODES,
    DEFAULT_ACK_MODE,
    DEFAULT_DEFER_ATTEMPT_LIMIT,
    DEFAULT_HEADER_PREFIX,
    DEFAULT_LEDGER_TTL_SECONDS,
    DEFAULT_MAX_AGENT_RETRIES,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEFAULT_RETRY_AFTER_SECONDS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    DEFAULT_SYNC_TIMEOUT_SECONDS,
    INSECURE_NO_AUTH,
)
from .routing import tunnel_plan
from .ledger import default_state_path

MODES = ("cli", "push")


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def load_hermes_config() -> dict:
    """Parse ``config.yaml``, or an empty dict if it cannot be read.

    Never raises: every caller is a diagnostic or an operator command that is
    more useful degraded than absent.
    """
    path = hermes_home() / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print(f"! Cannot read {path}: PyYAML is not installed")
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced, not raised
        print(f"! Cannot parse {path}: {exc}")
        return {}


def platform_extra(config: Optional[Mapping[str, Any]] = None) -> dict:
    """``gateway.platforms.hookdeck.extra`` from a parsed config."""
    parsed = load_hermes_config() if config is None else config
    gateway = parsed.get("gateway") or {}
    platforms = gateway.get("platforms") or parsed.get("platforms") or {}
    return ((platforms.get("hookdeck") or {}).get("extra")) or {}


def configured_state_path() -> Path:
    """The ledger the *running adapter* uses, honouring an explicit override.

    The adapter, the CLI, the dashboard and the agent tools all read and write
    this file. Resolving it differently in any one of them means writing to a
    database nobody else reads — which looks exactly like the feature silently
    not working.
    """
    configured = platform_extra().get("state_path")
    return Path(configured).expanduser() if configured else default_state_path()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback(host: Optional[str]) -> bool:
    return bool(host) and host in LOOPBACK_HOSTS


@dataclass(frozen=True)
class AdapterSettings:
    """Resolved configuration for one Hookdeck platform instance."""

    routes: dict[str, dict] = field(default_factory=dict)

    # ── Transport ──────────────────────────────────────────────────
    mode: str = "cli"
    host: Optional[str] = None
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    source: str = ""

    # ── Verification ───────────────────────────────────────────────
    signing_secret: str = ""
    header_prefix: str = DEFAULT_HEADER_PREFIX

    # ── Run semantics ──────────────────────────────────────────────
    ack_mode: str = DEFAULT_ACK_MODE
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_agent_retries: int = DEFAULT_MAX_AGENT_RETRIES
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    defer_attempt_limit: int = DEFAULT_DEFER_ATTEMPT_LIMIT
    sync_timeout_seconds: float = DEFAULT_SYNC_TIMEOUT_SECONDS
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS
    recover_on_boot: bool = True
    cancel_retries_on_unparseable: bool = False

    # ── Storage and tooling ────────────────────────────────────────
    state_path: Path = field(default_factory=default_state_path)
    ledger_ttl_seconds: float = DEFAULT_LEDGER_TTL_SECONDS
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    cli_binary: str = "hookdeck"
    cli_login: bool = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_extra(cls, extra: Optional[Mapping[str, Any]]) -> "AdapterSettings":
        """Build settings from ``platforms.hookdeck.extra``, with env fallbacks.

        Environment variables are a fallback rather than an override, so a
        value written in ``config.yaml`` is the one that takes effect — the
        opposite would make a stray shell export silently outrank the file an
        operator is looking at.
        """
        extra = extra or {}

        def text(key: str, env: str = "", default: str = "") -> str:
            return str(extra.get(key) or (os.getenv(env) if env else "") or default)

        mode = text("mode", "HOOKDECK_MODE", "cli").lower()

        return cls(
            routes=dict(extra.get("routes") or {}),
            mode=mode,
            # cli mode is loopback-only by construction: the CLI is the only
            # thing that should be able to reach the listener.
            host="127.0.0.1" if mode == "cli" else (extra.get("host") or None),
            port=int(extra.get("port") or os.getenv("HOOKDECK_PORT") or DEFAULT_PORT),
            path="/" + text("path", "HOOKDECK_PATH", DEFAULT_PATH).strip("/"),
            source=text("source", "HOOKDECK_SOURCE"),
            signing_secret=text("secret", "HOOKDECK_WEBHOOK_SECRET"),
            header_prefix=text("header_prefix", default=DEFAULT_HEADER_PREFIX),
            ack_mode=text("ack_mode", default=DEFAULT_ACK_MODE).lower(),
            max_concurrent=int(extra.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
            max_agent_retries=int(
                extra.get("max_agent_retries", DEFAULT_MAX_AGENT_RETRIES)
            ),
            retry_after_seconds=int(
                extra.get("retry_after_seconds", DEFAULT_RETRY_AFTER_SECONDS)
            ),
            defer_attempt_limit=int(
                extra.get("defer_attempt_limit", DEFAULT_DEFER_ATTEMPT_LIMIT)
            ),
            sync_timeout_seconds=float(
                extra.get("sync_timeout_seconds", DEFAULT_SYNC_TIMEOUT_SECONDS)
            ),
            run_timeout_seconds=float(
                extra.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
            ),
            recover_on_boot=bool(extra.get("recover_on_boot", True)),
            cancel_retries_on_unparseable=bool(
                extra.get("cancel_retries_on_unparseable", False)
            ),
            state_path=Path(
                extra.get("state_path") or default_state_path()
            ).expanduser(),
            ledger_ttl_seconds=float(
                extra.get("ledger_ttl_seconds", DEFAULT_LEDGER_TTL_SECONDS)
            ),
            max_body_bytes=int(extra.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)),
            # An npm global shadowing a Homebrew install is the common case,
            # and PATH silently picks the older one.
            cli_binary=text("cli_binary", default="hookdeck"),
            # Off by default: `hookdeck ci` rewrites the shared CLI config and
            # repoints its active project — not something starting a gateway
            # should do to a tool the operator uses for other work.
            cli_login=bool(extra.get("cli_login", False)),
        )

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------

    @property
    def verifies_signatures(self) -> bool:
        return self.signing_secret not in ("", INSECURE_NO_AUTH)

    @property
    def bind_hosts(self) -> list[Optional[str]]:
        """Addresses to listen on.

        In cli mode that is *both* loopback families. The Hookdeck CLI forwards
        to ``http://localhost:<port>``, which resolves to ``::1`` first on a
        dual-stack machine — against an IPv4-only listener every delivery fails
        with ECONNREFUSED while the tunnel reports itself perfectly healthy.
        Binding a wildcard would also fix it, by exposing an agent-dispatch
        endpoint to the network, so each loopback address is bound instead.
        """
        return ["127.0.0.1", "::1"] if self.mode == "cli" else [self.host]

    @property
    def tunnels(self) -> dict[str, str]:
        """Route name -> Hookdeck source, one per ``hookdeck listen`` process."""
        return tunnel_plan(self.routes, self.source)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Raise ``ValueError`` if this configuration cannot safely run."""
        if self.mode not in MODES:
            raise ValueError(
                f"[hookdeck] Unknown mode {self.mode!r}. Expected one of: "
                f"{', '.join(MODES)}."
            )
        if self.ack_mode not in ACK_MODES:
            raise ValueError(
                f"[hookdeck] Unknown ack_mode {self.ack_mode!r}. Expected one "
                f"of: {', '.join(ACK_MODES)}."
            )
        if not self.signing_secret:
            raise ValueError(
                "[hookdeck] No signing secret. Set HOOKDECK_WEBHOOK_SECRET (or "
                "platforms.hookdeck.extra.secret) to the signing secret from "
                "your Hookdeck project settings. For local testing only, set "
                f"it to '{INSECURE_NO_AUTH}' while bound to loopback."
            )
        if self.signing_secret == INSECURE_NO_AUTH and not is_loopback(self.host):
            raise ValueError(
                f"[hookdeck] {INSECURE_NO_AUTH} is set but the listener is "
                f"bound to non-loopback host {self.host!r}. Refusing to start: "
                "that would expose an unauthenticated agent-dispatch endpoint."
            )

        for name, route in self.routes.items():
            if not route.get("deliver_only"):
                continue
            target = route.get("deliver", "log")
            if not target or target == "log":
                raise ValueError(
                    f"[hookdeck] Route '{name}' sets deliver_only but deliver "
                    f"is '{target}'. Direct delivery needs a real target "
                    "(telegram, slack, github_comment, …)."
                )

        if self.mode == "cli" and not self.tunnels:
            raise ValueError(
                "[hookdeck] cli mode needs a Hookdeck source to listen to, and "
                "no route declares one. Set `source` on each route, or "
                "platforms.hookdeck.extra.source for a single-route setup."
            )
