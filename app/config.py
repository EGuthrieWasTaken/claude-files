"""Configuration, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_set(name: str, default: set[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None:
        return frozenset(n.lower() for n in default)
    return frozenset(n.strip().lower() for n in raw.split(",") if n.strip())


@dataclass(frozen=True)
class Config:
    root: str = "/data"
    token: str = ""
    readonly_token: str = ""
    max_upload_bytes: int = 256 * 1024 * 1024
    max_list_entries: int = 10_000
    denied_names: frozenset[str] = field(default_factory=frozenset)
    allow_hardlinked_files: bool = False
    allow_delete: bool = True
    enable_docs: bool = True
    rate_limit_per_minute: int = 600
    require_mount: bool = True
    enable_mcp: bool = True
    mcp_public_hostname: str = ""
    mcp_inline_limit_chars: int = 100_000
    # 8 KiB, not 1 MiB: this is a single-shot-call ceiling, bounded by what a
    # model can actually emit in one tool call (observed ~5 KB of decoded
    # bytes for a truncating surface -- docs/plan-mcp-large-writes.md, "The
    # advertised budgets are wrong by roughly 200x"), not a context-cost
    # budget. Above it, write_file refuses and names begin_write.
    mcp_base64_limit_bytes: int = 8 * 1024
    mcp_pdf_max_bytes: int = 32 * 1024 * 1024
    mcp_upload_session_ttl_seconds: int = 900
    mcp_max_concurrent_uploads: int = 8
    mcp_upload_chunk_advisory_bytes: int = 3072
    upload_ticket_ttl_seconds: int = 900
    upload_tickets_max: int = 100
    download_ticket_ttl_seconds: int = 900
    download_tickets_max: int = 100
    public_base_url: str = ""
    # -- git tools (docs/plan-mcp-git-ops.md) --------------------------------
    # Two independent switches. A disabled class is not registered at all.
    enable_git_read: bool = False
    enable_git_write: bool = False
    # Where the repository root is mounted. Empty means "same as root": the
    # shared folder IS the repository root and the scope is the whole repo.
    git_repo_root: str = ""
    git_remote: str = "origin"
    git_branch: str = ""
    git_author_name: str = "claude-files agent"
    git_author_email: str = "agent@localhost"
    git_commit_trailer: str = ""
    git_max_changed_files: int = 1000
    git_network_timeout_seconds: int = 60
    git_local_timeout_seconds: int = 30
    git_pull_outside_scope: bool = False
    ssh_key_path: str = ""
    ssh_known_hosts_path: str = ""
    git_token: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("API_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "API_TOKEN is required and must not be empty. "
                "Generate one with: openssl rand -hex 32"
            )
        if len(token) < 16:
            raise RuntimeError("API_TOKEN must be at least 16 characters")

        readonly = os.environ.get("API_TOKEN_READONLY", "").strip()
        if readonly and readonly == token:
            raise RuntimeError("API_TOKEN_READONLY must differ from API_TOKEN")
        if readonly and len(readonly) < 16:
            raise RuntimeError("API_TOKEN_READONLY must be at least 16 characters")

        return cls(
            root=os.environ.get("DATA_ROOT", "/data"),
            token=token,
            readonly_token=readonly,
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 256 * 1024 * 1024),
            max_list_entries=_env_int("MAX_LIST_ENTRIES", 10_000),
            denied_names=_env_set("DENIED_NAMES", {".git", ".gitmodules", ".ssh"}),
            allow_hardlinked_files=_env_bool("ALLOW_HARDLINKED_FILES", False),
            allow_delete=_env_bool("ALLOW_DELETE", True),
            enable_docs=_env_bool("ENABLE_DOCS", True),
            rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 600),
            require_mount=_env_bool("REQUIRE_MOUNT", True),
            enable_mcp=_env_bool("ENABLE_MCP", True),
            mcp_public_hostname=os.environ.get("MCP_PUBLIC_HOSTNAME", "").strip(),
            mcp_inline_limit_chars=_env_int("MCP_INLINE_LIMIT_CHARS", 100_000),
            mcp_base64_limit_bytes=_env_int("MCP_BASE64_LIMIT_BYTES", 8 * 1024),
            mcp_pdf_max_bytes=_env_int("MCP_PDF_MAX_BYTES", 32 * 1024 * 1024),
            mcp_upload_session_ttl_seconds=_env_int("MCP_UPLOAD_SESSION_TTL_SECONDS", 900),
            mcp_max_concurrent_uploads=_env_int("MCP_MAX_CONCURRENT_UPLOADS", 8),
            mcp_upload_chunk_advisory_bytes=_env_int("MCP_UPLOAD_CHUNK_ADVISORY_BYTES", 3072),
            upload_ticket_ttl_seconds=_env_int("UPLOAD_TICKET_TTL_SECONDS", 900),
            upload_tickets_max=_env_int("UPLOAD_TICKETS_MAX", 100),
            download_ticket_ttl_seconds=_env_int("DOWNLOAD_TICKET_TTL_SECONDS", 900),
            download_tickets_max=_env_int("DOWNLOAD_TICKETS_MAX", 100),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip(),
            enable_git_read=_env_bool("ENABLE_GIT_READ", False),
            enable_git_write=_env_bool("ENABLE_GIT_WRITE", False),
            git_repo_root=os.environ.get("GIT_REPO_ROOT", "").strip(),
            git_remote=os.environ.get("GIT_REMOTE", "origin").strip() or "origin",
            git_branch=os.environ.get("GIT_BRANCH", "").strip(),
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "").strip()
            or "claude-files agent",
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "").strip()
            or "agent@localhost",
            git_commit_trailer=os.environ.get("GIT_COMMIT_TRAILER", "").strip(),
            git_max_changed_files=_env_int("GIT_MAX_CHANGED_FILES", 1000),
            git_network_timeout_seconds=_env_int("GIT_NETWORK_TIMEOUT_SECONDS", 60),
            git_local_timeout_seconds=_env_int("GIT_LOCAL_TIMEOUT_SECONDS", 30),
            git_pull_outside_scope=_env_bool("GIT_PULL_OUTSIDE_SCOPE", False),
            ssh_key_path=os.environ.get("SSH_KEY_PATH", "").strip(),
            ssh_known_hosts_path=os.environ.get("SSH_KNOWN_HOSTS_PATH", "").strip(),
            git_token=os.environ.get("GIT_TOKEN", "").strip(),
        )
