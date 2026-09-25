"""GraphRAG module (Module F): corpus, chunks, embeddings, vector loads, retrieval, SAR, OFAC.

Public surface (consumed by agent/tools_local.py, agent/sar.py, memory/write_case.py):

    rag.embed.Embedder                      -> embed_documents / embed_query (EMBED_BACKEND=local by default:
                                               BAAI/bge-large-en-v1.5 1024-d via sentence-transformers, no API key;
                                               EMBED_BACKEND=voyage keeps the paid voyage-4-lite path)
    rag.retrieval.similar_prior_cases(...)  -> contract rows for `similar_prior_cases`
    rag.retrieval.grounding_chunks(...)     -> contract rows for `grounding_chunks`
    rag.retrieval.case_query_text(...)      -> the masked query string for case memory retrieval
    rag.closedcase_embed_text.agent_case_embed_text(answer) -> text for AgentCase.note_emb
    rag.sar.SarFacts / build_prompt / fallback_narrative / assemble / negative_sar
    rag.validate_sar.validate_sar(sar, ...) -> list[str] violations
    rag.ofac.ofac_screen(name)              -> {"query", "exact", "best_score", "matches", "list_version", "ref"}
    rag.load_vectors.load_psv / wait_index_ready / upsert_case_vector
"""
