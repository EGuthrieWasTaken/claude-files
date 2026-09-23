#!/usr/bin/env bash
# Builds a fake git repo:
#
#   repo/
#     .git/                 <- must never be reachable
#     secrets.env           <- must never be reachable
#     src/deploy_key        <- must never be reachable
#     shared/               <- the ONLY folder the API may touch
#       link-to-secret      -> ../secrets.env        (hostile symlink)
#       escape-dir          -> ..                    (hostile symlink)
#       rootlink            -> /                     (hostile symlink)
#       nested/deep-link    -> ../../secrets.env     (hostile symlink)
#       hardlink-to-secret                           (hostile hard link)
#
# The symlinks and hard link simulate a host-side attacker (or an accident)
# planting escape hatches inside the folder the agent can write to.
set -euo pipefail

FIXTURE="${1:?usage: make_fixture.sh <dir>}"
rm -rf "$FIXTURE"
mkdir -p "$FIXTURE/repo/.git/objects" "$FIXTURE/repo/src" "$FIXTURE/repo/shared/nested"

cd "$FIXTURE/repo"

echo "CANARY_SECRETS_ENV_a1b2c3d4" > secrets.env
echo "CANARY_GIT_CONFIG_e5f6a7b8"  > .git/config
echo "CANARY_DEPLOY_KEY_c9d0e1f2"  > src/deploy_key
echo "ordinary source file"        > src/app.py

echo "hello from the shared folder" > shared/existing.txt

# Hostile links planted inside the shared folder.
ln -s ../secrets.env       shared/link-to-secret
ln -s ..                   shared/escape-dir
ln -s /                    shared/rootlink
ln -s ../../secrets.env    shared/nested/deep-link
ln    secrets.env          shared/hardlink-to-secret

# Separate copies dedicated to "delete a symlink, spare its target" tests.
# escape_suite.py (REST) and mcp_escape_suite.py (MCP) both run this check,
# and tests/run_tests.sh runs them back-to-back against the same fixture --
# deleting link-to-secret/escape-dir themselves would remove the read/write
# escape targets the suite that runs second still needs.
ln -s ../secrets.env       shared/link-to-secret-del
ln -s ..                   shared/escape-dir-del

echo "fixture ready at $FIXTURE/repo"
