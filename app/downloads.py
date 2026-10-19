"""One-time download tickets: the read-side mirror of ``app.uploads``.

An agent asks for a ticket naming a file, then pulls the bytes directly with
`curl` -- the bytes never pass through a model. Same single-process,
in-memory design as upload tickets (see ``app/uploads.py`` and
``docs/download-urls.md``); the ticket exceptions are shared.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from .uploads import TicketExpired, TicketLimitReached, TicketNotFound, TicketNotPending

__all__ = [
    "DownloadTicket",
    "DownloadTickets",
    "TicketExpired",
    "TicketLimitReached",
    "TicketNotFound",
    "TicketNotPending",
]

PENDING = "pending"
STREAMING = "streaming"
COMPLETE = "complete"
FAILED = "failed"
EXPIRED = "expired"


@dataclass
class DownloadTicket:
    download_id: str
    secret: str
    parts: list[str]
    size: int
    mtime_ns: int
    sha256: str
    created_at: float
    expires_at: float
    state: str = PENDING
    bytes_sent: int = 0
    error: dict[str, str] | None = None
    completed_at: float | None = None

    @property
    def path(self) -> str:
        return "/".join(self.parts)

    def effective_state(self, now: float | None = None) -> str:
        if self.state == PENDING and (now if now is not None else time.time()) >= self.expires_at:
            return EXPIRED
        return self.state


class DownloadTickets:
    def __init__(self, *, ttl_seconds: float, max_tickets: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_tickets
        self._lock = threading.Lock()
        self._tickets: dict[str, DownloadTicket] = {}

    def _sweep_locked(self, now: float) -> None:
        for download_id, t in list(self._tickets.items()):
            if t.state in (COMPLETE, FAILED) or (t.state == PENDING and now >= t.expires_at):
                del self._tickets[download_id]

    def create(
        self, *, parts: list[str], size: int, mtime_ns: int, sha256: str
    ) -> DownloadTicket:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if len(self._tickets) >= self._max:
                raise TicketLimitReached()
            download_id = f"d_{secrets.token_urlsafe(16)}"
            while download_id in self._tickets:
                download_id = f"d_{secrets.token_urlsafe(16)}"
            ticket = DownloadTicket(
                download_id=download_id,
                secret=secrets.token_urlsafe(32),
                parts=list(parts),
                size=size,
                mtime_ns=mtime_ns,
                sha256=sha256,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._tickets[download_id] = ticket
            return ticket

    def get(self, download_id: str) -> DownloadTicket | None:
        with self._lock:
            return self._tickets.get(download_id)

    def begin_stream(self, download_id: str, secret: str) -> DownloadTicket:
        """Atomically claim a pending ticket for the single download it allows."""
        now = time.time()
        with self._lock:
            t = self._tickets.get(download_id)
            if t is None or not hmac.compare_digest(secret, t.secret):
                raise TicketNotFound()
            if t.state == PENDING and now >= t.expires_at:
                raise TicketExpired()
            if t.state != PENDING:
                raise TicketNotPending(t.state)
            t.state = STREAMING
            return t

    def update_progress(self, download_id: str, bytes_sent: int) -> None:
        with self._lock:
            t = self._tickets.get(download_id)
            if t is not None:
                t.bytes_sent = bytes_sent

    def mark_complete(self, download_id: str) -> None:
        with self._lock:
            t = self._tickets.get(download_id)
            if t is not None:
                t.state = COMPLETE
                t.bytes_sent = t.size
                t.completed_at = time.time()

    def mark_failed(self, download_id: str, *, code: str, message: str) -> None:
        with self._lock:
            t = self._tickets.get(download_id)
            if t is not None:
                t.state = FAILED
                t.error = {"code": code, "message": message}
                t.completed_at = time.time()
