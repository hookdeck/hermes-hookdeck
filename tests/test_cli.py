from __future__ import annotations

import argparse

from hookdeck import cli


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        route="",
        all=False,
        source="",
        source_type="",
        mode="",
        url="",
        path="",
        rate_limit=None,
        rate_limit_period="concurrent",
        group_key="",
        dry_run=True,
    )
    defaults.update(kwargs)
    return argparse.Namespace(hookdeck_action="setup", **defaults)


def test_parser_registers_every_subcommand():
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    parsed = parser.parse_args(["setup", "github", "--mode", "cli"])
    assert parsed.hookdeck_action == "setup"
    assert parsed.route == "github"
    assert parsed.mode == "cli"

    for action in ("status", "doctor", "replay", "pause", "resume"):
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
