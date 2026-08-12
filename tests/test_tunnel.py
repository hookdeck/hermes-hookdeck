from __future__ import annotations

import pytest

from hookdeck.tunnel import HookdeckCLIMissing, HookdeckTunnel


def test_listen_args_match_the_cli_grammar():
    tunnel = HookdeckTunnel(port=3579, path="/hookdeck/github-prs", source="github")
    # The grammar is positional, so the order of the leading arguments is the
    # part that matters; flags may be appended after it.
    assert tunnel.listen_args()[:7] == [
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
    assert tunnel.listen_args()[:8] == [
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


def test_the_gateway_owns_its_cli_session():
    """`listen` must forward from the project the API key manages.

    Those are two independent settings with nothing reconciling them, and the
    failure when they differ is silent: setup succeeds, the gateway reports
    connected, and the tunnel loops on "no connection found matching filter"
    while events pile up as CLI_DISCONNECTED.
    """
    tunnel = HookdeckTunnel(
        port=3579, path="/hookdeck/x", source="src",
        config_path="/tmp/gw-cli-config.toml",
    )
    args = tunnel.listen_args()
    assert "--hookdeck-config" in args
    assert args[args.index("--hookdeck-config") + 1] == "/tmp/gw-cli-config.toml"


def test_an_ambient_session_is_still_allowed():
    # Explicitly empty means "use my own `hookdeck login`", accepting the risk.
    args = HookdeckTunnel(port=1, path="/p", source="src", config_path="").listen_args()
    assert "--hookdeck-config" not in args


def test_sessions_are_identifiable_as_this_gateway():
    # The CLI defaults device name to the bare hostname, so the operator's own
    # `hookdeck listen` and the gateway's are indistinguishable in Hookdeck.
    args = HookdeckTunnel(port=1, path="/p", source="src").listen_args()
    assert "--device-name" in args
    assert args[args.index("--device-name") + 1].startswith("hermes-")
