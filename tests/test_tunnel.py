from __future__ import annotations

import pytest

from hookdeck.tunnel import HookdeckCLIMissing, HookdeckTunnel


def test_listen_args_match_the_cli_grammar():
    tunnel = HookdeckTunnel(port=3579, path="/hookdeck/github-prs", source="github")
    assert tunnel.listen_args() == [
        "listen",
        "3579",
        "github",
        "--path",
        "/hookdeck/github-prs",
        "--output",
        "compact",
    ]


def test_connection_name_is_passed_as_the_third_positional():
    tunnel = HookdeckTunnel(
        port=3579, path="/hookdeck/x", source="stripe", connection_name="disputes"
    )
    assert tunnel.listen_args() == [
        "listen",
        "3579",
        "stripe",
        "disputes",
        "--path",
        "/hookdeck/x",
        "--output",
        "compact",
    ]


def test_path_is_always_set():
    # The CLI defaults the destination path to "/", which would miss the
    # adapter's route handler entirely.
    args = HookdeckTunnel(port=1, path="/hookdeck/a", source="s").listen_args()
    assert "--path" in args


def test_a_source_is_required():
    with pytest.raises(ValueError, match="requires a source"):
        HookdeckTunnel(port=3579, path="/hookdeck", source="")


def test_a_missing_binary_explains_the_alternative():
    tunnel = HookdeckTunnel(
        port=1, path="/x", source="s", binary="hookdeck-does-not-exist"
    )
    with pytest.raises(HookdeckCLIMissing, match="mode: push"):
        tunnel.resolve_binary()


def test_output_mode_is_never_the_interactive_default():
    # `interactive` renders a full-screen UI and exits immediately when stdout
    # is a pipe — which it always is here, since the supervisor captures it.
    args = HookdeckTunnel(port=1, path="/x", source="s").listen_args()
    assert args[args.index("--output") + 1] == "compact"
