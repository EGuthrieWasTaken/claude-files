#!/usr/bin/env bash
#
# Poll a git repository (or one subfolder of it) and commit + push whatever
# appears.
#
# Pairs with claude-files: point both at the same folder and anything an
# agent uploads lands in git on its own. With SUBDIR=. it commits the whole
# working tree instead, as a general-purpose autocommit for any repo.
#
# Polls rather than watching with inotify, deliberately. unRAID's /mnt/user
# is a FUSE layer and inotify through it silently drops events; a missed
# event means a file that is never committed and nothing to tell you. A poll
# has no such failure mode, and it naturally batches a burst of uploads into
# one commit instead of forty.

set -uo pipefail    # NOT -e: the loop handles its own failures and keeps going

REPO_PATH="${REPO_PATH:-/repo}"
SUBDIR="${SUBDIR:-}"
INTERVAL="${INTERVAL:-120}"
QUIET_PERIOD="${QUIET_PERIOD:-30}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-}"
COMMIT_PREFIX="${COMMIT_PREFIX:-agent: sync}"
PUSH="${PUSH:-true}"
RUN_ONCE="${RUN_ONCE:-false}"
DRY_RUN="${DRY_RUN:-false}"
MAX_CHANGED_FILES="${MAX_CHANGED_FILES:-1000}"
SSH_KEY_PATH="${SSH_KEY_PATH:-}"
SSH_KNOWN_HOSTS_PATH="${SSH_KNOWN_HOSTS_PATH:-}"
GIT_TOKEN="${GIT_TOKEN:-}"

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-claude-files autocommit}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-autocommit@localhost}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

consecutive_failures=0
running=true

log() { printf '%s %-5s %s\n' "$(date -Is)" "$1" "$2"; }
die() { log FATAL "$1"; exit 1; }

trap 'running=false; log INFO "shutting down"; exit 0' TERM INT

# ---------------------------------------------------------------------------
# Startup validation. Everything here is a reason to refuse to start rather
# than run in a state that could commit more than it should.
# ---------------------------------------------------------------------------

