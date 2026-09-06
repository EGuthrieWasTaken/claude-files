# syntax=docker/dockerfile:1

FROM alpine:3.21

LABEL org.opencontainers.image.title="claude-files-autocommit" \
      org.opencontainers.image.description="Polls a subfolder of a git repo and commits/pushes changes" \
      org.opencontainers.image.source="https://hub.docker.com/r/eguthriewastaken2/claude-files"

# findutils for GNU find (-newermt); busybox find lacks it.
RUN apk add --no-cache bash git openssh-client findutils tzdata ca-certificates

COPY autocommit.sh /usr/local/bin/autocommit
RUN chmod +x /usr/local/bin/autocommit

ENV REPO_PATH=/repo \
    INTERVAL=120 \
    QUIET_PERIOD=30 \
    HOME=/tmp

# Override with `user:` to match your host's ownership (unRAID: 99:100).
USER 1000:1000

HEALTHCHECK NONE

ENTRYPOINT ["/usr/local/bin/autocommit"]
