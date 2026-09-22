#!/usr/bin/env bash
# Tests for the autocommit sidecar.
#
# Fixture layout (all inside one bind mount so paths match host and container):
#   /work/remote.git   bare repo standing in for the git remote
#   /work/repo         working clone the sidecar operates on
#     shared/          the folder the sidecar is scoped to
#     src/             must never be touched by the sidecar
set -uo pipefail

cd "$(dirname "$0")/.."
IMG="claude-files-autocommit:test"
FIX="${FIXTURE_DIR:-$PWD/.acfixture}"
PASS=0; FAIL=0
G="\033[32m"; R="\033[31m"; Y="\033[33m"; N="\033[0m"

ok()  { PASS=$((PASS+1)); printf "  ${G}PASS${N} %s\n" "$1"; }
bad() { FAIL=$((FAIL+1)); printf "  ${R}FAIL${N} %s -- %s\n" "$1" "$2"; }

cleanup() { [[ "${KEEP_FIXTURE:-0}" == "1" ]] || rm -rf "$FIX"; }
trap cleanup EXIT

# Match the container's uid:gid to whoever owns $FIX on disk (reset_fixture
# runs as this same user). The image's baked-in default is a fixed non-root
# uid (1000:1000, autocommit/Dockerfile) -- fine on Docker Desktop's bind
# mounts, which don't enforce host-side Unix permissions, but on a native
# Linux host (GitHub Actions runners included) a bind mount keeps real host
# ownership, and 1000:1000 almost never matches the invoking user, so every
# `git add`/`commit`/`checkout` fails with a permission error underneath
# git's own error text. Real deployments hit the same class of problem and
# solve it the same way -- docker-compose.yml's `user: "99:100"` for unRAID
# is this exact override, just for the main API image instead of this one.
DOCKER_USER=(--user "$(id -u):$(id -g)")

inc() {
  docker run --rm "${DOCKER_USER[@]}" -v "$FIX:/work" --entrypoint bash "$IMG" -c \
    "export HOME=/tmp; git config --global --add safe.directory '*' >/dev/null 2>&1; $1" 2>&1
}

sidecar() { # extra env as args
  docker run --rm "${DOCKER_USER[@]}" -v "$FIX:/work" \
    -e REPO_PATH=/work/repo -e SUBDIR=shared -e RUN_ONCE=true -e QUIET_PERIOD=0 \
    "$@" "$IMG" 2>&1
}

# Built entirely on the host. Creating it inside a container and then
# reading it from the host races against virtiofs visibility on macOS.
reset_fixture() {
  rm -rf "${FIX:?}"
  mkdir -p "$FIX"
  (
    set -e
    export HOME="$FIX/home"; mkdir -p "$HOME"
    export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
    git config --global user.email t@t
    git config --global user.name t
    git config --global init.defaultBranch main
    # Without -b main the bare repo's HEAD symrefs to master, which we never
    # push, and every `ls-tree HEAD` assertion fails to resolve.
    git init --bare -q -b main "$FIX/remote.git"
    git init -q -b main "$FIX/repo"
    cd "$FIX/repo"
    mkdir -p shared src
    echo "private source"  > src/app.py
    echo "SECRET=1"        > secrets.env
    echo "seed"            > shared/seed.txt
    git add -A && git commit -q -m "initial"
    git remote add origin "$FIX/remote.git"
    git push -q -u origin main
    # Point the remote at the in-container path for the sidecar.
    git remote set-url origin /work/remote.git
  ) || { echo "fixture setup failed"; exit 1; }
}

remote_files() { inc 'git -C /work/remote.git ls-tree -r --name-only HEAD' | tr -d '\r'; }
remote_log()   { inc 'git -C /work/remote.git log --oneline'; }
local_log()    { inc 'git -C /work/repo log --oneline'; }

# ===========================================================================
echo -e "\n${Y}autocommit sidecar tests${N}"

# --- 1. the happy path ------------------------------------------------------
reset_fixture
echo "agent output" > "$FIX/repo/shared/report.md"
out=$(sidecar)
if remote_files | grep -q "^shared/report.md$"; then
  ok "new file in shared/ is committed and pushed"
else
  bad "happy path" "report.md not in remote: $(remote_files | tr '\n' ' ')"
fi

