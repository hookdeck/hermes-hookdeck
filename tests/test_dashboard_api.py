"""The dashboard tab's backend.

Loaded by path the way the web server loads it, so the sys.path bootstrap at
the top of the module is exercised rather than bypassed by a package import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "hookdeck" / "dashboard" / "plugin_api.py"


@pytest.fixture(scope="module")
def plugin_api():
    spec = importlib.util.spec_from_file_location("hookdeck_dashboard_api", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_loads_the_way_the_web_server_loads_it(plugin_api):
    paths = {r.path for r in plugin_api.router.routes}
    assert paths == {
        "/overview",
        "/events/{event_id}/retry",
        "/events/{event_id}/body",
        "/connections/{connection_id}/pause",
        "/connections/{connection_id}/resume",
    }


async def test_only_this_gateways_connections_get_controls(plugin_api, monkeypatch):
    # The tab renders a Pause button next to every connection it is given. A
    # project's other connections are unrelated production traffic, so showing
    # them puts an outage one misclick away.
    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    monkeypatch.setattr(
        plugin_api,
        "_load_hermes_config",
        lambda: {"gateway": {"platforms": {"hookdeck": {"extra": {"routes": {"mine": {}}}}}}},
    )

    class FakeAPI:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def queue_depth(self, **_):
            return {"data": [{"metrics": {"max_depth": 3, "max_age": 1.5}}]}
        async def list_events(self, **_): return {"models": []}
        async def list_issues(self, **_): return {"models": []}
        async def list_connections(self, **_):
            return {"models": [
                {"id": "c1", "name": "mine", "paused_at": None},
                {"id": "c2", "name": "someone-elses-production", "paused_at": None},
                {"id": "c3", "name": "also-not-mine", "paused_at": "2026-01-01T00:00:00Z"},
            ]}

    monkeypatch.setattr(plugin_api, "HookdeckAPI", lambda *a, **k: FakeAPI())
    result = await plugin_api.overview()

    assert [c["name"] for c in result["hookdeck"]["connections"]] == ["mine"]
    # Surfaced rather than hidden, so the omission is explained rather than
    # looking like the project is empty.
    assert result["hookdeck"]["other_connection_count"] == 2


async def test_queue_metrics_are_flattened_for_display(plugin_api, monkeypatch):
    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    monkeypatch.setattr(plugin_api, "_load_hermes_config", lambda: {})

    class FakeAPI:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def queue_depth(self, **_):
            return {"data": [{"metrics": {"max_depth": 6, "max_age": 0.25}}], "metadata": {}}
        async def list_events(self, **_): return {"models": []}
        async def list_issues(self, **_): return {"models": []}
        async def list_connections(self, **_): return {"models": []}

    monkeypatch.setattr(plugin_api, "HookdeckAPI", lambda *a, **k: FakeAPI())
    depth = (await plugin_api.overview())["hookdeck"]["depth"]
    assert depth == {"max_depth": 6, "max_age_minutes": 0.25}


async def test_a_missing_api_key_is_reported_not_raised(plugin_api, monkeypatch):
    monkeypatch.delenv("HOOKDECK_API_KEY", raising=False)
    result = await plugin_api.overview()
    assert result["hookdeck"]["configured"] is False
    # The adapter runs regardless; the tab is observability, not a dependency.
    assert "local" in result
