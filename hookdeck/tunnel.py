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
from typing import Optional

logger = logging.getLogger(__name__)

_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0
# A run shorter than this is treated as a failed start rather than a healthy
# session that happened to end, so backoff keeps growing instead of resetting.
_HEALTHY_RUN_SECONDS = 20.0


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
        self._api_key = api_key or os.getenv("HOOKDECK_API_KEY", "")
        self._binary = binary
        self._process: Optional[asyncio.subprocess.Process] = None
        self._supervisor: Optional[asyncio.Task] = None
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
        return args

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        binary = self.resolve_binary()
        await self._login(binary)
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

    async def _login(self, binary: str) -> None:
        """Non-interactive auth so an unattended gateway can start.

        ``hookdeck ci`` is a no-op when the CLI already holds valid
        credentials, so this is safe to run on every boot. A failure here is
        logged rather than raised: an already-logged-in CLI on an older version
        without the subcommand should still be allowed to listen.
        """
        if not self._api_key:
            logger.info(
                "[hookdeck] No HOOKDECK_API_KEY set — relying on an existing "
                "`hookdeck login` session for the CLI tunnel"
            )
            return
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "ci",
                "--api-key",
                self._api_key,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                logger.warning(
                    "[hookdeck] `hookdeck ci` exited %s: %s",
                    process.returncode,
                    (stdout or b"").decode("utf-8", "replace").strip()[:300],
                )
        except asyncio.TimeoutError:
            logger.warning("[hookdeck] `hookdeck ci` timed out after 30s")
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("[hookdeck] `hookdeck ci` failed: %s", exc)

    async def _supervise(self, binary: str) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                await self._run_once(binary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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
            env={**os.environ, **({"HOOKDECK_API_KEY": self._api_key} if self._api_key else {})},
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