# --- 2. scoping: files outside the subfolder are never committed ------------
reset_fixture
echo "agent output"   > "$FIX/repo/shared/report.md"
echo "TAMPERED"       > "$FIX/repo/src/app.py"
echo "NEW_OUTSIDE"    > "$FIX/repo/outside.txt"
echo "LEAKED=2"       > "$FIX/repo/secrets.env"
sidecar >/dev/null
rf="$(remote_files)"
tampered=$(inc 'git -C /work/repo show HEAD:src/app.py')
if grep -q "^shared/report.md$" <<<"$rf" \
   && ! grep -q "^outside.txt$" <<<"$rf" \
   && [[ "$tampered" == "private source" ]] \
   && [[ "$(inc 'git -C /work/repo show HEAD:secrets.env')" == "SECRET=1" ]]; then
  ok "changes outside shared/ are not committed"
else
  bad "scoping" "remote=$(tr '\n' ' ' <<<"$rf") app.py=$tampered"
fi

# --- 3. in-flight uploads are excluded --------------------------------------
reset_fixture
echo "done"      > "$FIX/repo/shared/finished.txt"
echo "partial"   > "$FIX/repo/shared/.upload-deadbeef.tmp"
mkdir -p "$FIX/repo/shared/nested"
echo "partial"   > "$FIX/repo/shared/nested/.upload-cafe.tmp"
sidecar >/dev/null
rf="$(remote_files)"
if grep -q "finished.txt" <<<"$rf" && ! grep -q "upload-" <<<"$rf"; then
  ok "in-flight .upload-*.tmp files are excluded"
else
  bad "temp exclusion" "remote=$(tr '\n' ' ' <<<"$rf")"
fi

# --- 4. no changes means no empty commit ------------------------------------
reset_fixture
before=$(remote_log | wc -l)
sidecar >/dev/null
after=$(remote_log | wc -l)
[[ "$before" == "$after" ]] && ok "no changes produces no commit" \
  || bad "empty commit" "log grew from $before to $after"

# --- 5. deletions are recorded ----------------------------------------------
reset_fixture
rm "$FIX/repo/shared/seed.txt"
sidecar >/dev/null
if ! remote_files | grep -q "shared/seed.txt"; then
  ok "deletion is committed"
else
  bad "deletion" "seed.txt still in remote"
fi

# --- 6. quiet period defers a burst still in flight -------------------------
reset_fixture
echo "just written" > "$FIX/repo/shared/hot.txt"
out=$(sidecar -e QUIET_PERIOD=60)
if ! remote_files | grep -q "hot.txt" && grep -q "waiting" <<<"$out"; then
  ok "quiet period defers a file written seconds ago"
else
  bad "quiet period" "committed too early"
fi
out=$(sidecar -e QUIET_PERIOD=0)
remote_files | grep -q "hot.txt" && ok "commits once the folder settles" \
  || bad "quiet period release" "never committed"

# --- 7. divergent remote: rebase then push ----------------------------------
reset_fixture
(
  set -e
  export HOME="$FIX/home"; export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
  git clone -q "$FIX/remote.git" "$FIX/other"
  cd "$FIX/other" && echo "from elsewhere" > shared/theirs.txt
  git add -A && git commit -q -m "someone else committed" && git push -q origin main
) >/dev/null
echo "ours" > "$FIX/repo/shared/ours.txt"
out=$(sidecar)
rf="$(remote_files)"
if grep -q "shared/ours.txt" <<<"$rf" && grep -q "shared/theirs.txt" <<<"$rf"; then
  ok "diverged remote is rebased onto, both commits survive"
else
  bad "rebase" "remote=$(tr '\n' ' ' <<<"$rf")"
fi

# --- 7b. divergence WITH unrelated dirty files (regression) ----------------
# `git pull --rebase` refuses to start when the working tree is dirty, so
# without --autostash the sidecar would commit forever and never push again.
reset_fixture
(
  set -e
  export HOME="$FIX/home"; export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
  git clone -q "$FIX/remote.git" "$FIX/other2"
  cd "$FIX/other2" && echo "upstream change" > shared/theirs.txt
  git add -A && git commit -q -m "upstream" && git push -q origin main
) >/dev/null
echo "ours"                 > "$FIX/repo/shared/ours.txt"
echo "WORK IN PROGRESS"     > "$FIX/repo/src/app.py"      # tracked + dirty
echo "scratch"              > "$FIX/repo/notes.txt"       # untracked
out=$(sidecar)
rf="$(remote_files)"
wip="$(cat "$FIX/repo/src/app.py")"
if grep -q "shared/ours.txt" <<<"$rf" && grep -q "shared/theirs.txt" <<<"$rf"; then
  if [[ "$wip" == "WORK IN PROGRESS" ]] && [[ -f "$FIX/repo/notes.txt" ]]; then
    ok "rebases past a dirty working tree and restores the in-progress edits"
  else
    bad "autostash restore" "src/app.py='$wip' notes.txt=$([[ -f "$FIX/repo/notes.txt" ]] && echo kept || echo LOST)"
  fi
