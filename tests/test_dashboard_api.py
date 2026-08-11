"""The dashboard tab's backend.

Loaded by path the way the web server loads it, so the sys.path bootstrap at
the top of the module is exercised rather than bypassed by a package import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hookdeck.constants import API_KEY_ENV, CLI_API_KEY_ENV

MODULE_PATH = Path(__file__).resolve().parents[1] / "hookdeck" / "dashboard" / "plugin_api.py"


@pytest.fixture(scope="module")
def plugin_api():
    spec = importlib.util.spec_from_file_location("hookdeck_dashboard_api", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_loads_the_way_the_web_server_loads_it(plugin_api):
    paths = {r.path for r in plugin_api.router.routes}
    # Exactly the routes the tab calls — no unused surface. An endpoint the
    # UI never hits still accepts requests.
    assert paths == {
        "/overview",
        "/events/{event_id}/retry",
        "/connections/{connection_id}/pause",
        "/connections/{connection_id}/resume",
    }


async def test_only_this_gateways_connections_get_controls(plugin_api, monkeypatch):
    # The tab renders a Pause button next to every connection it is given. A
    # project's other connections are unrelated production traffic, so showing
    # them puts an outage one misclick away.
    monkeypatch.setenv(API_KEY_ENV, "key")
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


async def test_the_age_metric_is_not_surfaced(plugin_api, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "key")
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
    # max_age has no documented unit and is a window maximum rather than a live
    # age, so it cannot be labelled honestly. A number whose meaning cannot be
    # stated is worse on a dashboard than no number.
    assert depth == {"max_depth": 6}


async def test_a_missing_api_key_is_reported_not_raised(plugin_api, monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(CLI_API_KEY_ENV, raising=False)
    result = await plugin_api.overview()
    assert result["hookdeck"]["configured"] is False
    # The adapter runs regardless; the tab is observability, not a dependency.
    assert "local" in result


def test_loading_it_does_not_touch_the_hosts_import_path():
    # This module is imported into the Hermes web-server process. Prepending
    # the repo root would put `tests/` and `examples/` on the host's path —
    # and Hermes has its own top-level `tests` package, which would then be
    # shadowed process-wide. A plugin does not get to rearrange the host's
    # imports to reach its own siblings.
    import sys

    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("hookdeck_dash_api_pathcheck", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert [p for p in sys.path if p not in before] == []


def test_it_reuses_an_already_imported_package():
    # The gateway process has already imported `hookdeck`; re-executing the
    # package under the same name would give the dashboard a second copy with
    # its own module-level state.
    import sys

    import hookdeck

    spec = importlib.util.spec_from_file_location("hookdeck_dash_api_reuse", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sys.modules["hookdeck"] is hookdeck


# ----------------------------------------------------------------------
# Acting on a connection, not just displaying it
# ----------------------------------------------------------------------


class _ActionAPI:
    """Records mutations and answers name lookups from `owned`."""

    def __init__(self, owned: dict[str, str]):
        self.owned = owned
        self.paused: list[str] = []
        self.unpaused: list[str] = []
        self.retried: list[str] = []
        self.lookups: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def list_connections(self, **params):
        name = params.get("name", "")
        self.lookups.append(name)
        found = [{"id": cid} for cid, n in self.owned.items() if n == name]
        return {"models": found}

    async def pause_connection(self, connection_id):
        self.paused.append(connection_id)

    async def unpause_connection(self, connection_id):
        self.unpaused.append(connection_id)

    async def retry_event(self, event_id):
        self.retried.append(event_id)


@pytest.fixture()
def acting(plugin_api, monkeypatch):
    """Configure two routes, and hand every endpoint one recording client."""
    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    monkeypatch.setattr(
        plugin_api,
        "_load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {
                    "hookdeck": {"extra": {"routes": {"mine": {}, "also-mine": {}}}}
                }
            }
        },
    )
    api = _ActionAPI({"c1": "mine", "c2": "also-mine"})
    monkeypatch.setattr(plugin_api, "HookdeckAPI", lambda *a, **k: api)
    return api


async def test_a_connection_named_by_a_route_can_be_paused(plugin_api, acting):
    assert await plugin_api.pause("c1") == {"status": "paused", "connection_id": "c1"}
    assert acting.paused == ["c1"]


async def test_every_configured_route_is_checked_not_just_the_first(
    plugin_api, acting
):
    # Stopping at the first route would refuse a legitimate second connection.
    await plugin_api.pause("c2")
    assert acting.lookups == ["mine", "also-mine"]
    assert acting.paused == ["c2"]


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_a_connection_this_gateway_does_not_own_is_refused(
    plugin_api, acting, action
):
    # The endpoint is reachable whether or not the tab renders a button for
    # it, and the connection on the other end is somebody's production
    # traffic. Filtering the list is presentation; this is the control.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await getattr(plugin_api, action)("c_someone_elses")

    assert raised.value.status_code == 403
    assert "does not belong to a configured route" in raised.value.detail
    assert acting.paused == [] and acting.unpaused == []


async def test_an_owned_connection_can_be_resumed(plugin_api, acting):
    assert await plugin_api.resume("c1") == {"status": "resumed", "connection_id": "c1"}
    assert acting.unpaused == ["c1"]


async def test_retrying_an_event_needs_no_ownership_check(plugin_api, acting):
    # Retry is scoped by the event id itself, and an id from another project
    # simply 404s at Hookdeck — there is no connection to mistake.
    assert await plugin_api.retry_event("evt_1") == {
        "status": "queued",
        "event_id": "evt_1",
    }
    assert acting.retried == ["evt_1"]
    assert acting.lookups == []