validate() {
  [[ -d "$REPO_PATH" ]] || die "REPO_PATH does not exist: $REPO_PATH"

  # SUBDIR is what keeps this sidecar's blast radius equal to the API's. An
  # unset or empty value must never mean "everything", so it is refused;
  # committing the whole repository takes an explicit SUBDIR=. instead.
  [[ -n "$SUBDIR" ]] || die "SUBDIR is required (the folder to commit, relative to the repo root, or . for the whole repository)"
  case "$SUBDIR" in
    /*)   die "SUBDIR must be relative to the repo root, not absolute: $SUBDIR" ;;
    *..*) die "SUBDIR must not contain '..': $SUBDIR" ;;
    .|./) SUBDIR="." ;;
    *)    SUBDIR="${SUBDIR%/}" ;;
  esac

  [[ -d "$REPO_PATH/$SUBDIR" ]] || die "SUBDIR does not exist in the repo: $REPO_PATH/$SUBDIR"

  export HOME=/tmp
  git config --global --add safe.directory "$REPO_PATH" 2>/dev/null
  git config --global advice.detachedHead false 2>/dev/null

  git -C "$REPO_PATH" rev-parse --git-dir >/dev/null 2>&1 \
    || die "not a git repository: $REPO_PATH"

  # A detached HEAD would let us build commits that belong to no branch and
  # get garbage collected. Refuse.
  if ! git -C "$REPO_PATH" symbolic-ref -q HEAD >/dev/null; then
    die "HEAD is detached; check out a branch before running the sidecar"
  fi

  [[ -n "$GIT_BRANCH" ]] || GIT_BRANCH="$(git -C "$REPO_PATH" rev-parse --abbrev-ref HEAD)"

  local current
  current="$(git -C "$REPO_PATH" rev-parse --abbrev-ref HEAD)"
  [[ "$current" == "$GIT_BRANCH" ]] \
    || die "checked-out branch ($current) does not match GIT_BRANCH ($GIT_BRANCH)"

  if [[ "$PUSH" == "true" ]]; then
    git -C "$REPO_PATH" remote get-url "$GIT_REMOTE" >/dev/null 2>&1 \
      || die "remote '$GIT_REMOTE' is not configured in $REPO_PATH"
  fi

  if [[ "$SUBDIR" == "." ]]; then
    log WARN "SUBDIR=. -- committing the WHOLE repository, not one folder."
    log WARN "Anything not covered by .gitignore will be committed and pushed."
  fi

  setup_credentials
}

setup_credentials() {
  local ssh_opts=""

  if [[ -n "$SSH_KEY_PATH" ]]; then
    # A folder mapped onto this path is the common misconfiguration: the
    # container then sees a directory here, not the key. Say so explicitly --
    # "does not exist" sends people looking for the wrong problem.
    if [[ -d "$SSH_KEY_PATH" ]]; then
      log FATAL "SSH_KEY_PATH is a directory, not a file: $SSH_KEY_PATH"
      log FATAL "You have mapped a FOLDER to this path. Either:"
      log FATAL "  (a) map the key FILE itself to $SSH_KEY_PATH, or"
      log FATAL "  (b) map the folder to /keys and set SSH_KEY_PATH=/keys/<keyfilename>"
      log FATAL "Contents seen at $SSH_KEY_PATH: $(ls -A "$SSH_KEY_PATH" 2>/dev/null | tr '\n' ' ')"
      exit 1
    fi
    [[ -f "$SSH_KEY_PATH" ]] || die "SSH_KEY_PATH does not exist: $SSH_KEY_PATH (check the container path mapping)"
    # ssh refuses a key with loose permissions, and a read-only bind mount
    # from the host usually arrives as 0644. Copy to tmpfs and tighten.
    install -m 600 "$SSH_KEY_PATH" /tmp/id_key || die "could not stage the ssh key"
    ssh_opts="-i /tmp/id_key -o IdentitiesOnly=yes"

    if [[ -d "$SSH_KNOWN_HOSTS_PATH" ]]; then
      log WARN "SSH_KNOWN_HOSTS_PATH is a directory, not a file: $SSH_KNOWN_HOSTS_PATH"
      log WARN "Map the known_hosts FILE here, or point this at a file inside a mounted folder."
      SSH_KNOWN_HOSTS_PATH=""
    fi
    if [[ -n "$SSH_KNOWN_HOSTS_PATH" && -f "$SSH_KNOWN_HOSTS_PATH" ]]; then
      install -m 600 "$SSH_KNOWN_HOSTS_PATH" /tmp/known_hosts
      ssh_opts="$ssh_opts -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/known_hosts"
      log INFO "ssh: strict host key checking against $SSH_KNOWN_HOSTS_PATH"
    else
      ssh_opts="$ssh_opts -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/known_hosts"
      log WARN "ssh: no known_hosts supplied, trusting the host key on first use."
      log WARN "ssh: set SSH_KNOWN_HOSTS_PATH to pin it."
    fi
    export GIT_SSH_COMMAND="ssh $ssh_opts"
  fi

  if [[ -n "$GIT_TOKEN" ]]; then
    # Kept in tmpfs so it never touches disk.
    umask 077
    printf '%s\n' "$GIT_TOKEN" > /tmp/.git-token
    git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=$(cat /tmp/.git-token)"; }; f'
    log INFO "git: using token authentication"
  fi
}

# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

# Transient files from an interrupted claude-files upload. Excluded via
# pathspec rather than by editing the repo's .gitignore, so the sidecar
# leaves no trace in the working tree.
pathspec() {
  if [[ "$SUBDIR" == "." ]]; then
    # A leading **/ also matches the top level, so one pattern covers it.
    printf '%s\n' "." ":(exclude,glob)**/.upload-*.tmp"
    return
  fi
  printf '%s\n' \
    "$SUBDIR" \
    ":(exclude,glob)$SUBDIR/**/.upload-*.tmp" \
    ":(exclude,glob)$SUBDIR/.upload-*.tmp"
}

changed_files() {
  local -a spec
  mapfile -t spec < <(pathspec)
  git -C "$REPO_PATH" status --porcelain --untracked-files=all -- "${spec[@]}" 2>/dev/null
}

recently_touched() {
  # Any file written in the last QUIET_PERIOD seconds means a burst may still
  # be in flight; wait for it to settle rather than commit half of it.
  #
  # Only files git would commit count: modified or untracked, not ignored.
  # Walking the folder instead would see .git/ and every ignored file, and
  # anything that is always being written (a log, a database) would defer
  # the commit forever -- most visibly with SUBDIR=. on an appdata folder.
  # Deleted files are listed too but no longer exist; find skips them.
  [[ "$QUIET_PERIOD" -le 0 ]] && return 1
  local -a spec
  mapfile -t spec < <(pathspec)
  local hit
  hit="$(git -C "$REPO_PATH" ls-files -z --modified --others --exclude-standard -- "${spec[@]}" 2>/dev/null \
    | (cd "$REPO_PATH" && find -files0-from - -maxdepth 0 -type f -newermt "-${QUIET_PERIOD} seconds" -print -quit 2>/dev/null))"
  [[ -n "$hit" ]]
}

