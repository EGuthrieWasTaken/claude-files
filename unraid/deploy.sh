#!/usr/bin/env bash
# Redeploy claude-files after a new image version ships, and check from a
# client's point of view that the new version is what's actually serving.
#
# Works whether this container is managed by `docker compose` (this repo's
# docker-compose.yml) or was created through unRAID's own Docker Manager UI
# from unraid/claude-files.xml -- it detects which, and for the latter it
# tells you the one manual step it deliberately does NOT try to automate:
# a GUI-created container's exact `docker run` flags (bind mount, hardening
# flags, env) live in unRAID's saved template, not anywhere this script can
# see. Replaying them blind risks silently dropping one -- unRAID's own
# "Force Update" button reads that template and can't get it wrong.
#
# Usage: unraid/deploy.sh [build|pull|deploy|verify|all]
#   (no argument runs: deploy, then verify)
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-ghcr.io/eguthriewastaken/claude-files}"
CONTAINER_NAME="${CONTAINER_NAME:-claude-files}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

_default_tag() {
  # Whatever docker-compose.yml is currently pinned to -- the same source of
  # truth README.md points at ("docker-compose.yml stays pinned ... on
  # purpose"). Falls back to 1.4.0 if the file or the pin isn't found, so
  # this script still runs from outside a checkout.
  grep -oE "${IMAGE//\//\\/}:[0-9][0-9A-Za-z.+-]*" "$COMPOSE_FILE" 2>/dev/null | head -1 | cut -d: -f2
}
TAG="${TAG:-$(_default_tag)}"
TAG="${TAG:-1.4.0}"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
ok()   { echo "  ${GREEN}OK${RESET}    $*"; }
bad()  { echo "  ${RED}FAIL${RESET}  $*"; }
note() { echo "  ${YELLOW}--${RESET}    $*"; }

