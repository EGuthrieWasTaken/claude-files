"""claude-files: a minimal REST file API scoped to a single directory.

Designed to be the only thing an autonomous agent can reach on the host: the
container bind-mounts one subfolder, and every request is confined to it by
``app.safepath``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Config
from .downloads import DownloadTicket, DownloadTickets
from .gate import NullGate, WorktreeGate
from .git_ops import GitOpError, GitRepo
from .safepath import Entry, PathError, Vault
from .uploads import (
    TicketExpired,
    TicketLimitReached,
    TicketNotFound,
    TicketNotPending,
    UploadTicket,
    UploadTickets,
)

log = logging.getLogger("claude-files")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# The ticket URLs are `/v1/uploads/{id}/{secret}` and
# `/v1/downloads/{id}/{secret}` -- the secret in the path is the sole
# authorization for those routes (see docs/upload-urls.md and
# docs/download-urls.md), so it must never reach a log. Redact it from
# uvicorn's access log, which otherwise prints the raw request path verbatim.
_TICKET_SECRET_IN_PATH = re.compile(r"(/v1/(?:uploads|downloads)/[^/\s\"?]+)/[^/\s\"?]+")


class _RedactUploadSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TICKET_SECRET_IN_PATH.sub(r"\1/<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactUploadSecrets())

CONFIG = Config.from_env()

if CONFIG.require_mount and os.stat(CONFIG.root).st_dev == os.stat("/").st_dev:
    # DATA_ROOT is on the container's own root filesystem, which means the
    # bind mount is missing or misspelled. Refuse to start rather than
    # silently accept uploads into a directory that vanishes with the
    # container. Set REQUIRE_MOUNT=false only when running outside Docker.
    raise RuntimeError(
        f"{CONFIG.root} is not a mount point -- the volume is missing. "
        f"Bind-mount your shared folder to {CONFIG.root}, or set REQUIRE_MOUNT=false."
    )

# docs/plan-mcp-git-ops.md. Either switch turns git mode on; the gate is the
# reader-writer lock between file writes and git (a NullGate otherwise, so a
# deployment without git mode pays nothing for it).
_GIT_ENABLED = CONFIG.enable_git_read or CONFIG.enable_git_write
GATE = WorktreeGate() if _GIT_ENABLED else NullGate()

VAULT = Vault(
    CONFIG.root,
    denied_names=CONFIG.denied_names,
    allow_hardlinked_files=CONFIG.allow_hardlinked_files,
    gate=GATE,
)

TICKETS = UploadTickets(
    ttl_seconds=CONFIG.upload_ticket_ttl_seconds,
    max_tickets=CONFIG.upload_tickets_max,
)

DOWNLOAD_TICKETS = DownloadTickets(
    ttl_seconds=CONFIG.download_ticket_ttl_seconds,
    max_tickets=CONFIG.download_tickets_max,
)


def _uploads_in_flight() -> list[str]:
    """Paths an upload could still be writing to: pending/receiving tickets and
    open chunked sessions. git_pull / git_merge refuse while any exist.
    `_mcp_endpoint` is a module global set at the bottom of this file."""
    paths = [t.path for t in TICKETS.active()]
    if _mcp_endpoint is not None:
        paths += _mcp_endpoint.active_upload_paths()
    return sorted(set(paths))


GIT: GitRepo | None = None
if _GIT_ENABLED:
    # Validates the whole deployment (layout, .git denied, config origins,
    # remote URL, credentials, mount mode) and raises RuntimeError with a fix
    # if anything is wrong -- the container refuses to start rather than run
    # git in a state nobody has checked, exactly like the mount check above.
    GIT = GitRepo(CONFIG, vault=VAULT, gate=GATE, uploads_in_flight=_uploads_in_flight)
    GIT.start()

DESCRIPTION = """
A REST API for reading and writing files inside a single shared folder.

Every path is relative to the shared folder root. Absolute paths, `..`
segments, and symlinks are rejected -- there is no path that reaches outside
the shared folder.