sync_once() {
  local status count
  status="$(changed_files)"
  if [[ -z "$status" ]]; then
    return 0
  fi

  count="$(printf '%s\n' "$status" | grep -c .)"

  if [[ "$MAX_CHANGED_FILES" -gt 0 && "$count" -gt "$MAX_CHANGED_FILES" ]]; then
    log ERROR "$count changed files exceeds MAX_CHANGED_FILES=$MAX_CHANGED_FILES; refusing to commit."
    log ERROR "This is the guard against a runaway agent. Inspect the folder, then commit by hand."
    return 1
  fi

  if recently_touched; then
    log INFO "$count change(s) pending, but files were written in the last ${QUIET_PERIOD}s; waiting"
    return 0
  fi

  log INFO "$count change(s) in $SUBDIR"
  printf '%s\n' "$status" | head -20 | while read -r line; do log INFO "  $line"; done
  [[ "$count" -gt 20 ]] && log INFO "  ... and $((count - 20)) more"

  if [[ "$DRY_RUN" == "true" ]]; then
    log INFO "DRY_RUN: not committing"
    return 0
  fi

  local -a spec
  mapfile -t spec < <(pathspec)

  # Scoped to SUBDIR. Never `git add -A` -- anything dirty elsewhere in the
  # working tree is not this sidecar's business.
  if ! git -C "$REPO_PATH" add -- "${spec[@]}"; then
    log ERROR "git add failed"; return 1
  fi

  if git -C "$REPO_PATH" diff --cached --quiet; then
    log INFO "nothing staged after exclusions"
    return 0
  fi

  local msg body
  body="$(git -C "$REPO_PATH" diff --cached --name-status | head -50)"
  msg="$COMMIT_PREFIX ($count file$([[ $count -ne 1 ]] && echo s))"

  if ! git -C "$REPO_PATH" commit -q -m "$msg" -m "$body"; then
    log ERROR "git commit failed"; return 1
  fi
  log INFO "committed: $msg"

  [[ "$PUSH" == "true" ]] || { log INFO "PUSH=false, leaving the commit local"; return 0; }

  # Try the push first. In the common case the remote has not moved, this is
  # a fast-forward, and the working tree is never touched at all.
  local push_err
  if push_err="$(git -C "$REPO_PATH" push --quiet "$GIT_REMOTE" "HEAD:$GIT_BRANCH" 2>&1)"; then
    log INFO "pushed to $GIT_REMOTE/$GIT_BRANCH"
    return 0
  fi

  # Only a non-fast-forward rejection means the remote moved. Anything else --
  # auth failure, DNS, an unreachable host -- must be reported as itself.
  # Rebasing on top of such a failure produces a bogus "conflict" error that
  # sends you looking for a merge problem that does not exist.
  if ! grep -qiE 'non-fast-forward|fetch first|\[rejected\]|behind its remote|updates were rejected' <<<"$push_err"; then
    log ERROR "push to $GIT_REMOTE/$GIT_BRANCH failed, and not because the remote moved:"
    printf '%s\n' "$push_err" | head -10 | while read -r l; do [[ -n "$l" ]] && log ERROR "  $l"; done
    log ERROR "the commit is kept locally and will retry next cycle"
    return 1
  fi

  log INFO "push rejected as non-fast-forward, rebasing onto $GIT_REMOTE/$GIT_BRANCH"

  # Only reached on real divergence. --autostash is required, not optional:
  # any unrelated uncommitted edit elsewhere in the working tree makes rebase
  # refuse to start, which would wedge the sidecar into committing forever
  # and never pushing again.
  local dirty
  dirty="$(git -C "$REPO_PATH" status --porcelain --untracked-files=no)"
  if [[ -n "$dirty" ]]; then
    log WARN "unrelated uncommitted changes present; stashing them across the rebase:"
    printf '%s\n' "$dirty" | head -10 | while read -r l; do log WARN "  $l"; done
  fi

  local rebase_err
  if ! rebase_err="$(git -C "$REPO_PATH" pull --rebase --autostash "$GIT_REMOTE" "$GIT_BRANCH" 2>&1)"; then
    log ERROR "rebase onto $GIT_REMOTE/$GIT_BRANCH failed:"
    printf '%s\n' "$rebase_err" | head -10 | while read -r l; do [[ -n "$l" ]] && log ERROR "  $l"; done
    git -C "$REPO_PATH" rebase --abort 2>/dev/null
    log ERROR "the local commit is kept and will retry next cycle; resolve by hand if it persists"
    return 1
  fi

  if ! git -C "$REPO_PATH" push --quiet "$GIT_REMOTE" "HEAD:$GIT_BRANCH"; then
    log ERROR "git push failed after rebase: $push_err"
    return 1
  fi
  log INFO "pushed to $GIT_REMOTE/$GIT_BRANCH after rebase"
  return 0
}

# ---------------------------------------------------------------------------

validate

if [[ "$SUBDIR" == "." ]]; then
  log INFO "watching $REPO_PATH (whole repository) on branch $GIT_BRANCH"
else
  log INFO "watching $REPO_PATH/$SUBDIR on branch $GIT_BRANCH"
fi
log INFO "interval=${INTERVAL}s quiet=${QUIET_PERIOD}s push=$PUSH dry_run=$DRY_RUN max_files=$MAX_CHANGED_FILES"

if [[ "$RUN_ONCE" == "true" ]]; then
  sync_once
  exit $?
fi

while $running; do
  if sync_once; then
    consecutive_failures=0
  else
    consecutive_failures=$((consecutive_failures + 1))
    if [[ $consecutive_failures -ge 5 ]]; then
      log ERROR "$consecutive_failures consecutive failures -- this needs a human"
    fi
  fi
  sleep "$INTERVAL" &
  wait $! 2>/dev/null
done
