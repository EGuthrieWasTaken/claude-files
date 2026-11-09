#!/usr/bin/env bash
# Build the image and run the containment suite against TWO configurations:
#
#   A) isolated  -- production layout. Only repo/shared is bind-mounted, so
#                   the rest of the repo is not in the container's mount
#                   namespace at all.
#
#   B) app-only  -- deliberately WEAKENED. The whole repo is mounted and
#                   DATA_ROOT points at the subfolder, so the parent really
#                   is reachable in the namespace. This proves the
#                   application-level containment holds on its own, without
#                   help from the mount boundary.
#
# Both must pass every escape test.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
FIXTURE_BASE="${FIXTURE_DIR:-$ROOT/.fixture}"
FIXTURE_N=0
FIXTURE=""

# A fresh directory per step. Deleting and recreating one path leaves Docker
# bind-mounting a stale inode on macOS, which shows up as an empty /data.
new_fixture() {
  FIXTURE_N=$((FIXTURE_N + 1))
  FIXTURE="$FIXTURE_BASE/f$FIXTURE_N"
  bash tests/make_fixture.sh "$FIXTURE" >/dev/null
}
IMAGE="claude-files:test"
IMAGE_NOMCP="claude-files:test-nomcp"
IMAGE_NOGIT="claude-files:test-nogit"
TOKEN="test-token-$(openssl rand -hex 16)"
RO_TOKEN="test-ro-token-$(openssl rand -hex 16)"
# Deliberately small so tests/mcp_escape_suite.py's budget checks (truncation,
# binary/base64 refusal) exercise real boundaries instead of never tripping.
# Must stay comfortably above write_file's own hardcoded 1KB
# require-verification threshold (app/mcp_endpoint.py) -- 1024 here would
# make every "over 1KB, no expect_*" test collide with "over the single-shot
# cap" instead, refused for the wrong reason. Matches the production default
# (app/config.py) so the test boundary is the real one, not an arbitrary
# smaller stand-in.
MCP_INLINE_LIMIT_CHARS=2000
MCP_BASE64_LIMIT_BYTES=8192
# Deliberately short/small so tests/mcp_escape_suite.py's chunked-write
# checks (TTL reap, concurrency cap) exercise real boundaries in seconds
# instead of never tripping or requiring a 900s sleep.
MCP_UPLOAD_SESSION_TTL_SECONDS=3
MCP_MAX_CONCURRENT_UPLOADS=4
MCP_UPLOAD_CHUNK_ADVISORY_BYTES=3072
FAIL=0

cleanup() {
  docker rm -f cf-test-a cf-test-b cf-test-nomcp cf-test-offswitch cf-test-git >/dev/null 2>&1 || true
  [[ "${KEEP_FIXTURE:-0}" == "1" ]] || rm -rf "$FIXTURE_BASE"
}
trap cleanup EXIT

echo "==> Building image (WITH_MCP=1, the default)"
docker build -q -t "$IMAGE" . >/dev/null

echo "==> pip check inside the built image (test 11: catches the two-invocations trap)"
if docker run --rm --entrypoint /venv/bin/pip "$IMAGE" check; then
  echo "  PASS  no broken requirements (fastapi/mcp/starlette resolve cleanly)"
else
  echo "  FAIL  pip check found a broken dependency graph"; FAIL=1
fi

echo "==> git and ssh are in the default image (WITH_GIT=1); uid 99 resolves (OpenSSH needs it)"
if docker run --rm --entrypoint /usr/bin/git "$IMAGE" --version >/dev/null 2>&1 \
   && docker run --rm --entrypoint /usr/bin/ssh "$IMAGE" -V >/dev/null 2>&1 \
   && [[ "$(docker run --rm --user 99:100 --entrypoint /usr/bin/id "$IMAGE" -un 2>/dev/null)" == "unraid-nobody" ]]; then
  echo "  PASS  git, ssh, and a passwd entry for uid 99 are present"
else
  echo "  FAIL  the default image is missing git, ssh, or the uid-99 passwd entry"; FAIL=1