else
  bad "dirty-tree rebase" "remote=$(tr '\n' ' ' <<<"$rf")"
fi
# src/app.py is tracked from the initial commit, so it is legitimately in
# the tree -- what matters is that the DIRTY CONTENT was not committed.
remote_app="$(inc 'git -C /work/remote.git show HEAD:src/app.py')"
if [[ "$remote_app" == "private source" ]] && ! grep -q "notes.txt" <<<"$rf"; then
  ok "stashed edits are not committed to the remote"
else
  bad "dirty-tree scoping" "remote src/app.py='$remote_app'"
fi

# --- 7c. an unreachable remote is not a "conflict" (regression) ------------
# Any push failure used to be treated as divergence, so an auth or network
# error was reported as a rebase conflict -- sending you after a merge problem
# that does not exist.
reset_fixture
inc 'git -C /work/repo remote set-url origin /work/does-not-exist.git' >/dev/null
echo "ours" > "$FIX/repo/shared/unreachable.txt"
out=$(sidecar)
if grep -q "not because the remote moved" <<<"$out" && ! grep -qi "conflict" <<<"$out"; then
  ok "unreachable remote reports the real error, not a conflict"
else
  bad "push error classification" "$(grep -E 'ERROR' <<<"$out" | head -2 | tr '\n' ' ')"
fi
if local_log | grep -q "agent: sync"; then
  ok "the commit is kept locally for the next retry"
else
  bad "commit retention" "commit was lost"
fi

# --- 8. runaway guard -------------------------------------------------------
reset_fixture
for i in $(seq 1 6); do echo "f$i" > "$FIX/repo/shared/file$i.txt"; done
out=$(sidecar -e MAX_CHANGED_FILES=3); rc=$?
if [[ $rc -ne 0 ]] && grep -q "refusing to commit" <<<"$out" \
   && ! remote_files | grep -q "file1.txt"; then
  ok "MAX_CHANGED_FILES blocks a runaway agent"
else
  bad "runaway guard" "rc=$rc committed anyway?"
fi

# --- 9. startup validation --------------------------------------------------
reset_fixture
allref=0
for entry in "empty SUBDIR|" "repo root|." "absolute|/etc" "traversal|shared/../.."; do
  label="${entry%%|*}"; value="${entry#*|}"
  out=$(docker run --rm "${DOCKER_USER[@]}" -v "$FIX:/work" -e REPO_PATH=/work/repo \
        -e SUBDIR="$value" -e RUN_ONCE=true "$IMG" 2>&1); rc=$?
  if [[ $rc -eq 0 ]]; then bad "validation: $label" "started anyway"; allref=1; fi
done
[[ $allref -eq 0 ]] && ok "refuses every unsafe SUBDIR (empty, '.', absolute, '..')"

reset_fixture
out=$(inc 'cd /work/repo && git checkout -q --detach HEAD' 2>&1)
out=$(sidecar); rc=$?
if [[ $rc -ne 0 ]] && grep -qi "detached" <<<"$out"; then
  ok "refuses to run on a detached HEAD"
else
  bad "detached HEAD" "rc=$rc"
fi

# --- 10. PUSH=false and DRY_RUN --------------------------------------------
reset_fixture
echo "local only" > "$FIX/repo/shared/local.txt"
push_false_out=$(sidecar -e PUSH=false)
llog="$(local_log)"; rfiles="$(remote_files)"
if grep -q "agent: sync" <<<"$llog" && ! grep -q "local.txt" <<<"$rfiles"; then
  ok "PUSH=false commits locally without pushing"
else
  # Diagnostic dump -- this check has failed intermittently in CI without an
  # obvious cause from the pass/fail result alone; capture everything needed
  # to root-cause it on the next occurrence instead of guessing blind again.
  bad "PUSH=false" "local_log=$(tr '\n' '|' <<<"$llog") remote_files=$(tr '\n' '|' <<<"$rfiles") sidecar_out=$(tr '\n' '|' <<<"$push_false_out")"
fi

reset_fixture
echo "dry" > "$FIX/repo/shared/dry.txt"
sidecar -e DRY_RUN=true >/dev/null
if ! local_log | grep -q "agent: sync"; then
  ok "DRY_RUN makes no commit"
else
  bad "DRY_RUN" "committed anyway"
fi

# ===========================================================================
echo
if [[ $FAIL -eq 0 ]]; then
  echo -e "${G}ALL PASSED${N}  $PASS checks"; exit 0
else
  echo -e "${R}FAILED${N}  $PASS passed, $FAIL failed"; exit 1
fi
