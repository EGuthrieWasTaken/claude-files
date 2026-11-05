# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build the optional /mcp connector endpoint in by default. `WITH_MCP=0`
# produces a minimal image with today's audit surface (no mcp/httpx/etc.)
# and /mcp simply absent -- see docs/plan-mcp-connector.md, "Decision gate 3".
ARG WITH_MCP=1

WORKDIR /build
COPY app/requirements.txt app/requirements-mcp.txt ./
# requirements.txt and requirements-mcp.txt MUST be installed in a single
# invocation, not two -- mcp and fastapi share starlette, and pip resolves
# each invocation independently, so a separate `pip install -r
# requirements-mcp.txt` afterwards would let sse-starlette drag starlette
# past fastapi's declared bound even with mcp itself pinned. See
# docs/plan-mcp-connector.md, "Option A only holds if it resolves in one pip
# install". `pip check` here turns a bad resolve into a failed build instead
# of a production import error.
RUN python -m venv /venv \
 && if [ "$WITH_MCP" = "1" ]; then \
      /venv/bin/pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt; \
    else \
      /venv/bin/pip install --no-cache-dir -r requirements.txt; \
    fi \
 && /venv/bin/pip check


FROM python:3.12-slim

LABEL org.opencontainers.image.title="claude-files" \
      org.opencontainers.image.description="REST file API confined to a single shared folder" \
      org.opencontainers.image.source="https://hub.docker.com/r/eguthriewastaken2/claude-files"

ENV PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    DATA_ROOT=/data

COPY --from=build /venv /venv

# The git tools (docs/plan-mcp-git-ops.md) shell out to git and ssh. Built in
# by default; `--build-arg WITH_GIT=0` leaves them out, and then setting
# ENABLE_GIT_READ/ENABLE_GIT_WRITE is a startup error that names this arg
# (app/git_ops.py, _find_git) rather than a silent no-op.
#
# The uid-99 entry (unRAID's nobody:users, gid 100 already exists in Debian)
# is for the same reason the autocommit sidecar adds one: OpenSSH calls
# getpwuid() and refuses to run for a uid with no passwd entry ("No user
# exists for uid 99"), which breaks git-over-ssh whenever the container is run
# with --user 99:100. Home is /tmp, the only writable path on a read-only rootfs.
ARG WITH_GIT=1
RUN if [ "$WITH_GIT" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends git openssh-client \
      && rm -rf /var/lib/apt/lists/* \
      && useradd --system --uid 99 --gid 100 --no-create-home --home-dir /tmp \
           --shell /usr/sbin/nologin unraid-nobody; \
    fi

WORKDIR /srv
COPY app/ /srv/app/
# The bridge (docs/feature-request-path-based-upload.md) is client-side code
# that never runs in this image -- it's packaged here purely so GET
# /v1/bridge.py can hand it out in one request. No bridge/requirements.txt
# install here: this image never executes bridge/claude_files_bridge.py.
COPY bridge/ /srv/bridge/

# Compile at build time so a read-only rootfs never needs to write bytecode.
RUN python -m compileall -q /srv/app /srv/bridge

# The mount point exists but is empty in the image; the real folder is
# bind-mounted over it at runtime.
RUN mkdir -p /data

# A default non-root identity. In practice you should override this with
# `user:` / `--user` so written files get the ownership your host expects
# (on unRAID that is 99:100).
RUN useradd --system --uid 1000 --user-group --no-create-home appuser
USER 1000:1000

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--no-server-header", "--proxy-headers", \
     "--timeout-keep-alive", "30"]
