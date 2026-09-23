"""One-time upload tickets.

An agent asks for a ticket naming a destination path, then pushes the file's
bytes directly with `curl` -- the bytes never pass through a model. See
``docs/upload-urls.md`` for the full design.

The container runs a single uvicorn worker (see ``Dockerfile``), so an
in-process dict guarded by a lock is enough; there is no second process for
it to fall out of sync with. Tickets are not persisted: they are meant to be
short-lived, and losing them on restart is fine -- the agent just requests a
new one.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

# Ticket states. "expired" is never stored -- it's computed at read time from
# "pending" plus the clock, so a ticket that nobody polled or uploaded to
# still reports the right thing right up until it's swept away.
PENDING = "pending"
RECEIVING = "receiving"
COMPLETE = "complete"
FAILED = "failed"
EXPIRED = "expired"


@dataclass
class UploadTicket:
    upload_id: str
    secret: str
    parts: list[str]
    overwrite: bool
    parents: bool
    max_bytes: int
    expect_sha256: str | None
    created_at: float
    expires_at: float
    state: str = PENDING
    bytes_received: int = 0
    size: int | None = None
    sha256: str | None = None
    error: dict[str, str] | None = None
    completed_at: float | None = None

    @property
    def path(self) -> str:
        return "/".join(self.parts)

    def effective_state(self, now: float | None = None) -> str:
        if self.state == PENDING and (now if now is not None else time.time()) >= self.expires_at:
            return EXPIRED
        return self.state


class TicketNotFound(Exception):
    """Bad upload_id or secret. Deliberately indistinguishable from the
    caller's side -- see the security considerations in
    docs/upload-urls.md."""


class TicketExpired(Exception):
    pass


class TicketNotPending(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class TicketLimitReached(Exception):
    pass


class UploadTickets:
    """All state for pending and in-flight upload tickets."""

    def __init__(self, *, ttl_seconds: float, max_tickets: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_tickets
        self._lock = threading.Lock()
        self._tickets: dict[str, UploadTicket] = {}

    def _sweep_locked(self, now: float) -> None:
        for upload_id, t in list(self._tickets.items()):
            if t.state in (COMPLETE, FAILED) or (t.state == PENDING and now >= t.expires_at):
                del self._tickets[upload_id]

    def create(
        self,
        *,
        parts: list[str],
        overwrite: bool,
        parents: bool,
        max_bytes: int,
        expect_sha256: str | None,
    ) -> UploadTicket:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if len(self._tickets) >= self._max:
                raise TicketLimitReached()
            upload_id = f"u_{secrets.token_urlsafe(16)}"
            while upload_id in self._tickets:
                upload_id = f"u_{secrets.token_urlsafe(16)}"
            ticket = UploadTicket(
                upload_id=upload_id,
                # 32 random bytes = 256 bits of entropy, compared with
                # hmac.compare_digest so a wrong guess costs no more timing
                # information than a right one.
                secret=secrets.token_urlsafe(32),
                parts=list(parts),
                overwrite=overwrite,
                parents=parents,
                max_bytes=max_bytes,
                expect_sha256=expect_sha256,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._tickets[upload_id] = ticket
            return ticket

    def get(self, upload_id: str) -> UploadTicket | None:
        with self._lock:
            return self._tickets.get(upload_id)

    def begin_receive(self, upload_id: str, secret: str) -> UploadTicket:
        """Atomically claim a pending ticket for the single upload it allows."""
        now = time.time()
        with self._lock:
            t = self._tickets.get(upload_id)
            if t is None or not hmac.compare_digest(secret, t.secret):
                raise TicketNotFound()
            if t.state == PENDING and now >= t.expires_at:
                raise TicketExpired()
            if t.state != PENDING:
                raise TicketNotPending(t.state)
            t.state = RECEIVING
            return t

    def update_progress(self, upload_id: str, bytes_received: int) -> None:
        with self._lock:
            t = self._tickets.get(upload_id)
            if t is not None:
                t.bytes_received = bytes_received

    def mark_complete(self, upload_id: str, *, size: int, sha256: str) -> None:
        with self._lock:
            t = self._tickets.get(upload_id)
            if t is not None:
                t.state = COMPLETE
                t.size = size
                t.sha256 = sha256
                t.bytes_received = size
                t.completed_at = time.time()

    def mark_failed(self, upload_id: str, *, code: str, message: str) -> None:
        with self._lock:
            t = self._tickets.get(upload_id)
            if t is not None:
                t.state = FAILED
                t.error = {"code": code, "message": message}
                t.completed_at = time.time()
