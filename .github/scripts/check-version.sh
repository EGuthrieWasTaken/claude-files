#!/usr/bin/env bash
# The release version, and the checks CI and the release workflow run on it.
#
# app/main.py's `version="X.Y.Z"` is the single source of truth (it is what
# GET /health reports). Both images, the API and the autocommit sidecar, are
# published to GHCR under that same tag, by .github/workflows/release.yml only.
#
#   check-version.sh consistent   the version is X.Y.Z, and docker-compose.yml's
#                                 two pins and unraid/deploy.sh's fallback match it
#   check-version.sh unpublished  ...and the version was never released: no git
#                                 tag vX.Y.Z (every release creates one), and not
#                                 one of the versions once published on Docker Hub
#   check-version.sh release vX.Y.Z
#                                 ...and the release tag is exactly v<version>, and
#                                 neither image already has that tag on GHCR
#   check-version.sh print        just print the version
set -euo pipefail

cd "$(dirname "$0")/../.."

REGISTRY="${REGISTRY:-ghcr.io/eguthriewastaken}"
IMAGES=(claude-files claude-files-autocommit)

# Versions published to Docker Hub (eguthriewastaken2/*) before images moved to
# GHCR. That repository gets no new tags, so the list is fixed: these numbers
# mean other images and are never reused. (1.2.0 was pushed there on
# 2026-09-23 from a build that still reported 1.1.0.)
LEGACY_DOCKERHUB_VERSIONS="1.0.0 1.0.1 1.1.0 1.2.0"

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
    pin=$(grep -oE "image: ${REGISTRY}/${img}:[^[:space:]]+" docker-compose.yml | head -1 | sed 's/.*://')
    [[ "$pin" == "$v" ]] \
      || fail "docker-compose.yml pins ${REGISTRY}/${img}:${pin:-<none>}, but app/main.py says $v -- bump the pin too"
  done
  fallback=$(sed -nE 's/^TAG="\$\{TAG:-([0-9][^}]*)\}"$/\1/p' unraid/deploy.sh)
  [[ "$fallback" == "$v" ]] \
    || fail "unraid/deploy.sh falls back to TAG=${fallback:-<none>}, but app/main.py says $v -- bump it too"
  echo "ok: version $v; docker-compose.yml and unraid/deploy.sh agree"
}

check_not_legacy() {
  local v=$1 old
  for old in $LEGACY_DOCKERHUB_VERSIONS; do
    [[ "$v" != "$old" ]] \
      || fail "$v was already published (on Docker Hub, before images moved to GHCR). Bump to a version that has never been released."
  done
  echo "ok: $v is not one of the old Docker Hub versions ($LEGACY_DOCKERHUB_VERSIONS)"
}

check_no_git_tag() {
  local v=$1
  if git ls-remote --exit-code --tags origin "refs/tags/v${v}" >/dev/null 2>&1; then
    fail "git tag v${v} already exists: that version was released. Bump to a new version."
  fi
  echo "ok: no git tag v${v}"
}

# GHCR's registry API. A token for the pull scope, then a HEAD on the manifest:
# 200 = the tag exists, 404 = it doesn't. With GITHUB_TOKEN set (the release
# workflow), the token request is authenticated, so private packages this
# repository's workflow pushed are visible. A denial means the token can't see
# the package at all: release.yml is the only publisher and its token can read
# every package it pushed, so that means the package doesn't exist yet (the
# first release). Anything else fails the check rather than guessing.
ghcr_status() {
  local repo=$1 tag=$2 tok resp
  local accept="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    resp=$(curl -sS --max-time 20 -u "${GITHUB_ACTOR:-x}:${GITHUB_TOKEN}" \
      "https://ghcr.io/token?scope=repository:${repo}:pull") || { echo "curl-failed"; return; }
  else
    resp=$(curl -sS --max-time 20 "https://ghcr.io/token?scope=repository:${repo}:pull") || { echo "curl-failed"; return; }
  fi
  if grep -q '"DENIED"' <<<"$resp"; then echo "denied"; return; fi
  tok=$(sed -nE 's/.*"token":"([^"]+)".*/\1/p' <<<"$resp")
  [[ -n "$tok" ]] || { echo "no-token"; return; }
  curl -sS -o /dev/null -w '%{http_code}' -I --max-time 20 \
    -H "Authorization: Bearer ${tok}" -H "Accept: ${accept}" \
    "https://ghcr.io/v2/${repo}/manifests/${tag}" || echo "curl-failed"
}

check_not_on_ghcr() {
  local v=$1 img repo code
  for img in "${IMAGES[@]}"; do
    repo="${REGISTRY#ghcr.io/}/${img}"
    code=$(ghcr_status "$repo" "$v")
    case "$code" in
      404)    echo "ok: ghcr.io/${repo}:${v} is not published" ;;
      denied) echo "::warning::ghcr.io/${repo} is not visible to this token; treating it as not created yet (first release)"
              echo "ok: ghcr.io/${repo} does not exist yet" ;;
      200)    fail "ghcr.io/${repo}:${v} is already published; a release never overwrites a published tag" ;;
      *)      fail "could not ask GHCR whether ghcr.io/${repo}:${v} exists (${code}); refusing to guess" ;;
    esac
  done
}

mode="${1:-}"
case "$mode" in
  print)
    version ;;
  consistent)
    check_consistent "$(version)" ;;
  unpublished)
    v=$(version); check_consistent "$v"; check_not_legacy "$v"; check_no_git_tag "$v" ;;
  release)
    tag="${2:-}"; [[ -n "$tag" ]] || fail "usage: $0 release vX.Y.Z"
    v=$(version); check_consistent "$v"
    [[ "$tag" == "v${v}" ]] || fail "release tag '$tag' does not match app/main.py version $v (expected v${v})"
    check_not_legacy "$v"; check_not_on_ghcr "$v" ;;
  *)
    echo "usage: $0 {print|consistent|unpublished|release vX.Y.Z}" >&2; exit 2 ;;
esac
