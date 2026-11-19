# Design records

These are the design briefs each feature was built from, kept so the
reasoning behind the security-sensitive choices can be audited. They are not
user documentation: for how to run and use claude-files, start with the
[top-level README](../README.md), and for the brief to hand an agent, see
[`AGENT.md`](../AGENT.md).

Each brief was written before its feature was built. The status line at the
top of each says what shipped, and most end with **Implementation notes**
recording where the code differs from the plan. Read those before assuming
the main text describes the code exactly. Incident
reports quoted in them are verbatim agent output; the hostnames and file paths
in examples are placeholders.

| Record | What it covers |
|---|---|
| [`plan-mcp-connector.md`](plan-mcp-connector.md) | The `/mcp` endpoint: auth header, dependency pins, mounting the MCP SDK inside FastAPI, and why REST stays. |
| [`plan-mcp-large-writes.md`](plan-mcp-large-writes.md) | Silent truncation of base64 writes, the integrity floor (`expect_bytes` / `expect_sha256`), and chunked write sessions. |
| [`feature-request-path-based-upload.md`](feature-request-path-based-upload.md) | The local bridge (`bridge/`), and why a model's own output ceiling is the binding limit on uploads. |
| [`upload-urls.md`](upload-urls.md) | One-time upload tickets (`request_upload`, `POST /v1/uploads`). |
| [`download-urls.md`](download-urls.md) | One-time download tickets (`request_download`, `POST /v1/downloads`), PDF text extraction and ranged reads. |
| [`plan-mcp-git-ops.md`](plan-mcp-git-ops.md) | The scoped git tools: the scope model, the threat model, and how git is kept from running agent-written code. |
