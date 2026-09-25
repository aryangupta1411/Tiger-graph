"""Agent configuration (contracts §D `.env` keys) and repo paths.

Everything comes from the environment (a `.env` at the repo root is loaded once,
never overriding real variables) so the same code runs live, in RUN_MODE=mock,
and in CI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(os.environ.get("HHGOA_REPO_ROOT", Path(__file__).resolve().parents[1]))


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (REPO_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    """`.env` value, or `default`.

    An empty key with a trailing comment (`SAR_MODEL=   # empty = MODEL`) is read back by
    python-dotenv as the comment itself; such a value is treated as unset so the default applies.
    """
    v = os.environ.get(name, default)
    if isinstance(v, str) and v.lstrip().startswith("#"):
        return default
    return v


# Embedding defaults follow EMBED_BACKEND (mirrors rag/config.py, which is the source of truth for rag/).
_EMBED_DEFAULTS = {"local": ("BAAI/bge-large-en-v1.5", 1024), "voyage": ("voyage-4-lite", 1024)}


def _embed_default_model() -> str:
    return _EMBED_DEFAULTS.get((_env("EMBED_BACKEND", "local") or "local").strip().lower(), _EMBED_DEFAULTS["local"])[0]


def _embed_default_dim() -> int:
    return _EMBED_DEFAULTS.get((_env("EMBED_BACKEND", "local") or "local").strip().lower(), _EMBED_DEFAULTS["local"])[1]


@dataclass(frozen=True)
class Settings:
    # TigerGraph / MCP
    tg_host: str = field(default_factory=lambda: _env("TG_HOST", "http://127.0.0.1"))
    tg_graphname: str = field(default_factory=lambda: _env("TG_GRAPHNAME", "FraudGraph"))
    tg_secret: str = field(default_factory=lambda: _env("TG_SECRET", ""))
    tg_query_timeout_ms: int = field(default_factory=lambda: int(_env("TG_QUERY_TIMEOUT_MS", "120000")))
    tg_response_limit_bytes: int = field(default_factory=lambda: int(_env("TG_RESPONSE_LIMIT_BYTES", "64000000")))
    # LLM
    # cli (claude-agent-sdk over the bundled Claude Code CLI, subscription auth, no API key —
    # the default) | api (anthropic SDK, needs ANTHROPIC_API_KEY) | mock (FakeAnthropic).
    # Unset means "cli", except under RUN_MODE=mock where it means "mock" (agent/llm.resolve_backend).
    llm_backend: str = field(default_factory=lambda: _env("LLM_BACKEND", ""))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: _env("MODEL", "claude-sonnet-5"))
    sar_model: str = field(default_factory=lambda: _env("SAR_MODEL", _env("MODEL", "claude-sonnet-5")))
    effort: str = field(default_factory=lambda: _env("EFFORT", "high"))       # low|medium|high|xhigh|max
    max_tokens: int = field(default_factory=lambda: int(_env("MAX_TOKENS", "16000")))
    cache_ttl: str = field(default_factory=lambda: _env("CACHE_TTL", "1h"))   # "5m" | "1h"
    # Embeddings (EMBED_BACKEND=local is the default: sentence-transformers on this machine, no API key)
    embed_backend: str = field(default_factory=lambda: (_env("EMBED_BACKEND", "local") or "local").strip().lower())
    voyage_api_key: str = field(default_factory=lambda: _env("VOYAGE_API_KEY", ""))   # EMBED_BACKEND=voyage only
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "") or _embed_default_model())
    embed_dim: int = field(default_factory=lambda: int(_env("EMBED_DIM", "") or _embed_default_dim()))
    # Run
    run_mode: str = field(default_factory=lambda: _env("RUN_MODE", "live"))   # live | mock
    max_tool_calls: int = field(default_factory=lambda: int(_env("MAX_TOOL_CALLS", "12")))
    tool_result_max_chars: int = field(default_factory=lambda: int(_env("TOOL_RESULT_MAX_CHARS", "24000")))
    keep_alive_s: int = field(default_factory=lambda: int(_env("KEEP_ALIVE_S", "180")))
    # Paths (contracts §D)
    runs_dir: Path = field(default_factory=lambda: Path(_env("RUNS_DIR", str(REPO_ROOT / "runs"))))
    cases_dir: Path = field(default_factory=lambda: Path(_env("CASES_DIR", str(REPO_ROOT / "cases"))))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(REPO_ROOT / "data"))))
    duckdb_path: Path = field(default_factory=lambda: Path(_env("DUCKDB_PATH", str(REPO_ROOT / "data" / "hhgoa.duckdb"))))
    case_pack_csv: Path = field(default_factory=lambda: Path(_env("CASE_PACK_CSV", str(REPO_ROOT / "data" / "raw" / "case_pack.csv"))))
    ofac_dir: Path = field(default_factory=lambda: Path(_env("OFAC_DIR", str(REPO_ROOT / "data" / "ofac"))))
    prompts_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "prompts")
    query_descriptions_yaml: Path = field(
        default_factory=lambda: Path(_env("QUERY_DESCRIPTIONS_YAML", str(REPO_ROOT / "mcp" / "query_descriptions.yaml")))
    )
    launcher_path: Path = field(default_factory=lambda: REPO_ROOT / "mcp" / "tg_mcp_launcher.py")
    tools_allowlist: Path = field(default_factory=lambda: REPO_ROOT / "mcp" / "tools_allowlist.txt")

    @property
    def mock(self) -> bool:
        return self.run_mode.lower() == "mock"


SETTINGS = Settings()

# The 14 policy actions, verbatim (README Fraud Policy §1).
ACTIONS: tuple[str, ...] = (
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS",
    "GENERATE_REPORT", "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
)
AUTO_ACTIONS = frozenset({
    "ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
})
PATTERNS: tuple[str, ...] = (
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
)
REQUEST_TYPES: tuple[str, ...] = ("customer_validation", "step_up_auth", "analyst_info")