Authenticate with `Authorization: Bearer <token>`.
"""


@asynccontextmanager
async def _mcp_session_scope():
    """Enter the MCP SDK's own session-manager lifespan, if /mcp is mounted.

    Mounting mcp_endpoint's sub-app does not run its lifespan on its own --
    Starlette does not forward ASGI lifespan events into a mounted
    sub-application -- so it has to be entered explicitly, from this app's
    own lifespan. Skipping this produces a server that starts fine and then
    fails on the first /mcp tool call with "Task group is not initialized."
    A no-op when the mount is absent (not installed, or ENABLE_MCP=false) --
    see the ImportError guard at the bottom of this file, which is what sets
    `_mcp_endpoint` and runs before this function is ever awaited.
    """
    if _mcp_endpoint is not None:
        async with _mcp_endpoint.mcp.session_manager.run():
            yield
    else:
        yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    swept = VAULT.sweep_temp_files()
    if swept:
        log.info("removed %d stale upload temp file(s)", swept)
    log.info(
        "claude-files serving %s (delete=%s, max_upload=%d, denied=%s)",
        VAULT.root_path,
        CONFIG.allow_delete,
        CONFIG.max_upload_bytes,
        sorted(CONFIG.denied_names),
    )
    async with _mcp_session_scope():
        yield


app = FastAPI(
    title="claude-files",
    description=DESCRIPTION,
    version="1.1.0",
    docs_url="/docs" if CONFIG.enable_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if CONFIG.enable_docs else None,
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class EntryModel(BaseModel):
    name: str
    path: str
    type: str = Field(description="file | dir | symlink | other")
    size: int | None = None
    mtime: float | None = None
    readable: bool = Field(description="False for symlinks and other non-regular files")


class ListResponse(BaseModel):
    path: str
    entries: list[EntryModel]
    truncated: bool = False


class MoveRequest(BaseModel):
    src: str = Field(description="Source path, relative to the shared folder")
    dst: str = Field(description="Destination path, relative to the shared folder")
    overwrite: bool = False
    parents: bool = Field(
        default=False, description="Create missing parent directories of dst"
    )


class CopyRequest(BaseModel):
    src: str = Field(description="Source path, relative to the shared folder")
    dst: str = Field(description="Destination path, relative to the shared folder")
    overwrite: bool = False
    parents: bool = Field(
        default=False, description="Create missing parent directories of dst"
    )


class OkResponse(BaseModel):
    ok: bool = True
    path: str = ""
    size: int | None = None
    created: bool | None = None
    sha256: str | None = None


class UploadCreateRequest(BaseModel):
    path: str = Field(description="Destination path, relative to the shared folder")
    overwrite: bool = Field(default=True, description="If false, 409 now when the file already exists")
    parents: bool = Field(default=True, description="Create missing parent directories at upload time")
    max_bytes: int | None = Field(
        default=None, description="Optional per-ticket cap; clamped to MAX_UPLOAD_BYTES"
    )
    expect_sha256: str | None = Field(
        default=None,
        description="Optional. If set, the upload is rejected and nothing is written unless it matches.",
    )


class UploadCreateResponse(BaseModel):
    upload_id: str
    upload_url: str
    method: str = "PUT"
    expires_at: float
    max_bytes: int
    curl_command: str


class UploadErrorModel(BaseModel):
    code: str
    message: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    path: str
    state: str = Field(description="pending | receiving | complete | failed | expired")
    bytes_received: int
    size: int | None = None
    sha256: str | None = None
    error: UploadErrorModel | None = None
    created_at: float
    expires_at: float
    completed_at: float | None = None


class UploadResultResponse(BaseModel):
    ok: bool = True
    upload_id: str
    path: str
    size: int
    sha256: str


class DownloadCreateRequest(BaseModel):
    path: str = Field(description="File to download, relative to the shared folder")


class DownloadCreateResponse(BaseModel):
    download_id: str
    download_url: str
    method: str = "GET"
    path: str
    size: int
    sha256: str
    expires_at: float
    curl_command: str


class DownloadStatusResponse(BaseModel):
    download_id: str
    path: str
    state: str = Field(description="pending | streaming | complete | failed | expired")
    bytes_sent: int
    size: int
    sha256: str
    error: UploadErrorModel | None = None
    created_at: float
    expires_at: float
    completed_at: float | None = None


def _to_model(e: Entry) -> EntryModel:
    return EntryModel(
        name=e.name, path=e.path, type=e.type, size=e.size, mtime=e.mtime, readable=e.readable
    )


# --------------------------------------------------------------------------
# Auth + rate limiting
# --------------------------------------------------------------------------

_hits: deque[float] = deque()


def _rate_limit() -> None:
    if CONFIG.rate_limit_per_minute <= 0:
        return
    now = time.monotonic()
    while _hits and now - _hits[0] > 60.0:
        _hits.popleft()
    if len(_hits) >= CONFIG.rate_limit_per_minute:
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Too many requests"})
    _hits.append(now)


def _check_token(authorization: str | None, api_key: str | None = None) -> str:
    """Return "rw" or "ro".

    Accepts `Authorization: Bearer <token>` (the documented REST credential)
    or `X-API-Key: <token>` (what the claude.ai custom-connector dialog
    sends -- it offers a static header with `x-api-key` as a preset name,
    the arrangement the sibling meeting-transcriber connector already runs
    on). If `Authorization` is present it wins and `X-API-Key` is ignored --
    never "try each until one matches", which would turn one request into
    two guesses against the rate limiter.
    """
    presented = None
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:].strip()
    elif api_key:
        presented = api_key.strip()
    if presented is None:
        raise HTTPException(
            401,
            detail={"code": "unauthorized", "message": "Missing bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if hmac.compare_digest(presented, CONFIG.token):
        return "rw"
    if CONFIG.readonly_token and hmac.compare_digest(presented, CONFIG.readonly_token):
        return "ro"
    raise HTTPException(
        401,
        detail={"code": "unauthorized", "message": "Invalid token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_read(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    _rate_limit()
    return _check_token(authorization, x_api_key)


READONLY_MESSAGE = (
    "This token is read-only: it can list, stat, and read files, but every "
    "write, delete, move, copy, and upload is refused. Retrying won't help. "
    "If this task needs to write, ask the user for a read-write token "
    "(API_TOKEN). get_folder_info (MCP) or GET /v1/info (REST) reports a "
    "token's scope as token_scope."
)


def require_write(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    _rate_limit()
    scope = _check_token(authorization, x_api_key)
    if scope != "rw":
        raise HTTPException(
            403, detail={"code": "readonly", "message": READONLY_MESSAGE}
        )
    return scope


ReadAuth = Annotated[str, Depends(require_read)]
WriteAuth = Annotated[str, Depends(require_write)]


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------

@app.exception_handler(PathError)
async def _path_error_handler(request: Request, exc: PathError) -> JSONResponse:
    # Log every rejection: an escape attempt should be visible in the logs.
    log.warning(
        "denied %s %s -> %s (%s)", request.method, request.url.path, exc.code, exc.message
    )
    return JSONResponse(
        status_code=exc.status, content={"code": exc.code, "message": exc.message}
    )


@app.exception_handler(GitOpError)
async def _git_error_handler(request: Request, exc: GitOpError) -> JSONResponse:
    log.warning(
        "denied %s %s -> %s (%s)", request.method, request.url.path, exc.code,
        exc.message.splitlines()[0] if exc.message else "",
    )
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


@app.exception_handler(HTTPException)
async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail: Any = exc.detail
    if not isinstance(detail, dict):
        detail = {"code": "error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)


@app.middleware("http")
async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health", tags=["meta"], summary="Liveness probe (no auth)")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "claude-files", "version": app.version}


def _bridge_script_path() -> str | None:
    """Where bridge/claude_files_bridge.py lives, in the image or a source
    checkout. Fixed paths only -- no request input ever reaches this lookup.
    See GET /v1/bridge.py, below."""
    for candidate in (
        "/srv/bridge/claude_files_bridge.py",
        str(Path(__file__).resolve().parents[1] / "bridge" / "claude_files_bridge.py"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


@app.get("/v1/info", tags=["meta"], summary="Server limits and policy")
def info(scope: ReadAuth) -> dict[str, Any]:
    return {
        "token_scope": scope,
        "writes_allowed": scope == "rw",
        "max_upload_bytes": CONFIG.max_upload_bytes,
        "max_list_entries": CONFIG.max_list_entries,
        "denied_names": sorted(CONFIG.denied_names),
        "allow_delete": CONFIG.allow_delete,
        "allow_hardlinked_files": CONFIG.allow_hardlinked_files,
        "mcp_enabled": _mcp_endpoint is not None,
        "bridge_available": _bridge_script_path() is not None,
        "upload_ticket_ttl_seconds": CONFIG.upload_ticket_ttl_seconds,
        "upload_tickets_max": CONFIG.upload_tickets_max,
        "download_ticket_ttl_seconds": CONFIG.download_ticket_ttl_seconds,
        "download_tickets_max": CONFIG.download_tickets_max,
        "git": GIT.info_block() if GIT is not None else GitRepo.disabled_info(),
        "extensions": _mcp_endpoint.extensions_info() if _mcp_endpoint is not None else None,
    }


@app.get("/v1/bridge.py", tags=["meta"], summary="Download the local upload bridge")
def bridge_script(_: ReadAuth) -> Response:
    """docs/feature-request-path-based-upload.md, Part 2b: hand out
    bridge/claude_files_bridge.py so an agent's own environment can stream
    bytes to PUT /v1/content/{path} directly, never through its own output.
    Read auth, same as every other route -- the content is open-source
    client code, but an unauthenticated route is a new unauthenticated
    surface, and /health is deliberately the only one."""
    path = _bridge_script_path()
    if path is None:
        raise HTTPException(
            503,
            detail={
                "code": "bridge_unavailable",
                "message": "bridge/claude_files_bridge.py is not packaged in this image",
            },
        )
    with open(path, "rb") as f:
        data = f.read()
    return Response(
        content=data,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="claude_files_bridge.py"'},
    )


@app.get("/v1/list", response_model=ListResponse, tags=["read"], summary="List the root")
@app.get("/v1/list/{path:path}", response_model=ListResponse, tags=["read"], summary="List a directory")
def list_dir(
    _: ReadAuth,
    path: str = "",
    recursive: bool = Query(False, description="List all descendants"),
    limit: int = Query(1000, ge=1, le=100_000),
) -> ListResponse:
    parts = VAULT.components(path)
    cap = min(limit, CONFIG.max_list_entries)
    entries = VAULT.walk(parts, max_entries=cap) if recursive else VAULT.listdir(parts)
    return ListResponse(
        path="/".join(parts),
        entries=[_to_model(e) for e in entries[:cap]],
        truncated=len(entries) >= cap,
    )


@app.get("/v1/stat/{path:path}", response_model=EntryModel, tags=["read"], summary="Metadata for one path")
def stat_path(_: ReadAuth, path: str) -> EntryModel:
    return _to_model(VAULT.stat(VAULT.components(path)))


def _stream_fd(fd: int, chunk: int = 256 * 1024) -> Iterator[bytes]:
    try:
        while True:
            data = os.read(fd, chunk)
            if not data:
                return
            yield data
    finally:
        os.close(fd)


@app.get("/v1/content/{path:path}", tags=["read"], summary="Download a file")
def download(_: ReadAuth, path: str) -> StreamingResponse:
    parts = VAULT.components(path)
    fd, st = VAULT.open_read(parts)
    filename = parts[-1].replace('"', "")
    return StreamingResponse(
        _stream_fd(fd),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(st.st_size),
            # Always an attachment: this API must never be a vector for
            # serving active content from the host's origin.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-File-Size": str(st.st_size),
            "X-File-Mtime": str(st.st_mtime),
        },
    )


@app.head("/v1/content/{path:path}", tags=["read"], summary="File metadata as headers")
def head_content(_: ReadAuth, path: str) -> Response:
    entry = VAULT.stat(VAULT.components(path))
    if entry.type != "file" or not entry.readable:
        raise PathError(404, "not_found", "Not a readable regular file")
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(entry.size or 0),
            "X-File-Size": str(entry.size or 0),
            "X-File-Mtime": str(entry.mtime or 0),
        },
    )


_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _parse_expect_bytes(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(
            400, detail={"code": "bad_request", "message": "Invalid X-Expect-Bytes header"}
        ) from None
    return value


def _parse_expect_sha256(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not _SHA256_HEX_RE.match(raw):
        raise HTTPException(
            400,
            detail={
                "code": "bad_request",
                "message": "Invalid X-Expect-Sha256 header (expected 64 hex characters)",
            },
        )
    return raw.lower()


async def _write_body(
    request: Request, parts: list[str], *, overwrite: bool, create_parents: bool
) -> tuple[int, str]:
    """Stream the request body into place. Returns (bytes written, sha256 hex).

    docs/feature-request-path-based-upload.md, Part 2a: optional
    X-Expect-Bytes / X-Expect-Sha256 headers are verified against what was
    actually streamed, AFTER the stream and BEFORE committer.commit(fd) --
    on a mismatch nothing is renamed into place, so a truncated-then-closed
    proxy response or a caller sending the wrong file becomes a clean 422
    refusal instead of a silently wrong file. The sha256 is computed
    unconditionally (it costs microseconds per MB) so every successful
    caller gets one back in OkResponse, not just verifying ones.
    """
    declared = request.headers.get("content-length")
    declared_int: int | None = None
    if declared is not None:
        try:
            declared_int = int(declared)
        except ValueError:
            raise HTTPException(
                400, detail={"code": "bad_request", "message": "Invalid Content-Length"}
            ) from None
        if declared_int > CONFIG.max_upload_bytes:
            raise HTTPException(
                413,
                detail={
                    "code": "too_large",
                    "message": f"Body exceeds MAX_UPLOAD_BYTES ({CONFIG.max_upload_bytes})",
                },
            )

    expect_bytes = _parse_expect_bytes(request.headers.get("x-expect-bytes"))
    expect_sha256 = _parse_expect_sha256(request.headers.get("x-expect-sha256"))
    if expect_bytes is not None and declared_int is not None and expect_bytes != declared_int:
        raise HTTPException(
            400,
            detail={
                "code": "bad_request",
                "message": (
                    f"X-Expect-Bytes ({expect_bytes}) does not match Content-Length "
                    f"({declared_int})"
                ),
            },
        )

    written = 0
    hasher = hashlib.sha256()
    with VAULT.atomic_write(
        parts, overwrite=overwrite, create_parents=create_parents
    ) as (fd, committer):
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > CONFIG.max_upload_bytes:
                raise HTTPException(
                    413,
                    detail={
                        "code": "too_large",
                        "message": f"Body exceeds MAX_UPLOAD_BYTES ({CONFIG.max_upload_bytes})",
                    },
                )
            hasher.update(chunk)
            os.write(fd, chunk)
        digest = hasher.hexdigest()
        if expect_bytes is not None and expect_bytes != written:
            raise HTTPException(
                422,
                detail={
                    "code": "integrity_mismatch",
                    "message": (
                        f"Received {written} bytes but X-Expect-Bytes said {expect_bytes}"
                    ),
                },
            )
        if expect_sha256 is not None and expect_sha256 != digest:
            raise HTTPException(
                422,
                detail={
                    "code": "integrity_mismatch",
                    "message": (
                        f"Computed sha256 {digest} does not match X-Expect-Sha256 {expect_sha256}"
                    ),
                },
            )
        committer.commit(fd)
    return written, digest


@app.put("/v1/content/{path:path}", response_model=OkResponse, tags=["write"],
         summary="Upload or replace a file (raw body)")
async def put_content(
    request: Request,
    path: str,
    _: WriteAuth,
    parents: bool = Query(
        False,
        description=(
            "Create any missing parent directories first, like `mkdir -p`. "
            "Without this, writing into a directory that does not exist "
            "fails with 409 no_parent."
        ),
    ),
) -> OkResponse:
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot write to the root itself")
    size, digest = await _write_body(request, parts, overwrite=True, create_parents=parents)
    log.info("wrote %s (%d bytes)", "/".join(parts), size)
    return OkResponse(path="/".join(parts), size=size, created=True, sha256=digest)


@app.post("/v1/content/{path:path}", response_model=OkResponse, tags=["write"],
          summary="Create a file; 409 if it already exists")
async def post_content(
    request: Request,
    path: str,
    _: WriteAuth,
    parents: bool = Query(
        False,
        description=(
            "Create any missing parent directories first, like `mkdir -p`. "
            "Without this, writing into a directory that does not exist "
            "fails with 409 no_parent."
        ),
    ),
) -> OkResponse:
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot write to the root itself")
    size, digest = await _write_body(request, parts, overwrite=False, create_parents=parents)
    log.info("created %s (%d bytes)", "/".join(parts), size)
    return OkResponse(path="/".join(parts), size=size, created=True, sha256=digest)


@app.delete("/v1/content/{path:path}", response_model=OkResponse, tags=["write"],
            summary="Delete a file or directory")
def delete_content(
    path: str,
    _: WriteAuth,
    recursive: bool = Query(False, description="Required to delete a non-empty directory"),
) -> OkResponse:
    if not CONFIG.allow_delete:
        raise PathError(403, "delete_disabled", "Deletion is disabled on this server")
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot delete the root directory")
    VAULT.delete(parts, recursive=recursive)
    log.info("deleted %s (recursive=%s)", "/".join(parts), recursive)
    return OkResponse(path="/".join(parts))


@app.post("/v1/mkdir/{path:path}", response_model=OkResponse, tags=["write"],
          summary="Create a directory")
def mkdir(
    path: str,
    _: WriteAuth,
    parents: bool = Query(True, description="Create intermediate directories"),
) -> OkResponse:
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot create the root directory")
    created = VAULT.mkdir(parts, parents=parents)
    return OkResponse(path="/".join(parts), created=created)


@app.post("/v1/move", response_model=OkResponse, tags=["write"],
          summary="Move or rename within the shared folder")
def move(req: MoveRequest, _: WriteAuth) -> OkResponse:
    src = VAULT.components(req.src)
    dst = VAULT.components(req.dst)
    VAULT.move(src, dst, overwrite=req.overwrite, create_parents=req.parents)
    log.info("moved %s -> %s", "/".join(src), "/".join(dst))
    return OkResponse(path="/".join(dst))


def _public_base_url(request: Request | None) -> str:
    """Base URL for a ticket's upload_url / download_url: PUBLIC_BASE_URL, else
    MCP_PUBLIC_HOSTNAME, else whatever the (proxied) request says."""
    if CONFIG.public_base_url:
        return CONFIG.public_base_url.rstrip("/")
    if CONFIG.mcp_public_hostname:
        return f"https://{CONFIG.mcp_public_hostname}"
    if request is None:
        raise PathError(
            500, "no_base_url",
            "Cannot build a ticket URL: set PUBLIC_BASE_URL on the server",
        )
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def issue_upload_ticket(
    path: str,
    *,
    overwrite: bool,
    parents: bool,
    max_bytes: int | None,
    expect_sha256: str | None,
    request: Request | None,
) -> UploadCreateResponse:
    """Validate the destination and mint a one-time upload ticket.

    Shared by POST /v1/uploads and the MCP request_upload tool, so both
    front ends apply identical checks. Every refusal is a PathError, which
    each front end already logs and translates.
    """
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot upload to the root itself")
    VAULT.precheck_new_path(parts)

    if not overwrite:
        try:
            VAULT.stat(parts)
        except PathError as exc:
            if exc.code != "not_found":
                raise
        else:
            raise PathError(409, "conflict", "File already exists")

    digest = None
    if expect_sha256 is not None:
        digest = expect_sha256.strip().lower()
        if not _SHA256_RE.match(digest):
            raise PathError(400, "bad_request", "expect_sha256 must be 64 hex characters")

    cap = CONFIG.max_upload_bytes
    if max_bytes is not None:
        if max_bytes <= 0:
            raise PathError(400, "bad_request", "max_bytes must be positive")
        cap = min(max_bytes, CONFIG.max_upload_bytes)

    base_url = _public_base_url(request)
    try:
        ticket = TICKETS.create(
            parts=parts,
            overwrite=overwrite,
            parents=parents,
            max_bytes=cap,
            expect_sha256=digest,
        )
    except TicketLimitReached:
        raise PathError(
            503, "too_many_uploads", "Too many pending uploads; try again shortly"
        ) from None

    upload_url = f"{base_url}/v1/uploads/{ticket.upload_id}/{ticket.secret}"
    log.info("upload ticket %s created for %s", ticket.upload_id, ticket.path)
    return UploadCreateResponse(
        upload_id=ticket.upload_id,
        upload_url=upload_url,
        expires_at=ticket.expires_at,
        max_bytes=ticket.max_bytes,
        curl_command=f"curl -sS --fail-with-body -T <LOCAL_FILE> '{upload_url}'",
    )


def upload_ticket_status(upload_id: str) -> UploadStatusResponse:
    ticket = TICKETS.get(upload_id)
    if ticket is None:
        raise PathError(404, "not_found", "No such upload")
    return UploadStatusResponse(
        upload_id=ticket.upload_id,
        path=ticket.path,
        state=ticket.effective_state(),
        bytes_received=ticket.bytes_received,
        size=ticket.size,
        sha256=ticket.sha256,
        error=UploadErrorModel(**ticket.error) if ticket.error else None,
        created_at=ticket.created_at,
        expires_at=ticket.expires_at,
        completed_at=ticket.completed_at,
    )


@app.post("/v1/uploads", response_model=UploadCreateResponse, status_code=201, tags=["write"],
          summary="Create a one-time upload ticket")
def create_upload(req: UploadCreateRequest, request: Request, _: WriteAuth) -> UploadCreateResponse:
    return issue_upload_ticket(
        req.path,
        overwrite=req.overwrite,
        parents=req.parents,
        max_bytes=req.max_bytes,
        expect_sha256=req.expect_sha256,
        request=request,
    )


@app.get("/v1/uploads/{upload_id}", response_model=UploadStatusResponse, tags=["read"],
         summary="Check an upload ticket's status")
def upload_status(_: ReadAuth, upload_id: str) -> UploadStatusResponse:
    return upload_ticket_status(upload_id)


async def _stream_ticket_upload(request: Request, ticket: UploadTicket) -> tuple[int, str]:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > ticket.max_bytes:
                raise HTTPException(
                    413,
                    detail={
                        "code": "too_large",
                        "message": f"Body exceeds the ticket's max_bytes ({ticket.max_bytes})",
                    },
                )
        except ValueError:
            raise HTTPException(
                400, detail={"code": "bad_request", "message": "Invalid Content-Length"}
            ) from None

    hasher = hashlib.sha256()
    written = 0
    with VAULT.atomic_write(
        ticket.parts, overwrite=ticket.overwrite, create_parents=ticket.parents
    ) as (fd, committer):
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > ticket.max_bytes:
                raise HTTPException(
                    413,
                    detail={
                        "code": "too_large",
                        "message": f"Body exceeds the ticket's max_bytes ({ticket.max_bytes})",
                    },
                )
            hasher.update(chunk)
            os.write(fd, chunk)
            TICKETS.update_progress(ticket.upload_id, written)

        digest = hasher.hexdigest()
        if ticket.expect_sha256 and not hmac.compare_digest(digest, ticket.expect_sha256):
            # Raising here, before committer.commit(fd), leaves the temp file
            # to be cleaned up by atomic_write's `finally` and the target
            # untouched.
            raise HTTPException(
                422,
                detail={
                    "code": "sha_mismatch",
                    "message": f"Uploaded bytes hash to {digest}, expected {ticket.expect_sha256}",
                },
            )
        committer.commit(fd)
    return written, digest


@app.put("/v1/uploads/{upload_id}/{secret}", response_model=UploadResultResponse, tags=["write"],
         summary="Upload the bytes for a ticket (no bearer token; the secret is the authorization)")
@app.post("/v1/uploads/{upload_id}/{secret}", response_model=UploadResultResponse, tags=["write"],
          summary="Upload the bytes for a ticket (alias for clients that can't send PUT)")
async def upload_bytes(request: Request, upload_id: str, secret: str) -> UploadResultResponse:
    _rate_limit()
    try:
        ticket = TICKETS.begin_receive(upload_id, secret)
    except TicketNotFound:
        log.warning("denied %s /v1/uploads/%s/<secret> -> not_found", request.method, upload_id)
        raise HTTPException(404, detail={"code": "not_found", "message": "No such upload"}) from None
    except TicketExpired:
        raise HTTPException(
            410, detail={"code": "expired", "message": "Upload ticket has expired"}
        ) from None
    except TicketNotPending as exc:
        raise HTTPException(
            409,
            detail={"code": "conflict", "message": f"Upload ticket already {exc.state}"},
        ) from None

    try:
        size, digest = await _stream_ticket_upload(request, ticket)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        TICKETS.mark_failed(ticket.upload_id, code=detail.get("code", "error"), message=detail.get("message", ""))
        log.warning("upload %s failed: %s", ticket.upload_id, detail.get("code"))
        raise
    except PathError as exc:
        # Translated to HTTPException (rather than re-raised) so this never
        # reaches the PathError handler, which logs request.url.path -- and
        # this route's path contains the ticket's one-time secret.
        TICKETS.mark_failed(ticket.upload_id, code=exc.code, message=exc.message)
        log.warning("upload %s failed: %s (%s)", ticket.upload_id, exc.code, exc.message)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    except Exception as exc:
        TICKETS.mark_failed(ticket.upload_id, code="io_error", message=str(exc))
        log.exception("upload %s failed unexpectedly", ticket.upload_id)
        raise HTTPException(
            500, detail={"code": "io_error", "message": "Internal error while receiving the upload"}
        ) from exc

    TICKETS.mark_complete(ticket.upload_id, size=size, sha256=digest)
    log.info("upload %s complete: %s (%d bytes)", ticket.upload_id, ticket.path, size)
    return UploadResultResponse(upload_id=ticket.upload_id, path=ticket.path, size=size, sha256=digest)


# --------------------------------------------------------------------------
# One-time download tickets -- docs/download-urls.md
# --------------------------------------------------------------------------

def issue_download_ticket(path: str, *, request: Request | None) -> DownloadCreateResponse:
    """Validate a file and mint a one-time download ticket.

    Shared by POST /v1/downloads and the MCP request_download tool. Hashes the
    file now so the caller gets `sha256` up front and can verify the bytes
    after curl; the download re-checks size and mtime so a file replaced in
    between is refused rather than served with a stale hash.
    """
    parts = VAULT.components(path)
    if not parts:
        raise PathError(403, "is_root", "Cannot download the root itself")

    fd, st = VAULT.open_read(parts)
    hasher = hashlib.sha256()
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(fd)

    base_url = _public_base_url(request)
    try:
        ticket = DOWNLOAD_TICKETS.create(
            parts=parts, size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=hasher.hexdigest()
        )
    except TicketLimitReached:
        raise PathError(
            503, "too_many_downloads", "Too many pending downloads; try again shortly"
        ) from None

    download_url = f"{base_url}/v1/downloads/{ticket.download_id}/{ticket.secret}"
    log.info("download ticket %s created for %s", ticket.download_id, ticket.path)
    return DownloadCreateResponse(
        download_id=ticket.download_id,
        download_url=download_url,
        path=ticket.path,
        size=ticket.size,
        sha256=ticket.sha256,
        expires_at=ticket.expires_at,
        curl_command=f"curl -sS --fail-with-body -o <OUTPUT_FILE> '{download_url}'",
    )


def download_ticket_status(download_id: str) -> DownloadStatusResponse:
    ticket = DOWNLOAD_TICKETS.get(download_id)
    if ticket is None:
        raise PathError(404, "not_found", "No such download")
    return DownloadStatusResponse(
        download_id=ticket.download_id,
        path=ticket.path,
        state=ticket.effective_state(),
        bytes_sent=ticket.bytes_sent,
        size=ticket.size,
        sha256=ticket.sha256,
        error=UploadErrorModel(**ticket.error) if ticket.error else None,
        created_at=ticket.created_at,
        expires_at=ticket.expires_at,
        completed_at=ticket.completed_at,
    )


@app.post("/v1/downloads", response_model=DownloadCreateResponse, status_code=201, tags=["read"],
          summary="Create a one-time download ticket (read-only tokens allowed)")
def create_download(
    req: DownloadCreateRequest, request: Request, _: ReadAuth
) -> DownloadCreateResponse:
    return issue_download_ticket(req.path, request=request)


@app.get("/v1/downloads/{download_id}", response_model=DownloadStatusResponse, tags=["read"],
         summary="Check a download ticket's status")
def download_status(_: ReadAuth, download_id: str) -> DownloadStatusResponse:
    return download_ticket_status(download_id)


def _stream_ticket_download(fd: int, ticket: DownloadTicket) -> Iterator[bytes]:
    sent = 0
    try:
        while True:
            data = os.read(fd, 256 * 1024)
            if not data:
                break
            sent += len(data)
            DOWNLOAD_TICKETS.update_progress(ticket.download_id, sent)
            yield data
    finally:
        os.close(fd)
        if sent == ticket.size:
            DOWNLOAD_TICKETS.mark_complete(ticket.download_id)
            log.info("download %s complete: %s (%d bytes)", ticket.download_id, ticket.path, sent)
        else:
            DOWNLOAD_TICKETS.mark_failed(
                ticket.download_id,
                code="interrupted",
                message=f"Sent {sent} of {ticket.size} bytes before the transfer ended",
            )
            log.warning("download %s interrupted at %d/%d bytes", ticket.download_id, sent, ticket.size)


@app.get("/v1/downloads/{download_id}/{secret}", tags=["read"],
         summary="Download the bytes for a ticket (no bearer token; the secret is the authorization)")
def download_bytes(download_id: str, secret: str, request: Request) -> StreamingResponse:
    _rate_limit()
    try:
        ticket = DOWNLOAD_TICKETS.begin_stream(download_id, secret)
    except TicketNotFound:
        log.warning("denied %s /v1/downloads/%s/<secret> -> not_found", request.method, download_id)
        raise HTTPException(404, detail={"code": "not_found", "message": "No such download"}) from None
    except TicketExpired:
        raise HTTPException(
            410, detail={"code": "expired", "message": "Download ticket has expired"}
        ) from None
    except TicketNotPending as exc:
        raise HTTPException(
            409,
            detail={"code": "conflict", "message": f"Download ticket already {exc.state}"},
        ) from None

    # Same as upload_bytes: PathError is translated to HTTPException so it
    # never reaches the PathError handler, which logs a path carrying the secret.
    try:
        fd, st = VAULT.open_read(ticket.parts)
    except PathError as exc:
        DOWNLOAD_TICKETS.mark_failed(ticket.download_id, code=exc.code, message=exc.message)
        log.warning("download %s failed: %s (%s)", ticket.download_id, exc.code, exc.message)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc

    if st.st_size != ticket.size or st.st_mtime_ns != ticket.mtime_ns:
        os.close(fd)
        message = "File changed after the ticket was issued; request a new download"
        DOWNLOAD_TICKETS.mark_failed(ticket.download_id, code="file_changed", message=message)
        log.warning("download %s failed: file_changed", ticket.download_id)
        raise HTTPException(409, detail={"code": "file_changed", "message": message})

    filename = ticket.parts[-1].replace('"', "")
    return StreamingResponse(
        _stream_ticket_download(fd, ticket),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(ticket.size),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-File-Size": str(ticket.size),
            "X-File-Sha256": ticket.sha256,
        },
    )


@app.post("/v1/copy", response_model=OkResponse, tags=["write"],
          summary="Copy a file within the shared folder")
def copy(req: CopyRequest, _: WriteAuth) -> OkResponse:
    src = VAULT.components(req.src)
    dst = VAULT.components(req.dst)
    size = VAULT.copy(src, dst, overwrite=req.overwrite, create_parents=req.parents)
    log.info("copied %s -> %s (%d bytes)", "/".join(src), "/".join(dst), size)
    return OkResponse(path="/".join(dst), size=size, created=True)


# --------------------------------------------------------------------------
# Git -- docs/plan-mcp-git-ops.md
#
# One route per MCP tool. A disabled class answers 404 git_disabled. The
# handlers are plain `def`, so FastAPI runs them in its threadpool: a git push
# can take a minute and must not stall the event loop.
# --------------------------------------------------------------------------

class GitCommitRequest(BaseModel):
    message: str = Field(description="The commit message.")
    paths: list[str] | None = Field(
        default=None,
        description="Folder-relative paths to commit. Omit to commit everything changed in the folder.",
    )


class GitPullRequest(BaseModel):
    strategy: str = Field(default="ff-only", description="ff-only, merge, or rebase.")


class GitMergeRequest(BaseModel):
    ref: str = Field(description="A local branch name, or '<remote>/<branch>'.")
    no_ff: bool = False


def _git_read() -> GitRepo:
    if GIT is None or not GIT.read_enabled:
        raise GitOpError("git_disabled", "git read tools are disabled on this server (ENABLE_GIT_READ=false)")
    return GIT


def _git_write() -> GitRepo:
    if GIT is None or not GIT.write_enabled:
        raise GitOpError("git_disabled", "git write tools are disabled on this server (ENABLE_GIT_WRITE=false)")
    return GIT


@app.get("/v1/git/status", tags=["git"], summary="Branch and changed files across the whole repository")
def git_status(
    scope: ReadAuth, fetch: bool = False, limit: int = Query(500, ge=1, le=5000)
) -> dict[str, Any]:
    return _git_read().status_tool(fetch=fetch, token_rw=(scope == "rw"), limit=limit)


@app.get("/v1/git/log", tags=["git"], summary="Recent commits on the current branch")
def git_log(
    _: ReadAuth, limit: int = Query(20, ge=1, le=200), scope_only: bool = False
) -> dict[str, Any]:
    return _git_read().log(limit=limit, scope_only=scope_only)


@app.get("/v1/git/diff", tags=["git"], summary="Diff of the shared folder only")
def git_diff(
    _: ReadAuth,
    staged: bool = False,
    path: str | None = None,
    offset: int = Query(0, ge=0),
    length: int | None = Query(None, ge=0),
) -> dict[str, Any]:
    return _git_read().diff(
        staged=staged, path=path, offset=offset, length=length,
        inline_limit=CONFIG.mcp_inline_limit_chars,
    )


@app.post("/v1/git/commit", tags=["git"], summary="Commit changes inside the shared folder")
def git_commit(req: GitCommitRequest, _: WriteAuth) -> dict[str, Any]:
    return _git_write().commit(req.message, req.paths)


@app.post("/v1/git/fetch", tags=["git"], summary="Update remote-tracking refs only")
def git_fetch(_: WriteAuth) -> dict[str, Any]:
    return _git_write().fetch()


@app.post("/v1/git/pull", tags=["git"], summary="Fetch and integrate the remote branch")
def git_pull(req: GitPullRequest, _: WriteAuth) -> dict[str, Any]:
    return _git_write().pull(req.strategy)


@app.post("/v1/git/push", tags=["git"], summary="Push the current branch (never forced)")
def git_push(_: WriteAuth) -> dict[str, Any]:
    return _git_write().push()


@app.post("/v1/git/merge", tags=["git"], summary="Merge a branch into the current one")
def git_merge(req: GitMergeRequest, _: WriteAuth) -> dict[str, Any]:
    return _git_write().merge(req.ref, req.no_ff)


# --------------------------------------------------------------------------
# Remote MCP (optional) -- docs/plan-mcp-connector.md
# --------------------------------------------------------------------------
# `mcp` is declared in requirements-mcp.txt, installed in the SAME pip
# invocation as requirements.txt (not a separate Docker layer -- see
# docs/plan-mcp-connector.md, "Decision gate 2": mcp and fastapi share
# starlette, and installing them in separate invocations lets pip's resolver
# drag sse-starlette's starlette past fastapi's declared bound, breaking the
# image even though mcp itself is pinned). An image built with
# `--build-arg WITH_MCP=0` has no requirements-mcp.txt installed at all, so
# this import fails and /mcp is simply absent -- the REST API is unaffected
# either way. `lifespan()` above references `_mcp_endpoint` before this
# point in the file runs; that's fine, it's a module global looked up when
# `lifespan()` is actually awaited (at server startup), never before this
# module finishes importing.
try:
    from . import mcp_endpoint as _mcp_endpoint
except ImportError:
    if CONFIG.extra_tool_modules:
        # The extensions' tools only exist on /mcp; starting without it would
        # leave them silently missing. (An extension that fails to import
        # raises ExtensionError, a RuntimeError, so it never lands here.)
        raise RuntimeError(
            "EXTRA_TOOL_MODULES is set, but this image was built without the MCP SDK "
            "(--build-arg WITH_MCP=0), so there is no /mcp endpoint to add tools to. "
            "Rebuild with WITH_MCP=1, or unset EXTRA_TOOL_MODULES."
        ) from None
    _mcp_endpoint = None
    log.info("mcp not installed; /mcp endpoint disabled (REST API unaffected)")
else:
    if CONFIG.enable_mcp:
        _mcp_endpoint.mount_mcp(app)
        log.info("Streamable HTTP MCP endpoint mounted at /mcp")
    else:
        _mcp_endpoint = None
        log.info("ENABLE_MCP=false; /mcp endpoint disabled")
        if CONFIG.extra_tool_modules:
            log.warning(
                "ENABLE_MCP=false: the tools from EXTRA_TOOL_MODULES (%s) are unreachable",
                ", ".join(CONFIG.extra_tool_modules),
            )
