"""Minimal stand-ins for the Hermes internals the adapter imports.

The plugin lives outside the Hermes tree, so the adapter's tests need
``gateway.config`` and ``gateway.platforms.*`` to exist. These stubs implement
only the surface :mod:`hookdeck.adapter` actually touches, with the same
semantics as the real thing where it matters:

* ``handle_message`` is fire-and-forget, exactly as the real base class is —
  which is what makes the ``sync`` ack mode's completion-hook wait necessary
  rather than an affectation
* ``on_processing_complete`` is the hook that fires at the true end of a run

Anything inherited from the real ``WebhookAdapter`` (prompt templating, payload
filters, cross-platform delivery) is stubbed to something trivial: it is core
behaviour, not this plugin's, and testing the stub would prove nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Platform(Enum):
    WEBHOOK = "webhook"

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip().lower()
        if value in cls._value2member_map_:
            return cls._value2member_map_[value]
        member = object.__new__(cls)
        member._name_ = value.upper()
        member._value_ = value
        cls._value2member_map_[value] = member
        return member


@dataclass
class PlatformConfig:
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


class MessageType(Enum):
    TEXT = "text"


class ProcessingOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class SessionSource:
    platform: Any = None
    chat_id: str = ""
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    profile: Optional[str] = None


@dataclass
class MessageEvent:
    text: str
    message_type: Any = MessageType.TEXT
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class _RouteProcessor:
    """Stand-in for WebhookRouteProcessor: filters pass, scripts keep."""

    def route_filters_match(self, route, payload, event_type, headers) -> bool:
        predicate = route.get("_test_filter")
        return predicate(payload) if callable(predicate) else True

    def run_route_script(self, script, payload):
        return True, payload


class BasePlatformAdapter:
    def __init__(self, config: PlatformConfig, platform: Any):
        self.config = config
        self.platform = platform
        self.gateway_runner = None
        self._connected = False
        self._background_tasks: set = set()
        self._message_handler = None

    def _mark_connected(self) -> None:
        self._connected = True

    def _mark_disconnected(self) -> None:
        self._connected = False

    def build_source(self, chat_id: str, **kwargs) -> SessionSource:
        return SessionSource(platform=self.platform, chat_id=chat_id, **kwargs)

    async def handle_message(self, event: MessageEvent) -> None:
        """Fire-and-forget, like the real base class.

        Tests install ``self.run_agent`` to decide the outcome; the completion
        hook is invoked from a background task so nothing awaits the run.
        """

        async def _run() -> None:
            runner = getattr(self, "run_agent", None)
            outcome = ProcessingOutcome.SUCCESS
            if runner is not None:
                outcome = await runner(event)
            await self.on_processing_complete(event, outcome)

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def on_processing_complete(self, event, outcome) -> None:
        return None


class WebhookAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        extra = config.extra or {}
        self._host = extra.get("host") or None
        self._port = int(extra.get("port", 8787))
        self._routes: Dict[str, dict] = dict(extra.get("routes") or {})
        self._max_body_bytes = int(extra.get("max_body_bytes", 1_048_576))
        self._route_processor = _RouteProcessor()
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}
        self._delivery_info_order: deque = deque()
        self.direct_deliveries: list = []
        self.direct_deliver_result = SendResult(success=True)

    def _prune_delivery_info(self, now: float) -> None:
        return None

    def _render_prompt(self, template, payload, event_type, route_name) -> str:
        return template or json.dumps(payload)

    def _render_delivery_extra(self, extra, payload) -> dict:
        return dict(extra or {})

    async def _direct_deliver(self, content, delivery) -> SendResult:
        self.direct_deliveries.append((content, delivery))
        return self.direct_deliver_result


def install() -> None:
    """Insert the stub modules into ``sys.modules``."""
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []  # mark as a package
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig

    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    base_mod = types.ModuleType("gateway.platforms.base")
    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.ProcessingOutcome = ProcessingOutcome
    base_mod.SendResult = SendResult
    base_mod.SessionSource = SessionSource

    webhook_mod = types.ModuleType("gateway.platforms.webhook")
    webhook_mod.WebhookAdapter = WebhookAdapter

    sys.modules.update(
        {
            "gateway": gateway,
            "gateway.config": config_mod,
            "gateway.platforms": platforms,
            "gateway.platforms.base": base_mod,
            "gateway.platforms.webhook": webhook_mod,
        }
    )
