from __future__ import annotations

import argparse
import time

import pytest

from hookdeck import cli


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "route": "",
        "all": False,
        "source": "",
        "source_type": "",
        "mode": "",
        "url": "",
        "path": "",
        "rate_limit": None,
        "rate_limit_period": "concurrent",
        "group_key": "",
        "dry_run": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(hookdeck_action="setup", **defaults)


def test_parser_registers_every_subcommand():
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    parsed = parser.parse_args(["setup", "github", "--mode", "cli"])
    assert parsed.hookdeck_action == "setup"
    assert parsed.route == "github"
    assert parsed.mode == "cli"

    for action in ("status", "doctor", "retry", "pause", "resume"):
        argv = [action] + (["conn"] if action in {"pause", "resume"} else [])
        fresh = argparse.ArgumentParser()
        cli.register_cli(fresh)
        assert fresh.parse_args(argv).hookdeck_action == action


def test_no_action_is_a_usage_error(capsys):
    code = cli.hookdeck_command(argparse.Namespace(hookdeck_action=None))
    assert code == 2
    assert "Usage" in capsys.readouterr().out


def test_setup_without_a_route_lists_configured_routes(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_load_hermes_config",
        lambda: {"gateway": {"platforms": {"hookdeck": {"extra": {"routes": {"github": {}}}}}}},
    )
    assert cli.hookdeck_command(_args()) == 2
    assert "github" in capsys.readouterr().out


def test_setup_dry_run_prints_the_payload_and_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {
                    "hookdeck": {
                        "extra": {
                            "mode": "cli",
                            "path": "/hookdeck",
                            "routes": {
                                "github": {"source": "github", "source_type": "GITHUB"}
                            },
                        }
                    }
                }
            }
        },
    )
    assert cli.hookdeck_command(_args(route="github")) == 0
    out = capsys.readouterr().out
    assert '"type": "CLI"' in out
    assert '"path": "/hookdeck/github"' in out
    assert '"name": "github"' in out


def test_setup_can_target_a_route_that_is_not_in_config_yet(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_hermes_config", lambda: {})
    assert cli.hookdeck_command(_args(route="brand-new", source="stripe")) == 0
    assert '"name": "stripe"' in capsys.readouterr().out


# ----------------------------------------------------------------------
# CLI version gate
# ----------------------------------------------------------------------


def test_version_comparison_treats_a_prerelease_as_older():
    assert cli._version_at_least("2.3.2", cli.MIN_CLI_VERSION)
    assert cli._version_at_least("2.4.0", cli.MIN_CLI_VERSION)
    assert cli._version_at_least("3.0.0", cli.MIN_CLI_VERSION)
    assert not cli._version_at_least("2.3.1", cli.MIN_CLI_VERSION)
    assert not cli._version_at_least("2.2.9", cli.MIN_CLI_VERSION)
    # 2.3.2-beta.1 precedes 2.3.2, and the bug this gate exists for is fixed
    # in the release, not the pre-release.
    assert not cli._version_at_least("2.3.2-beta.1", cli.MIN_CLI_VERSION)
    assert not cli._version_at_least("", cli.MIN_CLI_VERSION)
    assert not cli._version_at_least("garbage", cli.MIN_CLI_VERSION)


def test_a_newer_prerelease_still_counts_as_newer():
    assert cli._version_at_least("2.4.0-beta.1", cli.MIN_CLI_VERSION)


def test_setup_warns_that_a_concurrency_cap_is_inert_under_an_early_ack(
    monkeypatch, capsys
):
    # Hookdeck counts deliveries open to the destination; async_retry closes
    # them at the 202. The setting is accepted and never engages, so the moment
    # someone types the flag is the moment to say so.
    monkeypatch.setattr(
        cli,
        "_load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {
                    "hookdeck": {
                        "extra": {
                            "ack_mode": "async_retry",
                            "routes": {"r": {"source": "s"}},
                        }
                    }
                }
            }
        },
    )
    cli.hookdeck_command(
        _args(route="r", rate_limit=2, rate_limit_period="concurrent", mode="push",
              url="https://example.com/hookdeck/r")
    )
    assert "has no effect with ack_mode: async_retry" in capsys.readouterr().out