fi

wait_healthy() {
  local name=$1 port=$2
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  echo "!! $name never became healthy"; docker logs "$name" 2>&1 | tail -30; return 1
}

common_flags=(
  --rm -d
  --read-only
  # Match the container's uid:gid to whoever owns the fixture on disk
  # (make_fixture.sh runs as this same user). The image's baked-in default
  # is a fixed non-root uid (1000:1000, Dockerfile) -- fine on Docker
  # Desktop's bind mounts, which don't enforce host-side Unix permissions,
  # but on a native Linux host (GitHub Actions runners included) a bind
  # mount keeps real host ownership, and 1000:1000 almost never matches the
  # invoking user, so every write 403s with "Permission denied". Real
  # deployments hit the same thing and solve it the same way --
  # docker-compose.yml's `user: "99:100"` for unRAID is this exact override.
  --user "$(id -u):$(id -g)"
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m
  --cap-drop ALL
  --security-opt no-new-privileges:true
  --pids-limit 200
  --memory 512m
  -e API_TOKEN="$TOKEN"
  -e API_TOKEN_READONLY="$RO_TOKEN"
  -e MAX_UPLOAD_BYTES=2097152
  -e UPLOAD_TICKET_TTL_SECONDS=3
  -e DOWNLOAD_TICKET_TTL_SECONDS=3
  -e LOG_LEVEL=INFO
  -e ENABLE_MCP=true
  -e MCP_INLINE_LIMIT_CHARS="$MCP_INLINE_LIMIT_CHARS"
  -e MCP_BASE64_LIMIT_BYTES="$MCP_BASE64_LIMIT_BYTES"
  -e MCP_UPLOAD_SESSION_TTL_SECONDS="$MCP_UPLOAD_SESSION_TTL_SECONDS"
  -e MCP_MAX_CONCURRENT_UPLOADS="$MCP_MAX_CONCURRENT_UPLOADS"
  -e MCP_UPLOAD_CHUNK_ADVISORY_BYTES="$MCP_UPLOAD_CHUNK_ADVISORY_BYTES"
)

