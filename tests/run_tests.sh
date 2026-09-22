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
  docker rm -f cf-test-a cf-test-b cf-test-nomcp cf-test-offswitch >/dev/null 2>&1 || true
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

  BASE_URL="127.0.0.1:$port" API_TOKEN="$TOKEN" API_TOKEN_READONLY="$RO_TOKEN" \
  FIXTURE_REPO="$FIXTURE/repo" MODE="$mode" \
    python3 tests/escape_suite.py || FAIL=1

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
