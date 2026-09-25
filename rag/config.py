"""Paths and environment for the rag/ package. All paths resolve relative to the repo root
(the directory that contains `rag/`), so the scripts run from anywhere.

.env keys used here (see contracts/interfaces.md section D):
  EMBED_BACKEND=local|voyage (default local: sentence-transformers on this machine, NO API key),
  EMBED_MODEL, EMBED_DIM, EMBED_DEVICE, EMBED_BATCH, EMBED_TRUST_REMOTE_CODE, VOYAGE_API_KEY
  (only for EMBED_BACKEND=voyage), RUN_MODE=live|mock,
  TG_HOST, TG_GRAPHNAME=FraudGraph, TG_SECRET, TG_QUERY_TIMEOUT_MS=120000
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(os.environ.get("HHGOA_REPO_ROOT", Path(__file__).resolve().parent.parent))


def _load_dotenv() -> None:
    """Load `.env` into os.environ (never overriding a real value), same policy as agent/config.py.
    Without this, a fresh `python -m rag.<module>` process never sees TG_HOST/TG_SECRET etc. from
    `.env` -- only vars already in the shell -- which is silent everywhere except rag.load_vectors,
    the one script here that must reach the live graph."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (REPO_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


_load_dotenv()
DATA_DIR = Path(os.environ.get("HHGOA_DATA_DIR", REPO_ROOT / "data"))
CORPUS_DIR = DATA_DIR / "corpus"
CORPUS_RAW = CORPUS_DIR / "raw"
CORPUS_TEXT = CORPUS_DIR / "text"
OUT_DIR = DATA_DIR / "out"
OFAC_DIR = DATA_DIR / "ofac"
DUCKDB_PATH = Path(os.environ.get("HHGOA_DUCKDB", DATA_DIR / "hhgoa.duckdb"))
README_PATH = Path(os.environ.get("HHGOA_README", DATA_DIR / "raw" / "README.md"))

# --- embeddings -------------------------------------------------------------------------------------
# EMBED_BACKEND picks the implementation; `local` is the default and needs no API key and no network at
# run time (one-off model download only).  The paid Voyage path stays available with EMBED_BACKEND=voyage.
#   local  -> BAAI/bge-large-en-v1.5   1024-d  MIT, 1.34 GB  (default; same dimension as the paid path)
#             BAAI/bge-small-en-v1.5    384-d  MIT, 133 MB   (lighter: set EMBED_MODEL and EMBED_DIM=384)
#   voyage -> voyage-4-lite            1024-d  (paid, needs VOYAGE_API_KEY)
# NOT usable here: voyageai/voyage-4-nano (PLAN §3.1). Its repo ships modeling_qwen3_bidirectional.py written
# for transformers 4.51.3; sentence-transformers 6.1.0 pins transformers>=5,<6 and the custom class has no
# `config_class`, so AutoModel.register() raises AttributeError. Re-check when voyageai updates the repo.
# EMBED_DIM must match the DIMENSION in graph/schema_change_vectors.gsql; rag.load_vectors renders that
# file's DIMENSION from EMBED_DIM, so changing it here is enough (rebuild the vector attributes after).
LOCAL_EMBED_MODEL_DEFAULT = "BAAI/bge-large-en-v1.5"
VOYAGE_EMBED_MODEL_DEFAULT = "voyage-4-lite"
EMBED_DEFAULTS = {"local": (LOCAL_EMBED_MODEL_DEFAULT, 1024), "voyage": (VOYAGE_EMBED_MODEL_DEFAULT, 1024)}


def resolve_embed(env: Mapping[str, str] | None = None) -> tuple[str, str, int]:
    """(backend, model, dim) from EMBED_BACKEND / EMBED_MODEL / EMBED_DIM, each falling back to the
    default for the selected backend. Pure, so it is unit-testable without reloading this module."""
    env = os.environ if env is None else env
    backend = (env.get("EMBED_BACKEND") or "local").strip().lower()
    model_default, dim_default = EMBED_DEFAULTS.get(backend, EMBED_DEFAULTS["local"])
    return backend, (env.get("EMBED_MODEL") or model_default), int(env.get("EMBED_DIM") or dim_default)


EMBED_BACKEND, EMBED_MODEL, EMBED_DIM = resolve_embed()
EMBED_DEVICE = os.environ.get("EMBED_DEVICE", "")            # "" = sentence-transformers auto (mps/cuda/cpu)
EMBED_BATCH = int(os.environ.get("EMBED_BATCH") or "32")     # texts per forward pass, local backend only
EMBED_TRUST_REMOTE_CODE = (os.environ.get("EMBED_TRUST_REMOTE_CODE") or "1").strip().lower() not in ("0", "false", "no")

RUN_MODE = os.environ.get("RUN_MODE", "live")  # live | mock

TG_HOST = os.environ.get("TG_HOST", "")
TG_GRAPHNAME = os.environ.get("TG_GRAPHNAME", "FraudGraph")
TG_SECRET = os.environ.get("TG_SECRET", "")
TG_QUERY_TIMEOUT_MS = int(os.environ.get("TG_QUERY_TIMEOUT_MS", "120000"))
TG_LOAD_TIMEOUT_MS = int(os.environ.get("TG_LOAD_TIMEOUT_MS", "600000"))

# Vector attributes that exist in FraudGraph (contracts/schema.md)
VECTOR_ATTRS = {
    "ClosedCase": "note_emb",
    "AgentCase": "note_emb",
    "PolicyChunk": "emb",
}

# The seven FraudPattern vertex ids (README `pattern` enum)
PATTERNS = [
    "card_testing",
    "card_not_present_fraud",
    "card_not_present_new_device",
    "out_of_region_use",
    "account_takeover",
    "undocumented",
    "none",
]


def ensure_dirs() -> None:
    for p in (CORPUS_RAW, CORPUS_TEXT, OUT_DIR, OFAC_DIR):
        p.mkdir(parents=True, exist_ok=True)