run_mode() {
  local mode=$1 name=$2 port=$3; shift 3
  echo
  echo "############################################################"
  echo "## MODE: $mode"
  echo "############################################################"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run "${common_flags[@]}" --name "$name" -p "127.0.0.1:$port:8080" "$@" "$IMAGE" >/dev/null
  wait_healthy "$name" "$port"

  local test_out="$FIXTURE/test_output_$name.log"
  if ! ( BASE_URL="127.0.0.1:$port" API_TOKEN="$TOKEN" API_TOKEN_READONLY="$RO_TOKEN" \
         FIXTURE_REPO="$FIXTURE/repo" MODE="$mode" UPLOAD_TICKET_TTL_SECONDS=3 DOWNLOAD_TICKET_TTL_SECONDS=3 \
           python3 tests/escape_suite.py 2>&1 | tee "$test_out" ); then
    FAIL=1
  fi

  echo
  echo "--- MCP connector: same containment suite via /mcp tools/call ---"
  BASE_URL="127.0.0.1:$port" API_TOKEN="$TOKEN" API_TOKEN_READONLY="$RO_TOKEN" \
  FIXTURE_REPO="$FIXTURE/repo" MODE="$mode" \
  MCP_INLINE_LIMIT_CHARS="$MCP_INLINE_LIMIT_CHARS" MCP_BASE64_LIMIT_BYTES="$MCP_BASE64_LIMIT_BYTES" \
  MAX_UPLOAD_BYTES=2097152 \
  MCP_UPLOAD_SESSION_TTL_SECONDS="$MCP_UPLOAD_SESSION_TTL_SECONDS" \
  MCP_MAX_CONCURRENT_UPLOADS="$MCP_MAX_CONCURRENT_UPLOADS" \
    python3 tests/mcp_escape_suite.py || FAIL=1

  echo
  echo "--- bridge: local upload/download client (docs/feature-request-path-based-upload.md) ---"
  if python3 -c "import ssl" >/dev/null 2>&1; then
    BASE_URL="127.0.0.1:$port" API_TOKEN="$TOKEN" API_TOKEN_READONLY="$RO_TOKEN" \
    FIXTURE_REPO="$FIXTURE/repo" MODE="$mode" MAX_UPLOAD_BYTES=2097152 \
      python3 tests/bridge_tests.py || FAIL=1
  else
    # Some hosts (e.g. unRAID) ship a python3 whose ssl module cannot load, and
    # the bridge imports ssl. Run the same test inside the image, which has one.
    echo "  (host python3 cannot import ssl; running the bridge tests inside $IMAGE)"
    docker run --rm --network host --user 0:0 --entrypoint python \
      -v "$ROOT:/src:ro" -v "$FIXTURE/repo:$FIXTURE/repo" \
      -e BASE_URL="127.0.0.1:$port" -e API_TOKEN="$TOKEN" -e API_TOKEN_READONLY="$RO_TOKEN" \
      -e FIXTURE_REPO="$FIXTURE/repo" -e MODE="$mode" -e MAX_UPLOAD_BYTES=2097152 \
      -e PYTHONDONTWRITEBYTECODE=1 -w /src "$IMAGE" tests/bridge_tests.py || FAIL=1
  fi

  echo
  echo "--- server log: denials recorded ---"
  docker logs "$name" 2>&1 | grep -c "denied" | sed 's/^/  denied requests logged: /' || true
  # Regression test for docs/plan-mcp-connector.md's "security regression":
  # FastMCP swallows a tool's exception into a 200 isError result, so a
  # rejected /mcp path would be invisible in the logs unless app/mcp_endpoint
  # .py's _guard logs it itself, tagged distinctly from the REST "denied ...".
  mcp_denied=$(docker logs "$name" 2>&1 | grep -c "denied MCP" || true)
  if [[ "$mcp_denied" -gt 0 ]]; then
    echo "  PASS  $mcp_denied MCP denial(s) logged (denied MCP ...)"
  else
    echo "  FAIL  no 'denied MCP ...' lines in the log -- MCP rejections are invisible"; FAIL=1
  fi

  echo
  echo "--- server log: upload-ticket secrets must never appear ---"
  local leaked=0 line secret
  while IFS= read -r line; do
    secret="${line#UPLOAD_SECRET_USED:}"
    [[ -z "$secret" ]] && continue
    if docker logs "$name" 2>&1 | grep -qF -- "$secret"; then
      echo "  FAIL  a ticket secret appeared in the container log"
      leaked=1
    fi
  done < <(grep '^UPLOAD_SECRET_USED:' "$test_out" || true)
  if [[ $leaked -eq 0 ]]; then
    echo "  PASS  no upload-ticket secret appeared in container logs"
  else
    FAIL=1
  fi

  docker rm -f "$name" >/dev/null 2>&1 || true
}

# A) production layout: only the subfolder is mounted
new_fixture   # must precede the call: $FIXTURE is expanded in the arguments
run_mode "isolated (only subfolder mounted)" cf-test-a 18080 \
  -v "$FIXTURE/repo/shared:/data"

# Prove the repo is genuinely absent from that container's namespace
echo
echo "==> Namespace check: what can the container actually see?"
new_fixture
docker run --rm -v "$FIXTURE/repo/shared:/data" --entrypoint python "$IMAGE" -c "
import os
print('  contents of /data      :', sorted(os.listdir('/data')))
print('  contents of /data/..   :', sorted(os.listdir('/data/..')))
print('  /data/../secrets.env   :', os.path.exists('/data/../secrets.env'))
print('  /data/../.git          :', os.path.exists('/data/../.git'))
"

