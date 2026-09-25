"""Backend selection and the free local embedding backend (rag/embed.py).

The local-model tests are SKIPPED unless the weights are already in the HuggingFace cache, so CI and a
fresh clone stay offline. Warm the cache once with:
    uv run python -c "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-large-en-v1.5')"
"""
from __future__ import annotations

import os

import pytest

from rag.config import resolve_embed
from rag.embed import BGE_QUERY_PREFIX, Embedder, LocalBackend, MockBackend, make_backend
from rag.load_vectors import render_dimension

# ----------------------------------------------------------------- config / selection

def test_local_is_the_default_backend():
    assert resolve_embed({}) == ("local", "BAAI/bge-large-en-v1.5", 1024)


def test_voyage_backend_still_selectable_and_keeps_its_defaults():
    assert resolve_embed({"EMBED_BACKEND": "voyage"}) == ("voyage", "voyage-4-lite", 1024)
    assert resolve_embed({"EMBED_BACKEND": " VOYAGE "}) == ("voyage", "voyage-4-lite", 1024)


def test_bge_small_dimension_flows_through_to_the_schema_change_job():
    assert resolve_embed({"EMBED_MODEL": "BAAI/bge-small-en-v1.5", "EMBED_DIM": "384"}) == (
        "local", "BAAI/bge-small-en-v1.5", 384)
    ddl = 'ALTER VERTEX ClosedCase ADD VECTOR ATTRIBUTE note_emb(DIMENSION=1024, METRIC="COSINE");'
    assert render_dimension(ddl, 384) == ddl.replace("DIMENSION=1024", "DIMENSION=384")


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="EMBED_BACKEND"):
        make_backend("openai", "m", 8, "live")


def test_mock_mode_needs_no_backend_and_no_key():
    b = make_backend("voyage", "voyage-4-lite", 16, "mock")
    assert isinstance(b, MockBackend)
    v = b.encode(["card testing burst"], "document")[0]
    assert len(v) == 16 and abs(sum(x * x for x in v) - 1.0) < 1e-6


def test_embedder_in_mock_mode_round_trips(tmp_path):
    e = Embedder(model="m", dim=32, mode="mock", cache_path=tmp_path / "c.sqlite", backend="local")
    a = e.embed_query("card testing burst on a new device")
    assert len(a) == 32
    assert e.embed_query("card testing burst on a new device") == a      # sqlite cache hit
    assert e.embed_documents(["x", "y"]) != [a, a]


# ----------------------------------------------------------------- the real local model

MODEL = os.environ.get("EMBED_TEST_MODEL", "BAAI/bge-large-en-v1.5")
DIM = int(os.environ.get("EMBED_TEST_DIM", "1024"))


def _cached() -> bool:
    """True when the weights are already downloaded (no network needed)."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(MODEL, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 - not cached, hub missing, anything
        return False


local_model = pytest.mark.skipif(not _cached(), reason=f"{MODEL} is not in the HuggingFace cache")


@local_model
def test_local_backend_produces_unit_vectors_of_the_configured_dimension():
    b = LocalBackend(MODEL, DIM)
    vecs = b.encode(["confirmed travel to the billing region", "card testing burst"], "document")
    assert len(vecs) == 2 and all(len(v) == DIM for v in vecs)
    for v in vecs:
        assert abs(sum(x * x for x in v) ** 0.5 - 1.0) < 1e-4


@local_model
def test_bge_query_prefix_is_applied_to_queries_only():
    b = LocalBackend(MODEL, DIM)
    assert b.has_prompts is False and b.query_prefix == BGE_QUERY_PREFIX
    text = "prior cases on this device"
    assert b.encode([text], "query")[0] != b.encode([text], "document")[0]


@local_model
def test_semantically_close_texts_are_closer_than_unrelated_ones(tmp_path):
    e = Embedder(model=MODEL, dim=DIM, mode="live", backend="local", cache_path=tmp_path / "c.sqlite")
    ct1, ct2, cleared = (
        "pattern: card_testing | outcome: confirmed_fraud | device: none | region: none | "
        "Case <CASE>: a burst of <N> small online authorizations on card <ID> within minutes.",
        "pattern: card_testing | outcome: confirmed_fraud | device: none | region: none | "
        "Case <CASE>: many tiny probing charges on card <ID> in a short window before a large purchase.",
        "pattern: none | outcome: cleared | device: none | region: none | "
        "Case <CASE>: cardholder confirmed travel to the billing region; activity legitimate. No action.",
    )
    a, b, c = e.embed_documents([ct1, ct2, cleared])
    same = sum(x * y for x, y in zip(a, b))          # unit vectors -> dot product == cosine
    other = sum(x * y for x, y in zip(a, c))
    assert same > other, f"card-testing pair {same:.4f} should beat card-testing/cleared {other:.4f}"
