# Security policy

claude-files exists to confine an agent to one folder, so a way around that
confinement is the most serious kind of bug it can have.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through GitHub's
vulnerability reporting instead: on the repository's **Security** tab, choose
**Report a vulnerability**
([direct link](https://github.com/EGuthrieWasTaken/claude-files/security/advisories/new)).

Helpful to include:

- the version (`GET /health` reports it) and whether you built the image
  yourself;
- how it's deployed: the volume mounts, and which of `ENABLE_MCP`,
  `ENABLE_GIT_READ`, `ENABLE_GIT_WRITE` are on;
- the requests or tool calls that reproduce it.

Reports are handled on a best-effort basis. Once a fix is released, the
advisory is published with credit to the reporter unless you'd rather not be
named.

## Supported versions

Fixes go into the next release. Only the latest release is supported, so
upgrade before reporting if you can.

## In scope

- Reading, writing, listing, or deleting anything outside the shared folder,
  by any route: path traversal, symlinks, hard links, races, or a path the
  REST API and `/mcp` handle differently.
- Getting past the token check, doing a write with the read-only token, or
  using an upload or download ticket for anything beyond its one path, one
  use, and lifetime.
- A token, ticket secret, or git credential appearing in a log line, error, or
  response.
- The git tools changing a path outside the agent's folder, running a hook or
  any other program named in git config, or talking to a remote other than
  the configured one.
- The autocommit sidecar committing outside its `SUBDIR`.

## Out of scope

The limits in the README's
[What this does not protect against](README.md#what-this-does-not-protect-against):
anyone with direct access to the host folder, a leaked token, malicious
content an agent writes into the folder, and kernel or container-runtime
escapes. A deployment that bind-mounts the repository root as `/data` has
handed the agent the whole repository by design.