# Misconfiguration check: a missing volume must fail closed, not silently
# accept uploads into the container's ephemeral rootfs.
echo
echo "==> Fail-closed check: no volume mounted"
# Capture first: under `set -o pipefail` the container's non-zero exit would
# otherwise mask a successful grep.
nomount_out=$(docker run --rm -e API_TOKEN=aaaaaaaaaaaaaaaaaaaa "$IMAGE" 2>&1 || true)
if grep -q "is not a mount point" <<<"$nomount_out"; then
  echo "  PASS  container refuses to start without a bind mount"
else
  echo "  FAIL  container started without a bind mount"; FAIL=1
fi

# Stale temp files from an interrupted upload are swept at startup.
echo
echo "==> Startup sweep: stale upload temp files"
new_fixture
touch "$FIXTURE/repo/shared/.upload-deadbeef.tmp" "$FIXTURE/repo/shared/nested/.upload-cafe.tmp"
if docker run --rm --user "$(id -u):$(id -g)" -e API_TOKEN=aaaaaaaaaaaaaaaaaaaa -v "$FIXTURE/repo/shared:/data" \
     --entrypoint python "$IMAGE" -c "
from app.safepath import Vault
v = Vault('/data')
print('  swept:', v.sweep_temp_files())
" | grep -q "swept: 2"; then
  echo "  PASS  stale temp files removed"
else
  echo "  FAIL  stale temp files not removed"; FAIL=1
fi

# B) weakened layout: the whole repo is mounted, app-level checks stand alone
new_fixture
run_mode "app-only (whole repo mounted, no mount boundary)" cf-test-b 18081 \
  -v "$FIXTURE/repo:/repo" -e DATA_ROOT=/repo/shared

# --------------------------------------------------------------------------
# Both /mcp off switches, tested separately (docs/plan-mcp-connector.md,
# test 8): a half-threaded flag's failure mode is a mounted-but-unlisted
# endpoint, so both assert the actual HTTP status, not just "no tool ran".
# --------------------------------------------------------------------------

echo
echo "==> Building image (WITH_MCP=0: mcp package not installed at all)"
docker build -q --build-arg WITH_MCP=0 -t "$IMAGE_NOMCP" . >/dev/null

echo
echo "==> Off switch 1/2: WITH_MCP=0 image serves REST with /mcp absent"
new_fixture
docker rm -f cf-test-nomcp >/dev/null 2>&1 || true
docker run "${common_flags[@]}" --name cf-test-nomcp -p "127.0.0.1:18082:8080" \
  -v "$FIXTURE/repo/shared:/data" "$IMAGE_NOMCP" >/dev/null
wait_healthy cf-test-nomcp 18082
nomcp_status=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:18082/mcp" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -d '{}')
rest_status=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:18082/v1/list" \
  -H "Authorization: Bearer $TOKEN")
if [[ "$nomcp_status" == "404" && "$rest_status" == "200" ]]; then
  echo "  PASS  /mcp -> 404 (mcp not installed), REST API unaffected (200)"
else
  echo "  FAIL  /mcp -> $nomcp_status (wanted 404), REST -> $rest_status (wanted 200)"; FAIL=1
fi
# The line is logged at import time, well before /health can respond, but
# docker logs' own delivery to this CLI call can lag a beat behind the
# container actually being healthy -- retry briefly rather than requiring
# one immediate grep to win a race that isn't the thing under test.
nomcp_log_ok=0
for _ in $(seq 1 10); do
  if docker logs cf-test-nomcp 2>&1 | grep -q "mcp not installed"; then
    nomcp_log_ok=1; break
  fi
  sleep 0.3
done
if [[ "$nomcp_log_ok" == "1" ]]; then
  echo "  PASS  log explains why /mcp is absent"
else
  echo "  FAIL  log does not explain the missing /mcp mount"
  docker logs cf-test-nomcp 2>&1 | tail -20
  FAIL=1
fi
docker rm -f cf-test-nomcp >/dev/null 2>&1 || true

echo
echo "==> Off switch 2/2: ENABLE_MCP=false 404s even though mcp IS installed"
new_fixture
docker rm -f cf-test-offswitch >/dev/null 2>&1 || true
docker run "${common_flags[@]}" --name cf-test-offswitch -p "127.0.0.1:18083:8080" \
  -e ENABLE_MCP=false -v "$FIXTURE/repo/shared:/data" "$IMAGE" >/dev/null
