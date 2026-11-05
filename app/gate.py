"""WORKTREE_GATE: a reader-writer gate between file writes and git.

docs/plan-mcp-git-ops.md, "Timeouts and locking". Every mutation of the shared
folder made through ``Vault`` (a rename into place, mkdir, delete, move) holds
the gate **shared** for the short critical section that actually changes the
tree. The git operations that read the whole tree for a commit, or rewrite it
for a pull/merge, hold it **exclusive**. A file write that arrives during an
exclusive hold waits, then fails with ``GateTimeout`` rather than interleaving
with a merge.

Writer-preferring, so a stream of small writes can't starve a pull, and
re-entrant for readers: a thread that already holds the gate shared (``move``
with ``create_parents`` calls ``mkdir`` from inside itself) re-enters without
waiting, which is what keeps a waiting writer from deadlocking it.

Single process only, like every other piece of in-memory state here (see the
comment above ``_SESSIONS`` in app/mcp_endpoint.py).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


class GateTimeout(Exception):
    """The gate could not be acquired in time."""


class WorktreeGate:
    def __init__(self, shared_timeout: float = 10.0) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer: int | None = None  # thread id holding it exclusive
        self._writers_waiting = 0
        self._local = threading.local()
        self.shared_timeout = shared_timeout

    # -- shared ------------------------------------------------------------

    @contextmanager
    def shared(self) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth > 0 or self._writer == threading.get_ident():
            # Already inside the gate on this thread (shared re-entry, or a
            # write performed by the thread that holds it exclusive).
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        deadline = time.monotonic() + self.shared_timeout
        with self._cond:
            while self._writer is not None or self._writers_waiting > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateTimeout(
                        "a git operation is updating the folder; retry in a few seconds"
                    )
                self._cond.wait(remaining)
            self._readers += 1
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    # -- exclusive ---------------------------------------------------------

    @contextmanager
    def exclusive(self, timeout: float = 30.0) -> Iterator[None]:
        me = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer is not None or self._readers > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GateTimeout(
                            "file writes are still in progress; retry in a few seconds"
                        )
                    self._cond.wait(remaining)
                self._writer = me
            finally:
                self._writers_waiting -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = None
                self._cond.notify_all()


class NullGate:
    """Used when git mode is off: no coordination, no overhead."""

    @contextmanager
    def shared(self) -> Iterator[None]:
        yield

    @contextmanager
    def exclusive(self, timeout: float = 30.0) -> Iterator[None]:
        yield
