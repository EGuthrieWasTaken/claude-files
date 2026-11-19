#!/usr/bin/env bash
# The release version, and the checks CI and the release workflow run on it.
#
# app/main.py's `version="X.Y.Z"` is the single source of truth (it is what
# GET /health reports). Both Docker images, the API and the autocommit sidecar,
# are published under that same tag.
#
#   check-version.sh consistent   the version is X.Y.Z, and docker-compose.yml's
#                                 two pins and unraid/deploy.sh's fallback match it
#   check-version.sh unpublished  ...and neither image has that tag on Docker Hub
#                                 yet, and there is no git tag vX.Y.Z (every PR
#                                 must ship a version that has never been released)
#   check-version.sh release vX.Y.Z
#                                 ...and the release tag is exactly v<version>,
#                                 and the version is still unpublished
#   check-version.sh print        just print the version
#
# Docker Hub is asked over its public API. Any answer other than "tag exists"
# (200) or "no such tag" (404) fails the check rather than passing it: a
# release must never overwrite a published tag because Hub was unreachable.
set -euo pipefail

cd "$(dirname "$0")/../.."

NAMESPACE="${DOCKERHUB_NAMESPACE:-eguthriewastaken2}"
IMAGES=(claude-files claude-files-autocommit)

fail() { echo "::error::$*" >&2; echo "FAIL: $*" >&2; exit 1; }

version() {
  local v
  v=$(sed -nE 's/^[[:space:]]*version="([^"]*)",[[:space:]]*$/\1/p' app/main.py)
  [[ -n "$v" ]] || fail "no version=\"...\" line found in app/main.py"
  [[ $(wc -l <<<"$v") -eq 1 ]] || fail "more than one version=\"...\" line in app/main.py"
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "app/main.py version '$v' is not X.Y.Z"
  echo "$v"
}

check_consistent() {
  local v=$1 img pin fallback
  for img in "${IMAGES[@]}"; do
    pin=$(grep -oE "image: ${NAMESPACE}/${img}:[^[:space:]]+" docker-compose.yml | head -1 | sed 's/.*://')
    [[ "$pin" == "$v" ]] \
      || fail "docker-compose.yml pins ${NAMESPACE}/${img}:${pin:-<none>}, but app/main.py says $v -- bump the pin too"
  done
  fallback=$(sed -nE 's/^TAG="\$\{TAG:-([0-9][^}]*)\}"$/\1/p' unraid/deploy.sh)
  [[ "$fallback" == "$v" ]] \
    || fail "unraid/deploy.sh falls back to TAG=${fallback:-<none>}, but app/main.py says $v -- bump it too"
  echo "ok: version $v; docker-compose.yml and unraid/deploy.sh agree"
}

hub_status() {  # 200 = tag exists, 404 = it doesn't
  curl -sS -o /dev/null -w '%{http_code}' --retry 3 --retry-delay 2 --max-time 20 \
    "https://hub.docker.com/v2/namespaces/${NAMESPACE}/repositories/$1/tags/$2" || echo "curl-failed"
}

check_unpublished() {
  local v=$1 img code
  for img in "${IMAGES[@]}"; do
    code=$(hub_status "$img" "$v")
    case "$code" in
      404) echo "ok: ${NAMESPACE}/${img}:${v} is not on Docker Hub yet" ;;
      200) fail "${NAMESPACE}/${img}:${v} is already published on Docker Hub. Bump version= in app/main.py (and the pins in docker-compose.yml and unraid/deploy.sh) to a version that has never been released." ;;
      *)   fail "could not ask Docker Hub whether ${NAMESPACE}/${img}:${v} exists (HTTP ${code}); refusing to guess" ;;
    esac
  done
  if git ls-remote --exit-code --tags origin "refs/tags/v${v}" >/dev/null 2>&1; then
    fail "git tag v${v} already exists: that version was released. Bump to a new version."
  fi
  echo "ok: no git tag v${v}"
}

mode="${1:-}"
case "$mode" in
  print)
    version ;;
  consistent)
    check_consistent "$(version)" ;;
  unpublished)
    v=$(version); check_consistent "$v"; check_unpublished "$v" ;;
  release)
    tag="${2:-}"; [[ -n "$tag" ]] || fail "usage: $0 release vX.Y.Z"
    v=$(version); check_consistent "$v"
    [[ "$tag" == "v${v}" ]] || fail "release tag '$tag' does not match app/main.py version $v (expected v${v})"
    for img in "${IMAGES[@]}"; do
      code=$(hub_status "$img" "$v")
      case "$code" in
        404) echo "ok: ${NAMESPACE}/${img}:${v} is free" ;;
        200) fail "${NAMESPACE}/${img}:${v} is already on Docker Hub; a release never overwrites a published tag" ;;
        *)   fail "could not ask Docker Hub whether ${NAMESPACE}/${img}:${v} exists (HTTP ${code}); refusing to guess" ;;
      esac
    done ;;
  *)
    echo "usage: $0 {print|consistent|unpublished|release vX.Y.Z}" >&2; exit 2 ;;
esac