wait_healthy cf-test-offswitch 18083
offswitch_status=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:18083/mcp" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -d '{}')
info_body=$(curl -s "http://127.0.0.1:18083/v1/info" -H "Authorization: Bearer $TOKEN")
if [[ "$offswitch_status" == "404" ]] && grep -q '"mcp_enabled":false' <<<"$info_body"; then
  echo "  PASS  ENABLE_MCP=false -> /mcp 404s, /v1/info reports mcp_enabled:false"
else
  echo "  FAIL  /mcp -> $offswitch_status (wanted 404), info -> $info_body"; FAIL=1
fi
docker rm -f cf-test-offswitch >/dev/null 2>&1 || true

# --------------------------------------------------------------------------
# Git tools (docs/plan-mcp-git-ops.md) in a REAL container, in the layout the
# "kernel boundary" claim rests on: the repository root mounted read-only, with
# only .git and the shared folder mounted read-write on top. tests/git_suite.py
# and tests/git_surface_tests.py cover the logic in-process; what only a real
# container can show is that this mount arrangement, the read-only rootfs, the
# noexec /tmp, and the uid mapping all work with the image's own git -- and that
# the kernel, not just the application, refuses a write outside the folder.
# --------------------------------------------------------------------------

echo
echo "==> Git mode: real image, repo root :ro with .git and shared/ writable"
GITFX="$FIXTURE_BASE/gitmode"
rm -rf "$GITFX"
mkdir -p "$GITFX/repo/shared" "$GITFX/repo/other"
(
  cd "$GITFX/repo"
  git init -q -b main
  git config user.email test@example.invalid
  git config user.name test
  echo "mine" > shared/a.txt
  echo "not yours" > other/b.txt
  git add -A
  git commit -q -m init
  # Startup validates the URL's scheme but never contacts the remote.
  git remote add origin ssh://git@example.invalid/org/repo.git
)
docker rm -f cf-test-git >/dev/null 2>&1 || true
docker run "${common_flags[@]}" --name cf-test-git -p "127.0.0.1:18084:8080" \
  -v "$GITFX/repo:/repo:ro" \
  -v "$GITFX/repo/.git:/repo/.git" \
  -v "$GITFX/repo/shared:/repo/shared" \
  -e DATA_ROOT=/repo/shared -e GIT_REPO_ROOT=/repo \
  -e ENABLE_GIT_READ=true -e ENABLE_GIT_WRITE=true -e GIT_BRANCH=main \
  "$IMAGE" >/dev/null