usage() {
  cat <<USAGE
Usage: $(basename "$0") [build|pull|deploy|verify|all]

  build    docker buildx build + push a multi-arch image to
           ${IMAGE} (needs buildx and a login to that registry).
           Official images are built by CI when a GitHub release vX.Y.Z
           is published (README.md, "Cutting a release"); this is for
           publishing by hand, or your own build under your own IMAGE.
  pull     docker pull ${IMAGE}:${TAG}
  deploy   pull, then recreate the container (via docker compose, if that's
           how it's managed -- otherwise prints the unRAID "Force Update"
           step) and wait for /health
  verify   check the redeployed server from a CLIENT's point of view --
           /health's version, /v1/info's bridge_available, GET /v1/bridge.py
  all      build, then deploy, then verify

  No argument: deploy, then verify.

Environment overrides: IMAGE, TAG (default: whatever $COMPOSE_FILE is
pinned to, currently resolves to '${TAG}'), CONTAINER_NAME, COMPOSE_FILE,
API_TOKEN (verify falls back to reading API_TOKEN from a .env file next to
COMPOSE_FILE if this isn't set).
USAGE
}

is_compose_managed() {
  docker inspect "$CONTAINER_NAME" \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
    2>/dev/null | grep -q .
}

cmd_build() {
  echo "==> docker buildx build --push ${IMAGE}:${TAG} (+ :latest), linux/amd64+arm64"
  docker buildx build --platform linux/amd64,linux/arm64 \
    --tag "${IMAGE}:${TAG}" --tag "${IMAGE}:latest" --push .
  echo "==> Confirming both architectures landed under one tag"
  docker buildx imagetools inspect "${IMAGE}:${TAG}"
}

cmd_pull() {
  echo "==> docker pull ${IMAGE}:${TAG}"
  docker pull "${IMAGE}:${TAG}"
}

cmd_deploy() {
  cmd_pull
  if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    note "No running container named '$CONTAINER_NAME' found."
    note "Start it the way README.md 'Quick start' or 'Running on unRAID' describes,"
    note "then re-run: $(basename "$0") verify"
    return 0
  fi
  if is_compose_managed && [[ -f "$COMPOSE_FILE" ]]; then
    echo "==> '$CONTAINER_NAME' is docker-compose-managed -- recreating via $COMPOSE_FILE"
    docker compose -f "$COMPOSE_FILE" up -d claude-files
  else
    note "'$CONTAINER_NAME' was not created by docker compose (no compose label"
    note "found) -- this is almost certainly an unRAID Docker Manager / template"
    note "container. Recreating it from here would mean replaying its exact"
    note "\`docker run\` flags blind, which risks silently dropping a bind mount"
    note "or a hardening flag. Instead:"
    note ""
    note "  unRAID WebUI -> Docker tab -> '$CONTAINER_NAME' -> Force Update"
    note "  (or: click the container icon -> Edit -> Apply -- either re-pulls"
    note "  and recreates it from the saved template)"
    note ""
    note "Then re-run: $(basename "$0") verify"
    return 0
  fi
  wait_healthy
}

host_endpoint() {
  local mapping
  mapping=$(docker port "$CONTAINER_NAME" 8080/tcp 2>/dev/null | head -1) || true
  if [[ -z "$mapping" ]]; then
    echo "127.0.0.1:8080"
  else
    echo "127.0.0.1:${mapping##*:}"
  fi
}

wait_healthy() {
  local endpoint
  endpoint=$(host_endpoint)
  echo "==> Waiting for http://${endpoint}/health"
  for _ in $(seq 1 30); do
    if curl -fsS "http://${endpoint}/health" >/dev/null 2>&1; then
      ok "healthy"
      return 0
    fi
    sleep 1
  done
  bad "never became healthy -- recent logs:"
  docker logs --tail=40 "$CONTAINER_NAME" 2>&1 || true
  return 1
}

json_str()  { grep -oE "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" <<<"$1" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/'; }
json_bool() { grep -oE "\"$2\"[[:space:]]*:[[:space:]]*(true|false)" <<<"$1" | head -1 | sed -E 's/.*:[[:space:]]*(true|false)/\1/'; }

resolve_token() {
  if [[ -n "${API_TOKEN:-}" ]]; then
    echo "$API_TOKEN"
    return
  fi
  local env_file
  env_file="$(dirname "$COMPOSE_FILE")/.env"
  if [[ -f "$env_file" ]]; then
    grep -E '^API_TOKEN=' "$env_file" | tail -1 | cut -d= -f2-
  fi
}

cmd_verify() {
  local endpoint health version info token bridge_status
  endpoint=$(host_endpoint)

  echo "==> GET http://${endpoint}/health"
  health=$(curl -fsS "http://${endpoint}/health") || { bad "unreachable at http://${endpoint}"; return 1; }
  echo "  $health"
  version=$(json_str "$health" version)
  if [[ "$version" == "$TAG" ]]; then
    ok "version is $TAG"
  else
    bad "version is '${version:-?}', expected $TAG -- redeploy did not take, or TAG doesn't match what was actually pushed"
  fi

  token=$(resolve_token)
  if [[ -z "$token" ]]; then
    note "No API_TOKEN found (checked \$API_TOKEN and $(dirname "$COMPOSE_FILE")/.env)."
    note "Skipping the authenticated checks -- pass one: API_TOKEN=... $(basename "$0") verify"
    return 0
  fi

  echo "==> GET http://${endpoint}/v1/info"
  info=$(curl -fsS -H "Authorization: Bearer ${token}" "http://${endpoint}/v1/info") \
    || { bad "unreachable, or the token is wrong"; return 1; }
  echo "  $info"
  [[ "$(json_bool "$info" bridge_available)" == "true" ]] \
    && ok "bridge_available: true" \
    || bad "bridge_available is not true -- this deployment predates the bridge feature"
  [[ "$(json_bool "$info" mcp_enabled)" == "true" ]] \
    && ok "mcp_enabled: true" \
    || note "mcp_enabled is false (ENABLE_MCP=false?) -- fine if that's intentional"

  echo "==> GET http://${endpoint}/v1/bridge.py"
  bridge_status=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${token}" "http://${endpoint}/v1/bridge.py")
  [[ "$bridge_status" == "200" ]] \
    && ok "GET /v1/bridge.py -> 200" \
    || bad "GET /v1/bridge.py -> $bridge_status (expected 200)"

  echo
  note "If a claude.ai connector still shows the old tool list, its tool cache"
  note "needs refreshing -- remove and re-add the connector (README.md, 'MCP connector')."
}

cmd_all() { cmd_build; cmd_deploy; cmd_verify; }

cmd="${1:-deploy_and_verify}"
case "$cmd" in
  build)  cmd_build ;;
  pull)   cmd_pull ;;
  deploy) cmd_deploy ;;
  verify) cmd_verify ;;
  all)    cmd_all ;;
  deploy_and_verify) cmd_deploy; cmd_verify ;;
  -h|--help) usage ;;
  *) usage; exit 1 ;;
esac
