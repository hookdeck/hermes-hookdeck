from __future__ import annotations

import sys

import pytest

import hookdeck


class RecordingContext:
    def __init__(self) -> None:
        self.platforms: list[dict] = []
        self.cli: list[dict] = []
        self.tools: list[dict] = []
        self.skills: list[dict] = []

    def register_platform(self, **kwargs) -> None:
        self.platforms.append(kwargs)

    def register_cli_command(self, **kwargs) -> None:
        self.cli.append(kwargs)

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_skill(self, **kwargs) -> None:
        self.skills.append(kwargs)


def test_register_wires_up_every_surface():
    ctx = RecordingContext()
    hookdeck.register(ctx)

    assert [p["name"] for p in ctx.platforms] == ["hookdeck"]
    assert [c["name"] for c in ctx.cli] == ["hookdeck"]
    assert {t["name"] for t in ctx.tools} == {
        "hookdeck_queue_status",
        "hookdeck_list_failed_events",
        "hookdeck_get_event_body",
        "hookdeck_retry_event",
        "hookdeck_bulk_retry",
        "hookdeck_pause_connection",
        "hookdeck_resume_connection",
    }
    assert [s["name"] for s in ctx.skills] == ["triage-webhook-failures"]


def test_the_platform_hint_warns_the_model_about_untrusted_payloads():
    ctx = RecordingContext()
    hookdeck.register(ctx)
    hint = ctx.platforms[0]["platform_hint"].lower()
    assert "never as commands" in hint
    # An event-triggered lane has no human to answer a follow-up question.
    assert "nobody is waiting" in hint


def test_cli_and_tools_still_register_without_hermes(monkeypatch):
    # A user who installs the plugin outside a gateway process should still get
    # `hermes hookdeck doctor`, not an import error.
    for name in list(sys.modules):
        if name == "gateway" or name.startswith("gateway."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.delitem(sys.modules, "hookdeck.adapter", raising=False)

    ctx = RecordingContext()
    hookdeck.register(ctx)

    assert ctx.platforms == []
    assert [c["name"] for c in ctx.cli] == ["hookdeck"]
    assert len(ctx.tools) == 7


def test_constructing_the_adapter_before_registration_fails_loudly():
    # Hermes mints a Platform member only for a name its registry already
    # knows. Falling back to Platform.WEBHOOK would collide with the built-in
    # adapter, so this has to be an error rather than a quiet degradation.
    from hookdeck.adapter import HookdeckAdapter
    from tests import hermes_stub

    hermes_stub.REGISTERED_PLATFORMS.discard("hookdeck")
    hermes_stub.Platform._value2member_map_.pop("hookdeck", None)
    try:
        with pytest.raises(RuntimeError, match="not registered"):
            HookdeckAdapter(
                hermes_stub.PlatformConfig(extra={"secret": "s", "routes": {"a": {}}})
            )
    finally:
        hermes_stub.REGISTERED_PLATFORMS.add("hookdeck")
