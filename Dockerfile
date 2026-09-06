# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY app/requirements.txt .
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -r requirements.txt


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
WORKDIR /srv
COPY app/ /srv/app/

# Compile at build time so a read-only rootfs never needs to write bytecode.
RUN python -m compileall -q /srv/app

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
