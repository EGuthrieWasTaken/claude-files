"""claude-files: a minimal REST file API scoped to a single directory.

Designed to be the only thing an autonomous agent can reach on the host: the
container bind-mounts one subfolder, and every request is confined to it by
``app.safepath``.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import deque
from typing import Annotated, Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Config
from .safepath import Entry, PathError, Vault

log = logging.getLogger("claude-files")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

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

VAULT = Vault(
    CONFIG.root,
    denied_names=CONFIG.denied_names,
    allow_hardlinked_files=CONFIG.allow_hardlinked_files,
)

DESCRIPTION = """
A REST API for reading and writing files inside a single shared folder.

Every path is relative to the shared folder root. Absolute paths, `..`
segments, and symlinks are rejected -- there is no path that reaches outside
the shared folder.

Authenticate with `Authorization: Bearer <token>`.
"""

app = FastAPI(
    title="claude-files",
    description=DESCRIPTION,
    version="1.0.0",
    docs_url="/docs" if CONFIG.enable_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if CONFIG.enable_docs else None,
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


class OkResponse(BaseModel):
    ok: bool = True
    path: str = ""
    size: int | None = None
    created: bool | None = None


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


def _check_token(authorization: str | None) -> str:
    """Return "rw" or "ro"."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            detail={"code": "unauthorized", "message": "Missing bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[7:].strip()
    if hmac.compare_digest(presented, CONFIG.token):
        return "rw"
    if CONFIG.readonly_token and hmac.compare_digest(presented, CONFIG.readonly_token):
        return "ro"
    raise HTTPException(
        401,
        detail={"code": "unauthorized", "message": "Invalid token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_read(authorization: Annotated[str | None, Header()] = None) -> str:
    _rate_limit()
    return _check_token(authorization)


def require_write(authorization: Annotated[str | None, Header()] = None) -> str:
    _rate_limit()
    scope = _check_token(authorization)
    if scope != "rw":
        raise HTTPException(
            403, detail={"code": "readonly", "message": "This token is read-only"}
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


@app.get("/v1/info", tags=["meta"], summary="Server limits and policy")
def info(_: ReadAuth) -> dict[str, Any]:
    return {
        "max_upload_bytes": CONFIG.max_upload_bytes,
        "max_list_entries": CONFIG.max_list_entries,
        "denied_names": sorted(CONFIG.denied_names),
        "allow_delete": CONFIG.allow_delete,
        "allow_hardlinked_files": CONFIG.allow_hardlinked_files,
    }


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


async def _write_body(
    request: Request, parts: list[str], *, overwrite: bool, create_parents: bool
) -> int:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > CONFIG.max_upload_bytes:
                raise HTTPException(
                    413,
                    detail={
                        "code": "too_large",
                        "message": f"Body exceeds MAX_UPLOAD_BYTES ({CONFIG.max_upload_bytes})",
                    },
                )
        except ValueError:
            raise HTTPException(
                400, detail={"code": "bad_request", "message": "Invalid Content-Length"}
            ) from None

    written = 0
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
            os.write(fd, chunk)
        committer.commit(fd)
    return written


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
    size = await _write_body(request, parts, overwrite=True, create_parents=parents)
    log.info("wrote %s (%d bytes)", "/".join(parts), size)
    return OkResponse(path="/".join(parts), size=size, created=True)


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
    size = await _write_body(request, parts, overwrite=False, create_parents=parents)
    log.info("created %s (%d bytes)", "/".join(parts), size)
    return OkResponse(path="/".join(parts), size=size, created=True)


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


@app.on_event("startup")
def _startup() -> None:
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
