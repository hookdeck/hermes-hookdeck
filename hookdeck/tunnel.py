"""Supervision of the ``hookdeck listen`` CLI process.

In ``cli`` mode the adapter does not need a public URL. The Hookdeck CLI holds
an outbound connection to Hookdeck and forwards events to the adapter's
loopback listener, which makes a laptop or homelab install a first-class
citizen — and, unlike a plain tunnel, events that arrive while the machine is
asleep sit in the Hookdeck queue and are delivered on reconnect.

This module owns the subprocess lifecycle only: spawn, stream its output into
the gateway log, restart with capped backoff if it dies, and shut down cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket

from .constants import CLI_API_KEY_ENV
from .constants import api_key as resolve_api_key

logger = logging.getLogger(__name__)

_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0
# A run shorter than this is treated as a failed start rather than a healthy
# session that happened to end, so backoff keeps growing instead of resetting.
_HEALTHY_RUN_SECONDS = 20.0

# Generous enough that CLI output never kills a working tunnel.
_STDOUT_LINE_LIMIT = 1024 * 1024


def _device_name() -> str:
    """How this gateway's CLI sessions identify themselves to Hookdeck.

    The CLI defaults to the bare hostname, so an operator running their own
    `hookdeck listen` on the same machine shows up indistinguishably from the
    gateway's. Prefixing says which is which in the dashboard.
    """
    try:
        host = socket.gethostname() or "unknown"
    except OSError:  # pragma: no cover - environment dependent
        host = "unknown"
    return f"hermes-{host}"


class HookdeckCLIMissing(RuntimeError):
    """The ``hookdeck`` binary is not on PATH."""


class HookdeckTunnel:
    """Runs ``hookdeck listen`` for one source and keeps it running.

    ``hookdeck listen`` forwards a single source, so a gateway with routes on
    several sources runs one tunnel per source. Each is given
    ``--path <base>/<route>``, which lines up with the adapter's
    ``/<base>/{route_name}`` handler and lets the route be resolved from the
    path instead of guessing from the source name.
    """

    def __init__(
        self,
        *,
        port: int,
        path: str,
        source: str,
        connection_name: str = "",
        api_key: str = "",
        binary: str = "hookdeck",
        config_path: str = "",
    ):
        if not source:
            raise ValueError(
                "hookdeck listen requires a source name. Set `source` on the "
                "route (or platforms.hookdeck.extra.source) to the Hookdeck "
                "source you want forwarded."
            )
        self._port = port
        self._path = path
        self._source = source
        self._connection_name = connection_name
        self._api_key = api_key or resolve_api_key()
        #: A CLI config this gateway owns, kept away from the operator's own.
        self._config_path = config_path
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def resolve_binary(self) -> str:
        resolved = shutil.which(self._binary)
        if not resolved:
            raise HookdeckCLIMissing(
                "The Hookdeck CLI is not installed or not on PATH. "
                "Install it with `brew install hookdeck/hookdeck/hookdeck` "
                "(or see https://hookdeck.com/docs/cli), or switch the adapter "
                "to `mode: push` in config.yaml."
            )
        return resolved

    def listen_args(self) -> list[str]:
        """Positional arguments for ``hookdeck listen``.

        The grammar is ``listen <port> <source> [connection]``. ``--path`` is
        set explicitly because the CLI otherwise defaults the destination path
        to ``/``, which would bypass the adapter's route handler.
        """
        args = ["listen", str(self._port), self._source]
        if self._connection_name:
            args.append(self._connection_name)
        if self._path:
            args += ["--path", self._path]
        # The default `interactive` output renders a full-screen UI and exits
        # immediately when stdout is not a TTY — which it never is here, since
        # the supervisor pipes it into the gateway log.
        args += ["--output", "compact"]
        if self._config_path:
            # The gateway's own CLI session, so `listen` forwards from the same
            # project the API key manages rather than whatever the operator's
            # shared config happens to point at.
            args += ["--hookdeck-config", self._config_path]
        args += ["--device-name", _device_name()]
        return args

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        binary = self.resolve_binary()
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(binary))

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        await self._terminate()

    async def authenticate(self) -> bool:
        """Point the CLI at the same project the API key manages.

        These are two independent settings, and nothing reconciles them: the
        API key decides which project ``setup`` provisions, while the CLI's own
        config decides which project ``hookdeck listen`` forwards from. When
        they disagree the failure is silent and expensive — ``setup`` succeeds,
        the gateway reports itself connected, and the tunnel restart-loops on
        "no connection found matching filter" while every event becomes a
        ``CLI_DISCONNECTED`` ignored event.

        So the gateway authenticates a CLI config of its own, from the API key
        it already has, and passes ``--hookdeck-config`` to every CLI call.
        Two projects cannot drift apart when only one of them is configurable.

        This is deliberately not ``hookdeck ci`` against the shared config.
        That rewrites ``~/.config/hookdeck/config.toml`` and switches the CLI's
        *active project*, so anyone using the CLI for other work would find
        their environment repointed by starting a gateway — and it does so even
        with ``--local``, which claims to write to the current directory
        (observed on CLI 2.4.0). Pointing at our own path avoids the shared
        file entirely.

        Without an API key there is nothing to authenticate with, so the
        operator's ambient session is used and the mismatch stays possible;
        ``hermes hookdeck doctor`` reports it.
        """
        if not self._config_path:
            return True
        binary = self.resolve_binary()
        if not self._api_key:
            logger.warning(
                "[hookdeck] No API key, so the CLI session cannot be pinned to "
                "the same project the adapter manages. Falling back to your "
                "own `hookdeck login`. Run `hermes hookdeck doctor` to check "
                "the two agree."
            )
            self._config_path = ""
            return True
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "ci",
                "--api-key",
                self._api_key,
                "--hookdeck-config",
                self._config_path,
                "--name",
                "hermes-gateway",
                "--device-name",
                _device_name(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                logger.error(
                    "[hookdeck] Could not authenticate the gateway's CLI "
                    "config (%s): %s",
                    self._config_path,
                    (stdout or b"").decode("utf-8", "replace").strip()[:300],
                )
                return False
        except asyncio.TimeoutError:
            logger.error("[hookdeck] `hookdeck ci` timed out after 30s")
            return False
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - environment dependent
            logger.error("[hookdeck] `hookdeck ci` failed: %s", exc)
            return False
        return True

    async def _supervise(self, binary: str) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                await self._run_once(binary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the supervisor restarts, never dies
                logger.error("[hookdeck] CLI tunnel error: %s", exc)

            if self._stopping:
                return

            ran_for = asyncio.get_running_loop().time() - started
            if ran_for >= _HEALTHY_RUN_SECONDS:
                backoff = _BACKOFF_INITIAL
            logger.warning(
                "[hookdeck] CLI tunnel exited after %.0fs — restarting in %.0fs",
                ran_for,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _run_once(self, binary: str) -> None:
        args = self.listen_args()
        logger.info("[hookdeck] Starting CLI tunnel: %s %s", binary, " ".join(args))
        self._process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # A single long line — a large payload echoed into the log — would
            # otherwise exceed asyncio's 64KiB default and raise, bouncing an
            # otherwise healthy tunnel through the restart backoff.
            limit=_STDOUT_LINE_LIMIT,
            env={**os.environ, **({CLI_API_KEY_ENV: self._api_key} if self._api_key else {})},
        )
        assert self._process.stdout is not None
        async for line in self._process.stdout:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("[hookdeck cli] %s", text)
        await self._process.wait()

    async def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
