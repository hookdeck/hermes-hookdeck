from __future__ import annotations

import asyncio
import logging

import pytest

from hookdeck import tunnel as tunnel_mod
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

# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# The supervisor
# ----------------------------------------------------------------------


class FakeStdout:
    """An async-iterable stand-in for the subprocess's stdout pipe."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProcess:
    def __init__(self, lines: list[bytes] | None = None, returncode: int | None = None):
        self.stdout = FakeStdout(lines or [])
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        #: Set to make wait() hang, so terminate escalates to kill.
        self.ignores_terminate = False

    async def wait(self) -> int:
        if self.ignores_terminate and not self.killed:
            await asyncio.Event().wait()  # never returns
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture()
def spawned(monkeypatch):
    """Capture every subprocess the tunnel would spawn, and spawn none."""
    processes: list[FakeProcess] = []
    commands: list[tuple] = []
    queued: list[FakeProcess] = []

    async def fake_exec(*args, **kwargs):
        commands.append((args, kwargs))
        process = queued.pop(0) if queued else FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(tunnel_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tunnel_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    return type(
        "Spawned", (), {"processes": processes, "commands": commands, "queue": queued}
    )


#: Captured before any test patches the module, so the driver below can still
#: yield to the loop while the supervisor's own sleeps are being recorded.
_real_sleep = asyncio.sleep


@pytest.fixture()
def no_waiting(monkeypatch):
    """Record the supervisor's backoff delays instead of serving them.

    ``tunnel_mod.asyncio`` is the shared module object, so this patch is
    global for the duration of the test — hence ``_real_sleep`` above.
    """
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        await _real_sleep(0)

    monkeypatch.setattr(tunnel_mod.asyncio, "sleep", fake_sleep)
    return slept


async def _supervise_rounds(tunnel: HookdeckTunnel, rounds: int, slept: list) -> None:
    """Run the supervisor until it has backed off *rounds* times."""
    task = asyncio.create_task(tunnel._supervise("/usr/bin/hookdeck"))
    for _ in range(2000):
        if len(slept) >= rounds:
            break
        await _real_sleep(0)
    tunnel._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_the_api_key_reaches_the_subprocess_environment(spawned):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s", api_key="key_abc")
    await tunnel._run_once("/usr/bin/hookdeck")
    _args, kwargs = spawned.commands[0]
    assert kwargs["env"]["HOOKDECK_API_KEY"] == "key_abc"


async def test_cli_output_is_relayed_into_the_gateway_log(spawned, caplog):
    spawned.queue.append(FakeProcess(lines=[b"Ready!\n", b"\n", b"forwarding\n"]))
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    with caplog.at_level(logging.INFO):
        await tunnel._run_once("/usr/bin/hookdeck")
    relayed = [m for m in (r.getMessage() for r in caplog.records) if "cli]" in m]
    # The blank line is dropped rather than relayed as an empty log record.
    assert relayed == ["[hookdeck cli] Ready!", "[hookdeck cli] forwarding"]


async def test_a_tunnel_that_keeps_dying_backs_off_and_then_stops_growing(
    spawned, no_waiting
):
    # Every run here is instant, so none reaches _HEALTHY_RUN_SECONDS: the
    # delay must keep doubling rather than resetting.
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    await _supervise_rounds(tunnel, 8, no_waiting)

    assert no_waiting[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert max(no_waiting) <= tunnel_mod._BACKOFF_MAX
    assert no_waiting[-1] == tunnel_mod._BACKOFF_MAX


async def test_a_tunnel_that_ran_healthily_restarts_promptly(
    spawned, no_waiting, monkeypatch
):
    # The rule that is easy to get backwards: a session that ran a long time
    # and then ended is not a failing start, so the next restart must be quick
    # rather than inheriting the previous delay.
    clock = {"now": 0.0}

    class FakeLoop:
        def time(self):
            return clock["now"]

    monkeypatch.setattr(tunnel_mod.asyncio, "get_running_loop", lambda: FakeLoop())

    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    runs = {"n": 0}
    original = tunnel._run_once

    async def timed_run(binary):
        runs["n"] += 1
        # Two quick failures, then one long healthy session.
        clock["now"] += 1.0 if runs["n"] <= 2 else tunnel_mod._HEALTHY_RUN_SECONDS * 2
        await original(binary)

    tunnel._run_once = timed_run
    await _supervise_rounds(tunnel, 4, no_waiting)

    assert no_waiting[0] == 2.0
    assert no_waiting[1] == 4.0
    # The healthy session ends here; the delay after it drops back to initial.
    assert no_waiting[2] == 2.0


async def test_stopping_does_not_schedule_another_restart(spawned, no_waiting):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    tunnel._stopping = True
    await tunnel._supervise("/usr/bin/hookdeck")
    assert no_waiting == []
    assert spawned.commands == []


async def test_a_crashing_run_is_logged_and_retried_rather_than_fatal(
    spawned, no_waiting, caplog
):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")

    async def explode(_binary):
        raise OSError("pipe died")

    tunnel._run_once = explode
    await _supervise_rounds(tunnel, 2, no_waiting)

    assert "CLI tunnel error: pipe died" in caplog.text
    assert len(no_waiting) >= 2  # it kept going


async def test_stop_terminates_the_process_and_cancels_the_supervisor(spawned):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    process = FakeProcess()
    process.returncode = None
    tunnel._process = process
    tunnel._supervisor = asyncio.create_task(asyncio.Event().wait())

    await tunnel.stop()

    assert process.terminated and not process.killed
    assert tunnel._supervisor is None
    assert tunnel._process is None
    assert tunnel._stopping is True


async def test_a_process_that_ignores_terminate_is_killed(spawned, monkeypatch):
    # Otherwise shutdown hangs on a wedged CLI, and the port stays bound.
    async def instant_timeout(awaitable, timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(tunnel_mod.asyncio, "wait_for", instant_timeout)

    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    process = FakeProcess()
    process.returncode = None
    tunnel._process = process

    await tunnel._terminate()

    assert process.terminated and process.killed


async def test_terminating_an_already_dead_process_is_a_no_op(spawned):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s")
    process = FakeProcess(returncode=0)
    tunnel._process = process
    await tunnel._terminate()
    assert not process.terminated


# ----------------------------------------------------------------------
# `hookdeck ci`
# ----------------------------------------------------------------------


async def test_login_is_skipped_unless_it_was_asked_for(spawned):
    # The default, and deliberately so: `hookdeck ci` rewrites the operator's
    # shared CLI config and repoints its active project.
    tunnel = HookdeckTunnel(port=1, path="/x", source="s", api_key="k", login=False)
    await tunnel._login("/usr/bin/hookdeck")
    assert spawned.commands == []


async def test_login_without_a_key_falls_back_to_an_existing_session(spawned, caplog):
    tunnel = HookdeckTunnel(port=1, path="/x", source="s", api_key="", login=True)
    with caplog.at_level(logging.INFO):
        await tunnel._login("/usr/bin/hookdeck")
    assert spawned.commands == []
    assert "existing `hookdeck login` session" in caplog.text


async def test_login_warns_before_rewriting_the_shared_config(spawned, caplog, monkeypatch):
    async def communicate_ok(self):
        self.returncode = 0
        return (b"", None)

    monkeypatch.setattr(FakeProcess, "communicate", communicate_ok, raising=False)
    monkeypatch.setattr(
        tunnel_mod.asyncio, "wait_for", lambda awaitable, timeout: awaitable
    )

    tunnel = HookdeckTunnel(port=1, path="/x", source="s", api_key="k", login=True)
    await tunnel._login("/usr/bin/hookdeck")

    assert "rewrites" in caplog.text
    assert spawned.commands[0][0][1:] == ("ci", "--api-key", "k")
