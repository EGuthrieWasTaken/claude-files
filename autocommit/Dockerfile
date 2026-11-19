# syntax=docker/dockerfile:1

FROM alpine:3.21

LABEL org.opencontainers.image.title="claude-files-autocommit" \
      org.opencontainers.image.description="Polls a git repo (or one subfolder of it) and commits/pushes changes" \
      org.opencontainers.image.source="https://github.com/EGuthrieWasTaken/claude-files"

# findutils for GNU find (-newermt); busybox find lacks it.
RUN apk add --no-cache bash git openssh-client findutils tzdata ca-certificates

COPY autocommit.sh /usr/local/bin/autocommit
RUN chmod +x /usr/local/bin/autocommit

ENV REPO_PATH=/repo \
    INTERVAL=120 \
    QUIET_PERIOD=30 \
    HOME=/tmp

# OpenSSH calls getpwuid() and refuses to run if the uid has no passwd entry
# ("No user exists for uid 99"), which breaks git-over-ssh whenever the
# container is run with --user. Create entries for the two uids this is
# actually run as: 99:100 (unRAID nobody:users) and the 1000 default.
# Home is /tmp because that is the only writable path on a read-only rootfs.
RUN adduser -D -H -h /tmp -s /sbin/nologin -G users -u 99 unraid-nobody \
 && adduser -D -H -h /tmp -s /sbin/nologin -G users -u 1000 appuser

# Override with `user:` to match your host's ownership (unRAID: 99:100).
USER 1000:1000

HEALTHCHECK NONE

ENTRYPOINT ["/usr/local/bin/autocommit"]