def test_no_warning_in_sync_mode_where_the_cap_does_work(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {
                    "hookdeck": {
                        "extra": {"ack_mode": "sync", "routes": {"r": {"source": "s"}}}
                    }
                }
            }
        },
    )
    cli.hookdeck_command(
        _args(route="r", rate_limit=2, rate_limit_period="concurrent", mode="push",
              url="https://example.com/hookdeck/r")
    )
    assert "has no effect" not in capsys.readouterr().out


# ----------------------------------------------------------------------
# doctor: the CLI and the API key must agree on a project
# ----------------------------------------------------------------------


def _write_cli_config(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_the_active_profile_decides_which_project_the_cli_uses(tmp_path):
    """A CLI config is multi-section, with `profile` selecting the active one.

    Reading the first `project_id` in the file reports a mismatch that is not
    real for anyone with more than one profile — worse than not checking.
    """
    config = _write_cli_config(
        tmp_path / "config.toml",
        "profile = 'work'\n\n[default]\nproject_id = 'tm_personal'\n\n"
        "[work]\nproject_id = 'tm_work'\n",
    )
    assert cli._cli_config_project(config) == ("tm_work", True)


def test_the_default_profile_is_assumed_when_none_is_named(tmp_path):
    config = _write_cli_config(
        tmp_path / "config.toml", "[default]\nproject_id = 'tm_a'\n"
    )
    assert cli._cli_config_project(config) == ("tm_a", True)


def test_a_missing_config_is_distinguished_from_one_without_a_project(tmp_path):
    # doctor says something different about each: an absent file is fine
    # because the gateway creates it, an unusable one is not.
    assert cli._cli_config_project(tmp_path / "nope.toml") == ("", False)
    present = _write_cli_config(tmp_path / "c.toml", "[default]\napi_key = 'k'\n")
    assert cli._cli_config_project(present) == ("", True)


def test_a_project_mismatch_is_reported(tmp_path, monkeypatch):
    config = _write_cli_config(
        tmp_path / "config.toml", "[default]\nproject_id = 'tm_cli'\n"
    )
    monkeypatch.setattr(cli, "_api_key_project", lambda: "tm_key")
    check = cli._check_cli_project({"cli_config_path": str(config)})
    assert not check.ok
    assert "tm_key" in check.message and "tm_cli" in check.message


def test_agreement_is_reported_as_fine(tmp_path, monkeypatch):
    config = _write_cli_config(
        tmp_path / "config.toml", "[default]\nproject_id = 'tm_same'\n"
    )
    monkeypatch.setattr(cli, "_api_key_project", lambda: "tm_same")
    assert cli._check_cli_project({"cli_config_path": str(config)}).ok


def test_a_config_the_gateway_has_not_created_yet_is_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_api_key_project", lambda: "tm_key")
    check = cli._check_cli_project({"cli_config_path": str(tmp_path / "absent.toml")})
    assert check.ok
    assert "does not exist yet" in check.note

# ----------------------------------------------------------------------
# The commands that talk to Hookdeck
# ----------------------------------------------------------------------


class FakeAPI:
    """A recording stand-in for :class:`HookdeckAPI`.

    ``raises`` makes every call fail, which is the interesting case for an
    operator command: these are the ones typed when something is already
    wrong, so a traceback instead of a diagnosis is a real failure mode.
    """

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple] = []
        self.raises: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def _answer(self, _name, /, *args, **kwargs):
        # Positional-only: `list_connections(name=…)` would otherwise collide.
        self.calls.append((_name, args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.responses.get(_name, {})

    async def queue_depth(self, **kw):
        return self._answer("queue_depth", **kw)

    async def list_events(self, **kw):
        return self._answer("list_events", **kw)

    async def list_issues(self, **kw):
        return self._answer("list_issues", **kw)

    async def list_connections(self, **kw):
        return self._answer("list_connections", **kw)

    async def pause_connection(self, cid):
        return self._answer("pause_connection", cid)

    async def unpause_connection(self, cid):
        return self._answer("unpause_connection", cid)

    async def retry_event(self, eid):
        return self._answer("retry_event", eid)

    async def bulk_retry_events(self, query):
        return self._answer("bulk_retry_events", query)


@pytest.fixture()
def fake_api(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(cli, "_api", lambda: api)
    return api


@pytest.fixture()
def ledger_at(tmp_path, monkeypatch):
    """Point the CLI's ledger reads at a temporary database."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_ledger_path", lambda: path)
    return path


def _ns(action: str, **kwargs):
    return argparse.Namespace(hookdeck_action=action, **kwargs)


def calls_named(api, name):
    return [c for c in api.calls if c[0] == name]


# ── status ──────────────────────────────────────────────────────────


def test_status_reports_each_source_and_says_when_one_is_empty(
    fake_api, ledger_at, capsys
):
    fake_api.responses.update(
        queue_depth={"max_depth": 4},
        list_events={"models": [{"id": "evt_1", "error_code": "TIMEOUT",
                                 "response_status": 500, "attempts": 2}]},
        list_issues={"models": []},
    )
    assert cli.hookdeck_command(_ns("status", limit=10)) == 0
    out = capsys.readouterr().out
    assert "Queue depth" in out and "evt_1" in out
    assert "Open issues (0)" in out and "(none)" in out
    assert "not created yet" in out  # no ledger written yet


def test_status_survives_an_api_that_is_down(fake_api, ledger_at, capsys):
    # Every section is reported independently: one failing call must not cost
    # the operator the other two, or the local ledger.
    from hookdeck.api import HookdeckAPIError

    fake_api.raises = HookdeckAPIError(503, "GET", "/metrics", "unavailable")
    assert cli.hookdeck_command(_ns("status", limit=10)) == 0
    out = capsys.readouterr().out
    assert "Queue depth unavailable" in out
    assert "Event list unavailable" in out
    assert "Issue list unavailable" in out


def test_status_prints_the_local_ledger_when_there_is_one(
    fake_api, ledger_at, capsys
):
    from hookdeck.ledger import RunLedger

    ledger = RunLedger(ledger_at)
    try:
        ledger.admit("evt_1", route="github", attempt=1)
        ledger.mark_failed("evt_1", "agent raised")
    finally:
        ledger.close()

    fake_api.responses.update(list_events={"models": []}, list_issues={"models": []})
    cli.hookdeck_command(_ns("status", limit=10))
    out = capsys.readouterr().out
    assert "Local delivery ledger" in out
    assert "evt_1" in out and "agent raised" in out


# ── pause and resume ────────────────────────────────────────────────


def test_pause_resolves_a_name_before_acting(fake_api, capsys):
    fake_api.responses["list_connections"] = {"models": [{"id": "web_1"}]}
    assert cli.hookdeck_command(_ns("pause", connection="github")) == 0
    assert calls_named(fake_api, "pause_connection")[0][1] == ("web_1",)
    assert "safe to restart" in capsys.readouterr().out


def test_pause_uses_an_id_as_given(fake_api):
    assert cli.hookdeck_command(_ns("pause", connection="web_direct")) == 0
    assert not calls_named(fake_api, "list_connections")
    assert calls_named(fake_api, "pause_connection")[0][1] == ("web_direct",)


def test_pausing_a_name_that_matches_nothing_fails_loudly(fake_api, capsys):
    fake_api.responses["list_connections"] = {"models": []}
    assert cli.hookdeck_command(_ns("pause", connection="typo")) == 1
    assert "No connection named 'typo'" in capsys.readouterr().out
    assert not calls_named(fake_api, "pause_connection")


def test_resume_unpauses(fake_api, capsys):
    assert cli.hookdeck_command(_ns("resume", connection="web_1")) == 0
    assert calls_named(fake_api, "unpause_connection")[0][1] == ("web_1",)
    assert "drain" in capsys.readouterr().out


def test_a_failing_pause_exits_non_zero(fake_api, capsys):
    from hookdeck.api import HookdeckAPIError

    fake_api.raises = HookdeckAPIError(403, "PUT", "/connections/x/pause", "denied")
    assert cli.hookdeck_command(_ns("pause", connection="web_1")) == 1
    assert "denied" in capsys.readouterr().out


# ── retry ───────────────────────────────────────────────────────────


def test_retrying_one_event(fake_api, capsys):
    args = _ns("retry", event_id="evt_1", failed=False, since="", connection="")
    assert cli.hookdeck_command(args) == 0
    assert calls_named(fake_api, "retry_event")[0][1] == ("evt_1",)
    assert "evt_1" in capsys.readouterr().out


def test_retry_with_no_target_is_a_usage_error(fake_api, capsys):
    args = _ns("retry", event_id="", failed=False, since="", connection="")
    assert cli.hookdeck_command(args) == 2
    assert "Usage" in capsys.readouterr().out
    assert fake_api.calls == []


def test_bulk_retry_is_scoped_to_failures(fake_api):
    args = _ns("retry", event_id="", failed=True, since="", connection="")
    assert cli.hookdeck_command(args) == 0
    assert calls_named(fake_api, "bulk_retry_events")[0][1][0] == {"status": "FAILED"}


def test_bulk_retry_carries_its_filters(fake_api):
    args = _ns("retry", event_id="", failed=True, since="2026-08-01T00:00:00Z",
               connection="web_1")
    cli.hookdeck_command(args)
    assert calls_named(fake_api, "bulk_retry_events")[0][1][0] == {
        "status": "FAILED",
        "created_at": {"gte": "2026-08-01T00:00:00Z"},
        "webhook_id": "web_1",
    }


def test_a_failing_retry_exits_non_zero(fake_api, capsys):
    from hookdeck.api import HookdeckAPIError

    fake_api.raises = HookdeckAPIError(404, "POST", "/events/x/retry", "no such event")
    args = _ns("retry", event_id="evt_gone", failed=False, since="", connection="")
    assert cli.hookdeck_command(args) == 1
    assert "no such event" in capsys.readouterr().out


# ── doctor ──────────────────────────────────────────────────────────


@pytest.fixture()
def doctor_env(monkeypatch, ledger_at):
    """A doctor run with nothing live: no API key, no CLI binary.

    Both env-var spellings are cleared. Only the unprefixed names are read on
    this branch, but an operator's shell may have either set, and a doctor
    test that passes because of the caller's environment is worse than none.
    """
    for name in ("API_KEY", "WEBHOOK_SECRET", "MODE", "PROJECT_ID"):
        monkeypatch.delenv(f"HOOKDECK_{name}", raising=False)
        monkeypatch.delenv(f"HOOKDECK_EG_{name}", raising=False)

    # Recorded, not just stubbed: "did the CLI checks run at all?" is the
    # question in push mode, and answering it by grepping the output for
    # "Hookdeck CLI" breaks as soon as an unrelated message mentions the CLI.
    monkeypatch.which_calls = []

    def which(binary):
        # Returns None: no binary is on PATH in this fixture's world.
        monkeypatch.which_calls.append(binary)

    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli, "_load_hermes_config", dict)
    monkeypatch.setattr(cli, "_platform_extra", dict)
    return monkeypatch


def _configure(monkeypatch, *, routes=None, **extra):
    """Set both config readers doctor uses, so they cannot disagree."""
    full = {**extra, "routes": routes or {}}
    monkeypatch.setattr(cli, "_platform_extra", lambda *_a, **_k: full)
    monkeypatch.setattr(
        cli,
        "_load_hermes_config",
        lambda: {"gateway": {"platforms": {"hookdeck": {"extra": full}}}},
    )


def test_doctor_fails_and_names_every_missing_piece(doctor_env, capsys):
    assert cli.hookdeck_command(_ns("doctor")) == 1
    out = capsys.readouterr().out

    # Matched against the failing lines only, so a passing check that happens
    # to use the same words cannot satisfy this.
    failures = "\n".join(
        line for line in out.splitlines() if line.startswith("✗")
    ).lower()
    # Either spelling of the variable counts: the regression worth catching is
    # doctor staying quiet about a missing key, not what the key is called.
    assert "api key" in failures or "api_key" in failures
    assert "no signing secret" in failures
    assert "no routes under" in failures
    assert "not found" in failures  # the CLI binary
    # An operator running this is already stuck; the diagnosis has to say what
    # to do, not just that something is wrong.
    assert "mode: push" in failures


def test_doctor_passes_when_everything_is_in_place(
    doctor_env, capsys, monkeypatch, fake_api
):
    from hookdeck.provision import retryable_status_codes

    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    monkeypatch.setenv("HOOKDECK_WEBHOOK_SECRET", "whsec_x")
    doctor_env.setattr(cli.shutil, "which", lambda _binary: "/usr/local/bin/hookdeck")
    doctor_env.setattr(cli, "_cli_version", lambda _binary: "2.4.0")
    doctor_env.setattr(cli, "_other_hookdeck_binaries", lambda _resolved: [])
    # A retry rule that does cover everything the adapter emits — the same
    # shape `hermes hookdeck setup` provisions.
    fake_api.responses["list_connections"] = {
        "models": [
            {
                "name": "github",
                "rules": [
                    {
                        "type": "retry",
                        "response_status_codes": retryable_status_codes(),
                    }
                ],
            }
        ]
    }
    _configure(doctor_env, secret="whsec_x", routes={"github": {"source": "github"}})

    assert cli.hookdeck_command(_ns("doctor")) == 0
    out = capsys.readouterr().out
    assert "1 route(s) configured: github" in out
    assert "meets the" in out
    assert "Hookdeck API reachable" in out
    assert "✗" not in out


def test_doctor_refuses_a_cli_too_old_to_stay_connected(doctor_env, capsys):
    # The failure this guards is silent: an expired session stops delivering
    # without saying so, which looks like "no events" rather than a bug.
    doctor_env.setattr(cli.shutil, "which", lambda _binary: "/usr/local/bin/hookdeck")
    doctor_env.setattr(cli, "_cli_version", lambda _binary: "2.3.1")
    doctor_env.setattr(cli, "_other_hookdeck_binaries", lambda _resolved: [])
    assert cli.hookdeck_command(_ns("doctor")) == 1
    assert "is below 2.3.2" in capsys.readouterr().out


def test_doctor_names_a_shadowed_cli_install(doctor_env, capsys):
    # Version-checking one binary and launching another is the whole reason
    # the resolved path is printed rather than the name.
    doctor_env.setattr(cli.shutil, "which", lambda _binary: "/opt/homebrew/bin/hookdeck")
    doctor_env.setattr(cli, "_cli_version", lambda _binary: "2.4.0")
    doctor_env.setattr(
        cli, "_other_hookdeck_binaries", lambda _resolved: ["/usr/local/bin/hookdeck"]
    )
    cli.hookdeck_command(_ns("doctor"))
    out = capsys.readouterr().out
    assert "/opt/homebrew/bin/hookdeck" in out
    assert "shadowed by it" in out


def test_doctor_in_push_mode_requires_a_public_url(doctor_env, capsys):
    _configure(doctor_env, mode="push")
    assert cli.hookdeck_command(_ns("doctor")) == 1
    assert "push mode with no public_url" in capsys.readouterr().out
    # The cli-mode checks must not run in push mode: looking for a binary the
    # gateway will never launch is a failure an operator cannot act on.
    assert doctor_env.which_calls == []


def test_doctor_surfaces_runs_that_were_never_completed(doctor_env, capsys, ledger_at):
    from hookdeck.ledger import RunLedger

    ledger = RunLedger(ledger_at)
    try:
        ledger.admit("evt_stuck", route="github", attempt=1)
        # Backdate it past the one-hour threshold.
        ledger._conn.execute(
            "UPDATE deliveries SET updated_at = ? WHERE event_id = ?",
            (time.time() - 7200, "evt_stuck"),
        )
        ledger._conn.commit()
    finally:
        ledger.close()

    cli.hookdeck_command(_ns("doctor"))
    out = capsys.readouterr().out
    assert "still marked running after an hour" in out
    assert "hermes hookdeck retry evt_stuck" in out


def test_doctor_checks_live_connections_when_a_key_is_present(
    doctor_env, capsys, monkeypatch, fake_api
):
    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    fake_api.responses["list_connections"] = {
        "models": [
            {
                "name": "github",
                # Narrower than what the adapter emits: 503 and 401 are missing,
                # so deferred and rejected events would never come back.
                "rules": [{"type": "retry", "response_status_codes": ["500"]}],
            }
        ]
    }
    _configure(doctor_env, routes={"github": {}})
    assert cli.hookdeck_command(_ns("doctor")) == 1
    out = capsys.readouterr().out
    assert "does not cover" in out
    assert "Re-run `hermes hookdeck setup`" in out


def test_doctor_reports_an_unreachable_api_as_a_failed_check(
    doctor_env, capsys, monkeypatch, fake_api
):
    from hookdeck.api import HookdeckAPIError

    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    fake_api.raises = HookdeckAPIError(0, "GET", "/connections", "connection refused")
    _configure(doctor_env, routes={"github": {}})
    assert cli.hookdeck_command(_ns("doctor")) == 1
    assert "Hookdeck API check failed" in capsys.readouterr().out


# ── doctor: burst headroom ──────────────────────────────────────────


def test_doctor_reports_how_large_a_burst_survives(doctor_env, fake_api, monkeypatch, capsys):
    # An event deferred with 503 only gets back in on a retry, so the queue
    # drains at max_concurrent per round and each event has `count` rounds.
    # Their product is the burst that survives — a real loss we measured.
    from hookdeck.provision import retryable_status_codes

    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    doctor_env.setattr(cli.shutil, "which", lambda _b: "/usr/local/bin/hookdeck")
    doctor_env.setattr(cli, "_cli_version", lambda _b: "2.4.0")
    doctor_env.setattr(cli, "_other_hookdeck_binaries", lambda _r: [])
    fake_api.responses["list_connections"] = {
        "models": [{
            "name": "github", "team_id": "tm_1",
            "rules": [{"type": "retry", "count": 10,
                       "response_status_codes": retryable_status_codes()}],
        }]
    }
    _configure(doctor_env, secret="s", max_concurrent=3,
               cli_config_path="", routes={"github": {}})

    cli.hookdeck_command(_ns("doctor"))
    out = capsys.readouterr().out
    assert "absorbs a burst of about 30 events" in out
    assert "max_concurrent 3 x 10 retries" in out


def test_unlimited_concurrency_defers_nothing(doctor_env, fake_api, monkeypatch, capsys):
    monkeypatch.setenv("HOOKDECK_API_KEY", "key")
    doctor_env.setattr(cli.shutil, "which", lambda _b: "/usr/local/bin/hookdeck")
    doctor_env.setattr(cli, "_cli_version", lambda _b: "2.4.0")
    doctor_env.setattr(cli, "_other_hookdeck_binaries", lambda _r: [])
    fake_api.responses["list_connections"] = {
        "models": [{"name": "github", "team_id": "tm_1",
                    "rules": [{"type": "retry", "count": 10}]}]
    }
    _configure(doctor_env, secret="s", max_concurrent=0,
               cli_config_path="", routes={"github": {}})

    cli.hookdeck_command(_ns("doctor"))
    assert "nothing is deferred for capacity" in capsys.readouterr().out
