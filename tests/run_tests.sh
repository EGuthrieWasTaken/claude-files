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
FIXTURE="${FIXTURE_DIR:-$ROOT/.fixture}"
IMAGE="claude-files:test"
TOKEN="test-token-$(openssl rand -hex 16)"
RO_TOKEN="test-ro-token-$(openssl rand -hex 16)"
FAIL=0

cleanup() {
  docker rm -f cf-test-a cf-test-b >/dev/null 2>&1 || true
  [[ "${KEEP_FIXTURE:-0}" == "1" ]] || rm -rf "$FIXTURE"
}
trap cleanup EXIT

echo "==> Building image"
docker build -q -t "$IMAGE" . >/dev/null

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
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m
  --cap-drop ALL
  --security-opt no-new-privileges:true
  --pids-limit 200
  --memory 512m
  -e API_TOKEN="$TOKEN"
  -e API_TOKEN_READONLY="$RO_TOKEN"
  -e MAX_UPLOAD_BYTES=2097152
  -e LOG_LEVEL=INFO
)

run_mode() {
  local mode=$1 name=$2 port=$3; shift 3
  echo
  echo "############################################################"
  echo "## MODE: $mode"
  echo "############################################################"
  bash tests/make_fixture.sh "$FIXTURE" >/dev/null
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run "${common_flags[@]}" --name "$name" -p "127.0.0.1:$port:8080" "$@" "$IMAGE" >/dev/null
  wait_healthy "$name" "$port"

  BASE_URL="127.0.0.1:$port" API_TOKEN="$TOKEN" API_TOKEN_READONLY="$RO_TOKEN" \
  FIXTURE_REPO="$FIXTURE/repo" MODE="$mode" \
    python3 tests/escape_suite.py || FAIL=1

  echo
  echo "--- server log: denials recorded ---"
  docker logs "$name" 2>&1 | grep -c "denied" | sed 's/^/  denied requests logged: /' || true
  docker rm -f "$name" >/dev/null 2>&1 || true
}

# A) production layout: only the subfolder is mounted
run_mode "isolated (only subfolder mounted)" cf-test-a 18080 \
  -v "$FIXTURE/repo/shared:/data"

# Prove the repo is genuinely absent from that container's namespace
echo
echo "==> Namespace check: what can the container actually see?"
bash tests/make_fixture.sh "$FIXTURE" >/dev/null
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
bash tests/make_fixture.sh "$FIXTURE" >/dev/null
touch "$FIXTURE/repo/shared/.upload-deadbeef.tmp" "$FIXTURE/repo/shared/nested/.upload-cafe.tmp"
if docker run --rm -e API_TOKEN=aaaaaaaaaaaaaaaaaaaa -v "$FIXTURE/repo/shared:/data" \
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
run_mode "app-only (whole repo mounted, no mount boundary)" cf-test-b 18081 \
  -v "$FIXTURE/repo:/repo" -e DATA_ROOT=/repo/shared

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