if wait_healthy cf-test-git 18084; then
  gj() {  # gj <expression over the parsed JSON on stdin>
    python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"
  }
  api() { curl -s -H "Authorization: Bearer $TOKEN" "$@"; }

  info=$(api http://127.0.0.1:18084/v1/info)
  if [[ "$(gj 'd["git"]["write_boundary"]' <<<"$info")" == "kernel" \
     && "$(gj 'd["git"]["scope"]' <<<"$info")" == "shared/" ]]; then
    echo "  PASS  /v1/info reports scope shared/ and a kernel write boundary"
  else
    echo "  FAIL  unexpected git info: $info"; FAIL=1
  fi

  # The kernel, not the application, refuses a write outside the folder.
  if docker exec cf-test-git python -c "open('/repo/other/b.txt','w')" >/dev/null 2>&1; then
    echo "  FAIL  the container could WRITE outside the shared folder"; FAIL=1
  else
    echo "  PASS  a write to /repo/other is refused by the read-only mount"
  fi
  if docker exec cf-test-git python -c "open('/repo/shared/probe','w').close()" >/dev/null 2>&1; then
    echo "  PASS  the shared folder is writable"
  else
    echo "  FAIL  the shared folder is not writable"; FAIL=1
  fi

  # git reads the whole repo; the agent's commit takes only its folder.
  api -X PUT "http://127.0.0.1:18084/v1/content/new.txt" --data-binary "agent file" >/dev/null
  echo "host edit" >> "$GITFX/repo/other/b.txt"           # a person edits outside the folder
  st=$(api http://127.0.0.1:18084/v1/git/status)
  if [[ "$(gj 'sorted((c["path"], c["in_scope"]) for c in d["changes"] if c["path"] != "shared/probe")' <<<"$st")" \
        == "[('other/b.txt', False), ('shared/new.txt', True)]" ]]; then
    echo "  PASS  git_status sees both folders, marking only shared/ as in scope"
  else
    echo "  FAIL  unexpected status: $st"; FAIL=1
  fi
  diff_body=$(api "http://127.0.0.1:18084/v1/git/diff")
  if grep -q "host edit" <<<"$diff_body"; then
    echo "  FAIL  git_diff leaked a change outside the shared folder"; FAIL=1
  else
    echo "  PASS  git_diff shows nothing from outside the shared folder"
  fi
  commit=$(api -X POST http://127.0.0.1:18084/v1/git/commit -H "Content-Type: application/json" \
           -d '{"message":"agent: container commit"}')
  changed=$(git -C "$GITFX/repo" diff-tree -r --no-commit-id --name-only HEAD | sort | tr '\n' ' ')
  if [[ "$(gj 'd.get("ok")' <<<"$commit")" == "True" && "$changed" == "shared/new.txt shared/probe " ]] \
     && [[ "$(git -C "$GITFX/repo" log -1 --format=%s)" == "agent: container commit" ]]; then
    echo "  PASS  git_commit committed only the shared folder, in a real container"
  else
    echo "  FAIL  commit result: $commit; changed files: $changed"; FAIL=1
  fi
  if grep -q "host edit" "$GITFX/repo/other/b.txt"; then
    echo "  PASS  the host's edit outside the folder is untouched"
  else
    echo "  FAIL  the host's edit outside the folder was lost"; FAIL=1
  fi
  push=$(api -X POST http://127.0.0.1:18084/v1/git/push -H "Content-Type: application/json" -d '{}')
  if [[ "$(gj 'd.get("code")' <<<"$push")" == "stale_remote" ]]; then
    echo "  PASS  push without a fetched remote ref is refused (stale_remote), no network used"
  else
    echo "  FAIL  push result: $push"; FAIL=1
  fi
  if docker logs cf-test-git 2>&1 | grep -qi "mounted read-write"; then
    echo "  FAIL  the read-only layout logged the 'root mounted read-write' warning"; FAIL=1
  else
    echo "  PASS  no read-write-root warning in the read-only layout"
  fi
else
  # `if wait_healthy` disables errexit for the call, so without this branch a
  # git-mode container that never starts would skip every check above and the
  # script would still exit 0.
  echo "  FAIL  the git-mode container never became healthy (log above)"; FAIL=1
fi
docker rm -f cf-test-git >/dev/null 2>&1 || true

echo
echo "==> WITH_GIT=0: asking for the git tools on an image without git refuses to start"
docker build -q --build-arg WITH_GIT=0 -t "$IMAGE_NOGIT" . >/dev/null
new_fixture
nogit_out=$(docker run --rm -e API_TOKEN=aaaaaaaaaaaaaaaaaaaa -e ENABLE_GIT_READ=true \
            --user "$(id -u):$(id -g)" -v "$FIXTURE/repo/shared:/data" "$IMAGE_NOGIT" 2>&1 || true)
if grep -q "WITH_GIT=1" <<<"$nogit_out"; then
  echo "  PASS  refuses to start and names the build arg"
else
  echo "  FAIL  expected a startup error naming WITH_GIT=1; got: $nogit_out" | head -5; FAIL=1
fi

echo
if [[ $FAIL -eq 0 ]]; then
  echo "=============================================="
  echo " ALL MODES PASSED"
  echo "=============================================="
else
  echo "=============================================="
  echo " FAILURES -- see above"
  echo "=============================================="
fi
exit $FAIL
