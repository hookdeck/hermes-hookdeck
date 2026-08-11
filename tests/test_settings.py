from __future__ import annotations

import pytest

from hookdeck import constants
from hookdeck.constants import MODE_ENV, PROJECT_ID_ENV, WEBHOOK_SECRET_ENV
from hookdeck.settings import AdapterSettings

MINIMAL = {"secret": "whsec_x", "routes": {"a": {"source": "s"}}}


def test_defaults_are_the_conservative_ones(monkeypatch):
    monkeypatch.delenv("HOOKDECK_MODE", raising=False)
    settings = AdapterSettings.from_extra(MINIMAL)
    assert settings.mode == "cli"
    assert settings.ack_mode == "async_retry"
    assert settings.recover_on_boot is True
    # Off by default: cancelling retries discards traffic, and `hookdeck ci`
    # rewrites the operator's shared CLI config.
    assert settings.cancel_retries_on_unparseable is False
    assert settings.cli_login is False


def test_config_wins_over_the_environment(monkeypatch):
    # The other way round would let a stray shell export silently outrank the
    # file the operator is looking at.
    monkeypatch.setenv("HOOKDECK_PATH", "/from-env")
    assert AdapterSettings.from_extra({**MINIMAL, "path": "/from-config"}).path == (
        "/from-config"
    )
    assert AdapterSettings.from_extra(MINIMAL).path == "/from-env"


def test_paths_are_normalised_to_one_leading_slash():
    for given in ("hookdeck", "/hookdeck", "/hookdeck/"):
        assert AdapterSettings.from_extra({**MINIMAL, "path": given}).path == "/hookdeck"


def test_cli_mode_pins_the_bind_to_loopback():
    settings = AdapterSettings.from_extra({**MINIMAL, "mode": "cli", "host": "0.0.0.0"})
    assert settings.host == "127.0.0.1"
    # Both families: the CLI forwards to localhost, which resolves to ::1 first
    # on a dual-stack machine.
    assert settings.bind_hosts == ["127.0.0.1", "::1"]


def test_push_mode_binds_only_what_was_configured():
    settings = AdapterSettings.from_extra({**MINIMAL, "mode": "push", "host": "127.0.0.1"})
    assert settings.bind_hosts == ["127.0.0.1"]


def test_verification_is_off_only_for_the_local_testing_escape_hatch():
    assert AdapterSettings.from_extra(MINIMAL).verifies_signatures
    assert not AdapterSettings.from_extra(
        {**MINIMAL, "secret": "INSECURE_NO_AUTH"}
    ).verifies_signatures


@pytest.mark.parametrize(
    "extra, message",
    [
        ({**MINIMAL, "mode": "carrier-pigeon"}, "Unknown mode"),
        ({**MINIMAL, "ack_mode": "whenever"}, "Unknown ack_mode"),
        ({"routes": {"a": {"source": "s"}}}, "signing secret"),
        (
            {"secret": "INSECURE_NO_AUTH", "mode": "push", "host": "0.0.0.0",
             "routes": {"a": {}}},
            "non-loopback",
        ),
        (
            {**MINIMAL, "routes": {"a": {"source": "s", "deliver_only": True}}},
            "deliver_only",
        ),
        ({"secret": "s", "mode": "cli", "routes": {"a": {}}}, "needs a Hookdeck source"),
    ],
)
def test_a_configuration_that_cannot_run_is_refused_at_startup(extra, message, monkeypatch):
    monkeypatch.delenv("HOOKDECK_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("HOOKDECK_SOURCE", raising=False)
    with pytest.raises(ValueError, match=message):
        AdapterSettings.from_extra(extra).validate()


def test_a_valid_configuration_passes(monkeypatch):
    monkeypatch.delenv("HOOKDECK_MODE", raising=False)
    AdapterSettings.from_extra(MINIMAL).validate()


def test_every_caller_resolves_the_same_ledger(tmp_path, monkeypatch):
    # The adapter writes this file; the CLI, dashboard and agent tools read and
    # write it. A caller resolving it differently records pause deadlines and
    # outcomes where nothing else will look — indistinguishable from the
    # feature silently not working.
    from hookdeck import cli
    from hookdeck.settings import configured_state_path

    monkeypatch.setattr(
        "hookdeck.settings.load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {
                    "hookdeck": {"extra": {"state_path": str(tmp_path / "custom.db")}}
                }
            }
        },
    )
    assert configured_state_path() == tmp_path / "custom.db"
    assert cli._ledger_path() == configured_state_path()


def test_the_default_is_used_when_nothing_is_configured(monkeypatch):
    from hookdeck.settings import configured_state_path
    from hookdeck.ledger import default_state_path

    monkeypatch.setattr("hookdeck.settings.load_hermes_config", dict)
    assert configured_state_path() == default_state_path()


def test_the_pre_namespace_env_vars_still_work(monkeypatch, caplog):
    """A rename must not be the reason someone's gateway stops booting.

    These names were documented before the plugin was ever published, so a
    git-installed gateway may well have them exported. They keep working and
    say so once.
    """
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.setenv("HOOKDECK_WEBHOOK_SECRET", "legacy_secret")
    monkeypatch.setenv("HOOKDECK_MODE", "push")
    constants._WARNED.clear()

    settings = AdapterSettings.from_extra({})
    assert settings.signing_secret == "legacy_secret"
    assert settings.mode == "push"
    assert "HOOKDECK_WEBHOOK_SECRET is deprecated" in caplog.text


def test_the_namespaced_env_var_wins_over_the_old_one(monkeypatch):
    monkeypatch.setenv("HOOKDECK_WEBHOOK_SECRET", "old")
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, "new")
    assert AdapterSettings.from_extra({}).signing_secret == "new"


def test_config_still_outranks_both(monkeypatch):
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, "from_env")
    assert AdapterSettings.from_extra({"secret": "from_yaml"}).signing_secret == "from_yaml"


def test_the_project_can_be_pinned_for_an_org_level_key(monkeypatch):
    monkeypatch.setenv(PROJECT_ID_ENV, "tm_from_env")
    assert AdapterSettings.from_extra({}).project_id == "tm_from_env"
    assert AdapterSettings.from_extra({"project_id": "tm_yaml"}).project_id == "tm_yaml"
