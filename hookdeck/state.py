"""Restart-safe delivery ledger.

The built-in Hermes webhook adapter dedupes in a process-local dict with a
one-hour TTL, so a gateway restart re-runs the agent on anything a provider
redelivers. This ledger is a small SQLite table instead, keyed on the Hookdeck
event id, so dedup survives restarts.

The subtle part is that deduplication and retry pull in opposite directions.
When an agent run fails, the adapter asks Hookdeck to redeliver the *same event
id* — which naive dedup would then reject as a duplicate. The ledger resolves
this with the attempt counter Hookdeck sends on every delivery
(``x-hookdeck-attempt-count``): a delivery is admitted when its attempt number
is higher than the highest one already seen for that event, and rejected
otherwise. Genuine duplicates repeat an attempt number; real retries increment
it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Terminal and non-terminal statuses for a delivery.
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_EXHAUSTED = "exhausted"
# Retries were cancelled with Retry-After: -1 rather than left to expire.
STATUS_CANCELLED = "cancelled"
# The same condition, with cancellation disabled: what *would* have been
# discarded. Counted so the safety of enabling it can be judged from evidence.
STATUS_WOULD_CANCEL = "would_cancel"

def default_state_path() -> Path:
    """Where the ledger lives, honouring HERMES_HOME.

    Defined here rather than in each caller: the adapter writes this file, and
    the CLI and dashboard read it. Three copies of the same expression is three
    chances for one of them to look at the wrong database and report an empty
    queue.
    """
    home = os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "hookdeck" / "state.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    event_id       TEXT PRIMARY KEY,
    route          TEXT NOT NULL DEFAULT '',
    attempt        INTEGER NOT NULL DEFAULT 0,
    agent_attempts INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    session_chat_id TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    first_seen     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_updated ON deliveries(updated_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
"""


@dataclass(frozen=True)
class Admission:
    """Outcome of asking the ledger whether to process a delivery."""

    admitted: bool
    agent_attempts: int
    reason: str = ""


