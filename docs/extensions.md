# Extension modules: extra MCP tools from a derived image

`EXTRA_TOOL_MODULES` lets a separate project add tools to this server's `/mcp`
endpoint without forking it. The motivating case is an org-mode toolset
(`org_list_items`, `org_set_state`, `org_reschedule`, ...) for one deployment
that serves an Emacs folder. Those tools are useless to everyone else, so they
live in their own repository and image, and this one carries only the hook.

## Shape

```
claude-files (this repo)                    claude-files-org (separate repo)
  app/extensions.py   <-- the contract        claude_files_org/__init__.py   register(api)
  EXTRA_TOOL_MODULES  <-- the switch          Dockerfile:
                                                FROM ghcr.io/eguthriewastaken/claude-files:<pinned tag>
                                                COPY claude_files_org/ /srv/claude_files_org/
                                                ENV EXTRA_TOOL_MODULES=claude_files_org
```

The derived image is a drop-in replacement: same port, env vars, token
handling, `/mcp` path and volume layout. Swapping a deployment over is a
change of image name. Which claude-files a derived image runs is its pinned
`FROM` tag, so "how far behind upstream are we" is a one-line diff, and
picking up upstream fixes is bumping that tag and re-running the extension's
tests.

## The contract

An extension is any importable Python module. Put it on `sys.path`: `/srv` is
on it already, because uvicorn runs from there, or `pip install` it into
`/venv`. The module provides:

| Name | Required | Meaning |
|---|---|---|
| `register(api)` | yes | Called once at startup, after every core tool is registered. Adds tools with `api.add_tool`. |
| `INSTRUCTIONS` | no | A string appended to the MCP server instructions. Use it to tell the agent when to prefer these tools over `read_file`/`write_file`. |
| `REQUIRES_API` | no, default `1` | The server refuses to start if this is greater than `app.extensions.EXTENSION_API_VERSION`. |

`api` is an `ExtensionAPI`:

| Member | What it is |
|---|---|
| `api.add_tool(fn, *, read_only, name=None, title=None, description=None, destructive=False, open_world=False)` | Registers `fn`. **`read_only` is required.** With `read_only=False`, the server refuses the call for a read-only token *before* `fn` runs, so an extension can't forget that check. The description defaults to `fn`'s docstring. A name already taken by a core tool or an earlier extension stops the server from starting. |
| `api.vault` | The shared `app.safepath.Vault`. **All file access goes through it.** It provides path containment, denied names, symlink and hard-link refusal, atomic writes, and the worktree gate that keeps writes from interleaving with a `git_pull`. An extension that opens files with `open()` gets none of those. |
| `api.guard(op, fn, *args)` | Runs `fn` the way core tools do. A `PathError`/`GitOpError`/`ValueError` is logged as `denied MCP <op>` and reaches the agent as a tool error carrying its message. Wrap every Vault call in it. |
| `await api.run_in_thread(op, fn)` | `guard` in a worker thread, for an `async def` tool doing slow work. |
| `api.require_write(ctx)` / `api.token_is_rw(ctx)` | For a `read_only=True` tool that has an optional write mode. |
| `api.config` | The frozen `app.config.Config`. |
| `api.git` | The `app.git_ops.GitRepo` when git mode is on, else `None`. |
| `api.log` | A logger named `claude-files.ext.<module>`. |
| `api.api_version`, `api.name` | The contract version, and this module's name. |

The extension gets neither the FastMCP object nor the FastAPI app. Every
additional thing it can reach is another thing a claude-files change could
break, so the surface stays this narrow. Adding a member doesn't change
`EXTENSION_API_VERSION`. Removing or changing one does.

### Two gotchas, both from the pinned MCP SDK

- **No `from __future__ import annotations` in a module that defines tools.**
  `mcp==1.12.4` inspects tool signatures without evaluating string
  annotations, so with that import every tool fails to register. The comment
  above the imports in `app/mcp_endpoint.py` has the full story.
- **Take `ctx: Context` as a parameter if you need it.** The SDK fills it in
  and hides it from the tool's input schema. This still works for
  `read_only=False` tools, because the write gate is a `functools.wraps`
  wrapper and the SDK sees through it to `fn`'s real signature.

## Failure is loud

Each of these stops the server from starting, with a message naming the
module:

- the name isn't a dotted module name
- a name is listed twice
- the import fails, including an `ImportError` *inside* the module
- `register` is missing
- `REQUIRES_API` is too new
- a tool name is taken
- `register()` raises
- `EXTRA_TOOL_MODULES` is set in an image built with `WITH_MCP=0`

That last one matters because `app/main.py` treats an `ImportError` from
`mcp_endpoint` as "the SDK isn't installed" and quietly runs without `/mcp`.
`ExtensionError` is a `RuntimeError` precisely so a broken extension can never
take that path and leave the server running without its tools.

The one exception is `ENABLE_MCP=false`: it is the no-rebuild kill switch for
`/mcp`, so it still works with extensions configured. It logs a warning that
their tools are unreachable.

## Seeing what loaded

`get_folder_info` and `GET /v1/info` both carry:

```json
"extensions": {"api_version": 1, "modules": [{"module": "claude_files_org", "tools": ["org_list_items", "..."]}]}
```

## A minimal extension

```python
# claude_files_org/__init__.py -- no `from __future__ import annotations`
import os

INSTRUCTIONS = "Use org_list_items for inbox.org rather than reading the whole file."

_api = None


def org_list_items(path: str = "inbox.org") -> dict:
    """List open items in an org file."""
    def _do():
        fd, _st = _api.vault.open_read(_api.vault.components(path))
        try:
            text = os.read(fd, 1 << 22).decode()
        finally:
            os.close(fd)
        return {"items": [ln for ln in text.splitlines() if ln.startswith("* TODO")]}
    return _api.guard("org_list_items", _do)


def register(api):
    global _api
    _api = api
    api.add_tool(org_list_items, read_only=True)
```

`tests/extension_tests.py` is the reference for writes: `atomic_write` plus
`committer.commit(fd)`, a sync tool and an async one.
