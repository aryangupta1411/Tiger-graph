"""Embeddings for ClosedCase.note_emb, AgentCase.note_emb and PolicyChunk.emb.

Two interchangeable backends, chosen with EMBED_BACKEND (default `local`, no API key, no network at run
time after a one-off model download):

    EMBED_BACKEND=local    sentence-transformers, EMBED_MODEL=BAAI/bge-large-en-v1.5 EMBED_DIM=1024  (default)
                           lighter option:        EMBED_MODEL=BAAI/bge-small-en-v1.5 EMBED_DIM=384
    EMBED_BACKEND=voyage   Voyage API,            EMBED_MODEL=voyage-4-lite          EMBED_DIM=1024  (VOYAGE_API_KEY)

    uv run python -m rag.embed --input data/out/policy_chunk_embed.jsonl --out data/out/vec_PolicyChunk.psv
    uv run python -m rag.embed --input data/out/closed_case_embed.jsonl --out data/out/vec_ClosedCase.psv
    RUN_MODE=mock uv run python -m rag.embed ...        # deterministic hashed vectors, no model, no API key

local backend (sentence-transformers 6.1.0, verified on this machine 2026-09-22):
    SentenceTransformer(model, trust_remote_code=..., truncate_dim=EMBED_DIM, device=...)
    .encode_query(texts, ...) / .encode_document(texts, ...) apply the model's own "query"/"document"
    prompts from config_sentence_transformers.json when it ships them (voyage-4-nano does:
    "Represent the query for retrieving supporting documents: " / "Represent the document for retrieval: ").
    Models without those prompts (BAAI/bge-small-en-v1.5) get the documented BGE query prefix instead.
    normalize_embeddings=True is applied AFTER truncation (SentenceTransformer.encode truncates at
    model.py:946 and normalises at model.py:970), so truncated vectors are still unit vectors.
    trust_remote_code is on by default (EMBED_TRUST_REMOTE_CODE=0 turns it off); neither BGE model needs
    it - they are plain BERT encoders. PLAN §3.1 names voyageai/voyage-4-nano as the offline fallback, but
    its repo's modeling_qwen3_bidirectional.py targets transformers 4.51.3 while sentence-transformers
    6.1.0 pins transformers>=5,<6, and loading it raises AttributeError in AutoModel.register (the custom
    class has no `config_class`). BAAI/bge-large-en-v1.5 is the substitute: MIT, 1024-d (so the graph
    DIMENSION is unchanged), no remote code.

voyage backend, verified against voyageai 0.5.0 and https://docs.voyageai.com/docs/embeddings on 2026-09-19:
    voyageai.Client(api_key=None, max_retries=0, timeout=None, base_url=None)
    Client.embed(texts, model, input_type, truncation=True, output_dtype, output_dimension) -> EmbeddingsObject
    voyage-4-lite: 32K context, 1024-d default (256/512/2048 optional), max 1,000 texts / request,
    input_type must be "document" for corpus rows and "query" for queries (a prompt is prepended server-side).

Both backends return unit-normalised vectors, so COSINE distance in TigerGraph == 1 - dot product.
The two spaces are NOT compatible: never mix vectors from different backends/models in one graph.

Output file format (data/out/vec_<Vertex>.psv), one row per vertex, no header, no brackets:
    <id>|<f1>,<f2>,...,<f{EMBED_DIM}>
which is exactly what `LOAD f TO VECTOR ATTRIBUTE ... VALUES ($0, SPLIT($1, ",")) USING SEPARATOR="|"` expects.
A sqlite cache keyed by sha256(backend|model|dim|mode|input_type|text) makes re-runs free.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from ops.console import Col, Table, header, ok, progress, step, summary

from . import config

BATCH_TEXTS = 128          # well under voyage's 1,000-text cap
BATCH_TOKENS = 200_000     # well under voyage-4-lite's 1M tokens / request
MAX_RETRIES = 6

# BAAI/bge-small-en-v1.5 model card: queries (not documents) get this prefix; documents get none.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# EMBED_MODEL values that only make sense for the Voyage API; if one of them is left in .env while
# EMBED_BACKEND=local, fall back to the local default instead of trying to pull it from HuggingFace.
_VOYAGE_API_MODELS = ("voyage-4-lite", "voyage-4", "voyage-4.5", "voyage-3", "voyage-3-lite", "voyage-code-3", "voyage-law-2")


def _approx_tokens(t: str) -> int:
    return int(len(t.split()) * 1.3) + 1


def mock_vector(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-words hashing (unigrams + bigrams) -> unit vector. Used when RUN_MODE=mock, so
    retrieval tests run offline; NOT the same space as any real backend (never mix in one graph)."""
    v = [0.0] * dim
    toks = re.findall(r"[a-z0-9<>]+", text.lower())
    grams = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
    for g in grams:
        h = hashlib.blake2b(g.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        v[idx] += sign
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# ------------------------------------------------------------------------------------ backends

class VoyageBackend:
    """Voyage API (paid). Kept so the paid path stays available behind EMBED_BACKEND=voyage."""

    name = "voyage"
    batch_texts = BATCH_TEXTS
    batch_tokens = BATCH_TOKENS

    def __init__(self, model: str, dim: int, api_key: str | None = None):
        self.model, self.dim = model, dim
        self.total_tokens = 0
        key = api_key or os.environ.get("VOYAGE_API_KEY", "")
        if not key:
            raise RuntimeError("VOYAGE_API_KEY is not set (use EMBED_BACKEND=local, the default, or RUN_MODE=mock)")
        import voyageai  # verified signature in the module docstring

        self._client = voyageai.Client(api_key=key, max_retries=0, timeout=120)

    def describe(self) -> dict:
        return {"backend": self.name, "model": self.model, "dim": self.dim}

    def encode(self, texts: list[str], input_type: str) -> list[list[float]]:
        r = self._client.embed(texts, model=self.model, input_type=input_type,
                               truncation=True, output_dimension=self.dim)
        self.total_tokens += int(getattr(r, "total_tokens", 0) or 0)
        return [list(map(float, e)) for e in r.embeddings]


class LocalBackend:
    """sentence-transformers, running on this machine. No API key, no network after the model download."""

    name = "local"
    batch_tokens = 10_000_000          # no per-request token cap; the batch size is what matters

    def __init__(self, model: str, dim: int, device: str = "", batch_size: int = 0,
                 trust_remote_code: bool | None = None):
        if "/" not in model or model in _VOYAGE_API_MODELS:
            fallback = config.LOCAL_EMBED_MODEL_DEFAULT
            print(f"[embed] EMBED_MODEL={model!r} is a Voyage API model name; EMBED_BACKEND=local uses "
                  f"{fallback!r} instead (set EMBED_MODEL to a HuggingFace id to choose another)", file=sys.stderr)
            model = fallback
        self.model, self.dim = model, dim
        self.total_tokens = 0
        self.batch_texts = batch_size or config.EMBED_BATCH
        trust = config.EMBED_TRUST_REMOTE_CODE if trust_remote_code is None else trust_remote_code
        from sentence_transformers import SentenceTransformer

        kw: dict = {"truncate_dim": dim, "trust_remote_code": trust}
        if device:
            kw["device"] = device
        self._st = SentenceTransformer(model, **kw)
        # The model ships its own query/document prompts (voyage-4-nano does) -> encode_query/encode_document
        # use them.  Otherwise fall back to the documented BGE query prefix.
        # sentence-transformers 6.x synthesises {"query": "", "document": ""} for models that ship no
        # prompts (bge-small does), so test for a NON-EMPTY prompt, not just the key.
        prompts = getattr(self._st, "prompts", None) or {}
        self.has_prompts = bool(prompts.get("query")) and bool(prompts.get("document"))
        self.query_prefix = "" if self.has_prompts else (BGE_QUERY_PREFIX if "bge" in model.lower() else "")
        self.device = str(getattr(self._st, "device", "") or device or "cpu")

    def describe(self) -> dict:
        return {"backend": self.name, "model": self.model, "dim": self.dim, "device": self.device,
                "model_prompts": self.has_prompts, "query_prefix": bool(self.query_prefix)}

    def encode(self, texts: list[str], input_type: str) -> list[list[float]]:
        kw = {"batch_size": self.batch_texts, "normalize_embeddings": True, "show_progress_bar": False,
              "convert_to_numpy": True}
        if input_type == "query":
            payload = [self.query_prefix + t for t in texts] if self.query_prefix else list(texts)
            vecs = self._st.encode_query(payload, **kw)
        else:
            vecs = self._st.encode_document(list(texts), **kw)
        return [[float(x) for x in row] for row in vecs]


class MockBackend:
    name = "mock"
    batch_texts = BATCH_TEXTS
    batch_tokens = BATCH_TOKENS

    def __init__(self, model: str, dim: int):
        self.model, self.dim, self.total_tokens = model, dim, 0

    def describe(self) -> dict:
        return {"backend": self.name, "model": self.model, "dim": self.dim}

    def encode(self, texts: list[str], input_type: str) -> list[list[float]]:
        return [mock_vector(t, self.dim) for t in texts]


def make_backend(backend: str, model: str, dim: int, mode: str, api_key: str | None = None):
    if mode == "mock":
        return MockBackend(model, dim)
    if backend == "voyage":
        return VoyageBackend(model, dim, api_key=api_key)
    if backend == "local":
        return LocalBackend(model, dim, device=config.EMBED_DEVICE)
    raise ValueError(f"unknown EMBED_BACKEND {backend!r} (expected 'local' or 'voyage')")


# ------------------------------------------------------------------------------------ embedder

class Embedder:
    def __init__(self, model: str = "", dim: int = 0, mode: str = "",
                 cache_path: Path | None = None, api_key: str | None = None, backend: str = ""):
        self.backend_name = backend or config.EMBED_BACKEND
        self.model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM
        self.mode = mode or config.RUN_MODE
        self.total_tokens = 0
        self.calls = 0
        self.batches = 0      # backend batches flushed (display only)
        self.cached = 0       # texts served from the sqlite cache in the last embed() (display only)
        self._backend = make_backend(self.backend_name, self.model, self.dim, self.mode, api_key=api_key)
        self.model = self._backend.model          # LocalBackend may substitute a HuggingFace id
        cache_path = cache_path or (config.OUT_DIR / "embed_cache.sqlite")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(cache_path))
        self._db.execute("create table if not exists emb (k text primary key, v text)")

    def describe(self) -> dict:
        return self._backend.describe()

    # -- cache ---------------------------------------------------------------------------------------
    def _key(self, text: str, input_type: str) -> str:
        return hashlib.sha256(
            f"{self.backend_name}|{self.model}|{self.dim}|{self.mode}|{input_type}|{text}".encode()).hexdigest()

    def _get(self, k: str) -> list[float] | None:
        row = self._db.execute("select v from emb where k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, k: str, v: list[float]) -> None:
        self._db.execute("insert or replace into emb values (?,?)", (k, json.dumps(v)))

    # -- backend call --------------------------------------------------------------------------------
    def _call(self, texts: list[str], input_type: str) -> list[list[float]]:
        if self.mode == "mock":
            return self._backend.encode(texts, input_type)
        delay = 2.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                vecs = self._backend.encode(texts, input_type)
                self.total_tokens = int(getattr(self._backend, "total_tokens", 0) or 0)
                self.calls += 1
                if any(len(v) != self.dim for v in vecs):
                    raise RuntimeError(f"embedding dimension != {self.dim} (got {len(vecs[0]) if vecs else 0}); "
                                       f"set EMBED_DIM to the model's dimension")
                return vecs
            except Exception as e:  # rate limit / transient: back off; voyageai raises voyageai.error.*
                name = type(e).__name__
                fatal = name in ("AuthenticationError", "InvalidRequestError", "OSError", "RepositoryNotFoundError",
                                 "ValueError", "RuntimeError", "ImportError", "ModuleNotFoundError")
                if attempt == MAX_RETRIES or fatal:
                    raise
                print(f"[embed] {name}: {e}; retry {attempt}/{MAX_RETRIES} in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay + random.random())
                delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")

    def embed(self, texts: list[str], input_type: str, on_progress: Callable[[int], None] | None = None) -> list[list[float]]:
        """Embed `texts`, using the sqlite cache for anything already seen.

        `on_progress(n)` (optional) is called with the number of texts finished since the last call -
        once for the cache hits, then once per flushed batch. It is how `main()` drives the progress
        bar; library callers (agent/tools_local.py) leave it out and nothing is printed.
        """
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        for i, t in enumerate(texts):
            cached = self._get(self._key(t, input_type))
            if cached is not None:
                out[i] = cached
            else:
                todo.append(i)
        self.cached = len(texts) - len(todo)
        if on_progress is not None and self.cached:
            on_progress(self.cached)
        batch: list[int] = []
        btoks = 0
        max_texts = getattr(self._backend, "batch_texts", BATCH_TEXTS)
        max_toks = getattr(self._backend, "batch_tokens", BATCH_TOKENS)

        def flush() -> None:
            nonlocal batch, btoks
            if not batch:
                return
            vecs = self._call([texts[i] for i in batch], input_type)
            for i, v in zip(batch, vecs):
                out[i] = v
                self._put(self._key(texts[i], input_type), v)
            self._db.commit()
            if on_progress is not None:
                on_progress(len(batch))
            self.batches += 1
            batch, btoks = [], 0

        for i in todo:
            t = _approx_tokens(texts[i])
            if batch and (len(batch) >= max_texts or btoks + t > max_toks):
                flush()
            batch.append(i)
            btoks += t
        flush()
        return [v for v in out]  # type: ignore[misc]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text], "query")[0]


def write_psv(rows: Iterable[tuple[str, list[float]]], out: Path, precision: int = 6) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w") as f:
        for vid, vec in rows:
            if "|" in vid or "\n" in vid:
                raise ValueError(f"vertex id {vid!r} contains the field separator")
            f.write(vid + "|" + ",".join(f"{x:.{precision}f}" for x in vec) + "\n")
            n += 1
    return n


def read_psv(path: Path) -> Iterable[tuple[str, list[float]]]:
    with path.open() as f:
        for line in f:
            vid, vec = line.rstrip("\n").split("|", 1)
            yield vid, [float(x) for x in vec.split(",")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, required=True, help="jsonl with id/text keys")
    ap.add_argument("--out", type=Path, required=True, help="psv output: id|f1,f2,...")
    ap.add_argument("--id-key", default="id")
    ap.add_argument("--text-key", default="text")
    ap.add_argument("--input-type", default="document", choices=["document", "query"])
    ap.add_argument("--backend", default="", choices=["", "local", "voyage"], help="default: $EMBED_BACKEND (local)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true",
                    help="print the run manifest as one JSON line instead of the summary (same content as "
                         "<out>.manifest.json, which is always written)")
    a = ap.parse_args(argv)
    rows = [json.loads(line) for line in a.input.open() if line.strip()]
    if a.limit:
        rows = rows[: a.limit]
    ids = [r[a.id_key] for r in rows]
    texts = [r[a.text_key] for r in rows]
    t0 = time.time()
    if not a.json:
        header("rag.embed", "corpus rows -> unit-normalised vectors -> <id>|<f1,f2,...> for the TigerGraph loader",
               {"backend": "mock (RUN_MODE=mock)" if config.RUN_MODE == "mock" else (a.backend or config.EMBED_BACKEND),
                "model": config.EMBED_MODEL, "dim": config.EMBED_DIM, "mode": config.RUN_MODE, "input type": a.input_type, "rows": f"{len(rows):,}",
                "input": str(a.input), "out": str(a.out),
                "cache": str(config.OUT_DIR / "embed_cache.sqlite")})
        step("loading the backend (first local run downloads the model)")
    emb = Embedder(backend=a.backend)
    if a.json:
        vecs = emb.embed(texts, a.input_type)
    else:
        ok(f"backend ready: {emb.backend_name} / {emb.model} / {emb.dim}-d")
        with progress("embedding rows", total=len(texts)) as p:
            vecs = emb.embed(texts, a.input_type, on_progress=p.advance)
    n = write_psv(zip(ids, vecs), a.out)
    manifest = {"model": emb.model, "dim": emb.dim, "mode": emb.mode, "input_type": a.input_type, "rows": n,
                "api_calls": emb.calls, "voyage_total_tokens": emb.total_tokens, "seconds": round(time.time() - t0, 1),
                "input": str(a.input), "output": str(a.out), **emb.describe()}
    a.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    if a.json:
        print(json.dumps(manifest))
        return 0
    d = emb.describe()
    t = Table(Col("setting", max_width=20), Col("value", max_width=46), title="embedding run")
    for k, v in (("backend", d.get("backend")), ("model", d.get("model")), ("dimensions", d.get("dim")),
                 ("device", d.get("device", "-")), ("model prompts", d.get("model_prompts", "-")),
                 ("query prefix", d.get("query_prefix", "-")), ("run mode", emb.mode),
                 ("input type", a.input_type)):
        t.add_row(k, "yes" if v is True else "no" if v is False else v)
    t.print()
    v = Table(Col("measure", max_width=24), Col("value", align="right", width=14), title="throughput")
    secs = manifest["seconds"]
    v.add_row("rows written", f"{n:,}")
    v.add_row("served from cache", f"{emb.cached:,}")
    v.add_row("embedded now", f"{n - emb.cached:,}")
    v.add_row("backend batches", f"{emb.batches:,}")
    if emb.total_tokens:
        v.add_row("tokens billed", f"{emb.total_tokens:,}")
    v.add_row("seconds", f"{secs:,.1f}")
    v.add_row("rows / second", f"{(n / secs):,.0f}" if secs > 0 else "-")
    v.print()
    ok(f"vectors -> {a.out} ({n:,} rows x {emb.dim} dims)")
    summary("rag.embed complete",
            {"rows": f"{n:,}", "dim": emb.dim, "backend": emb.backend_name, "model": emb.model,
             "psv": str(a.out), "manifest": str(a.out.with_suffix('.manifest.json')),
             "elapsed": f"{secs:.1f}s"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