class DeliveryLedger:
    """SQLite-backed record of every Hookdeck delivery this gateway has seen."""

    def __init__(self, path: str | Path):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock. Every statement here is a
        # single-row read or write on an indexed key, so the lock is held for
        # microseconds and never spans an await — cheaper and far easier to
        # reason about than a pool or a thread offload.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    @staticmethod
    def _admits(row: Optional[sqlite3.Row], attempt: int) -> tuple[bool, str]:
        """Whether *attempt* is new work, given what is already recorded.

        The rule: a delivery is new when its attempt number exceeds the highest
        already seen. Genuine duplicates repeat an attempt number; real retries
        increment it, which is what lets deduplication and retry coexist.

        Two exceptions. With no attempt number on the wire (``0``) the ledger
        falls back to admitting only after a recorded failure, so redelivery
        still works without opening a duplicate-run hole. And an ``exhausted``
        event is never admitted again: the retry budget is spent, and in sync
        mode Hookdeck's own retries would otherwise keep re-running it long
        past ``max_agent_retries``.
        """
        if row is None:
            return True, ""
        if row["status"] == STATUS_EXHAUSTED:
            return False, "retry budget already spent for this event"
        if attempt > int(row["attempt"]):
            return True, ""
        if attempt == 0 and row["status"] == STATUS_FAILED:
            return True, ""
        return False, f"duplicate delivery (attempt {attempt}, status {row['status']})"

    def is_duplicate(self, event_id: str, attempt: int) -> bool:
        """Read-only: would :meth:`admit` reject this delivery?

        Lets a caller recognise a duplicate *without* recording anything —
        which matters when the answer is "yes, and also we are at capacity",
        since a deferred event must leave no trace or its redelivery would be
        mistaken for a duplicate in turn.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt, status FROM deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        admitted, _ = self._admits(row, attempt)
        return not admitted

    def admit(
        self,
        event_id: str,
        *,
        route: str,
        attempt: int,
        session_chat_id: str = "",
    ) -> Admission:
        """Claim this delivery for an agent run, if it is new work.

        *attempt* is Hookdeck's attempt counter for the event; pass 0 when the
        header was absent. See :meth:`_admits` for the rule.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt, agent_attempts, status FROM deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()

            admitted, reason = self._admits(row, attempt)
            if not admitted:
                return Admission(False, int(row["agent_attempts"]), reason=reason)

            if row is None:
                self._conn.execute(
                    "INSERT INTO deliveries (event_id, route, attempt, agent_attempts,"
                    " status, session_chat_id, first_seen, updated_at)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                    (event_id, route, attempt, STATUS_RUNNING, session_chat_id, now, now),
                )
                self._conn.commit()
                return Admission(True, 1)

            agent_attempts = int(row["agent_attempts"]) + 1
            self._conn.execute(
                "UPDATE deliveries SET attempt = ?, agent_attempts = ?, status = ?,"
                " session_chat_id = ?, error = '', updated_at = ? WHERE event_id = ?",
                (
                    max(attempt, int(row["attempt"])),
                    agent_attempts,
                    STATUS_RUNNING,
                    session_chat_id,
                    now,
                    event_id,
                ),
            )
            self._conn.commit()
            return Admission(True, agent_attempts)

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------

    def _set_status(self, event_id: str, status: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE deliveries SET status = ?, error = ?, updated_at = ?"
                " WHERE event_id = ?",
                (status, error[:500], time.time(), event_id),
            )
            self._conn.commit()

    def mark_succeeded(self, event_id: str) -> None:
        self._set_status(event_id, STATUS_SUCCEEDED)

    def mark_failed(self, event_id: str, error: str = "") -> None:
        self._set_status(event_id, STATUS_FAILED, error)

    def mark_exhausted(self, event_id: str, error: str = "") -> None:
        self._set_status(event_id, STATUS_EXHAUSTED, error)

    def record_cancelled(
        self, event_id: str, reason: str, *, cancelled: bool = True
    ) -> None:
        """Note that retries were cancelled — or would have been.

        Both are recorded so ``hermes hookdeck status`` can show them: the
        first because discarding traffic must never be quiet, the second
        because "measure how often this would fire before enabling it" needs
        something to measure. The row is inserted if the event never got as far
        as being admitted.
        """
        now = time.time()
        status = STATUS_CANCELLED if cancelled else STATUS_WOULD_CANCEL
        with self._lock:
            self._conn.execute(
                "INSERT INTO deliveries (event_id, route, attempt, agent_attempts,"
                " status, session_chat_id, error, first_seen, updated_at)"
                " VALUES (?, '', 0, 0, ?, '', ?, ?, ?)"
                " ON CONFLICT(event_id) DO UPDATE SET status = excluded.status,"
                " error = excluded.error, updated_at = excluded.updated_at",
                (event_id, status, reason[:500], now, now),
            )
            self._conn.commit()

    def get(self, event_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM deliveries WHERE event_id = ?", (event_id,)
            ).fetchone()

    def agent_attempts(self, event_id: str) -> int:
        row = self.get(event_id)
        return int(row["agent_attempts"]) if row else 0

    # ------------------------------------------------------------------
    # Reporting and housekeeping
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def recent_failures(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM deliveries WHERE status IN (?, ?)"
                " ORDER BY updated_at DESC LIMIT ?",
                (STATUS_FAILED, STATUS_EXHAUSTED, limit),
            ).fetchall()

    def all_running(self) -> list[sqlite3.Row]:
        """Every delivery still marked running.

        At startup these are orphans by definition — the process that owned
        them is gone — which is what boot-time recovery reconciles.
        """
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM deliveries WHERE status = ? ORDER BY first_seen ASC",
                (STATUS_RUNNING,),
            ).fetchall()

    def stale_running(self, older_than_seconds: float) -> list[sqlite3.Row]:
        """Runs still marked running past *older_than_seconds*.

        After a hard crash these are the deliveries whose outcome was never
        recorded — ``hermes hookdeck doctor`` reports them so the operator can
        decide whether to replay.
        """
        cutoff = time.time() - older_than_seconds
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM deliveries WHERE status = ? AND updated_at < ?"
                " ORDER BY updated_at ASC",
                (STATUS_RUNNING, cutoff),
            ).fetchall()

    def prune(self, ttl_seconds: float) -> int:
        """Drop terminal rows older than *ttl_seconds*. Returns rows removed.

        Running rows are never pruned — losing one would let a redelivery start
        a second agent run for work that may still be in flight.
        """
        cutoff = time.time() - ttl_seconds
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM deliveries WHERE updated_at < ? AND status != ?",
                (cutoff, STATUS_RUNNING),
            )
            self._conn.commit()
            return cur.rowcount or 0
