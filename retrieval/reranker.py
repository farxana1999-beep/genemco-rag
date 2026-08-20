"""
Reranker pass — cross-encoder (local sentence-transformers) by default,
Cohere Rerank optional, or 'none' to skip (falls back to fusion order).
"""
from core.config import settings
from core.logging_utils import get_logger

log = get_logger("reranker")
_cross_encoder = None


def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder
        _cross_encoder = CrossEncoder(settings.CROSS_ENCODER_MODEL)
    return _cross_encoder


def rerank(query: str, candidates: list[dict], top_k: int = 8) -> list[dict]:
    if not candidates:
        return []
    backend = settings.RERANKER_BACKEND

    try:
        if backend == "cross-encoder":
            ce = _get_cross_encoder()
            pairs = [(query, c.get("text", "")) for c in candidates]
            scores = ce.predict(pairs)
            for c, s in zip(candidates, scores):
                c["rerank_score"] = float(s)
            return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_k]

        if backend == "cohere":
            import cohere
            co = cohere.Client(settings.COHERE_API_KEY)
            resp = co.rerank(
                model="rerank-english-v3.0", query=query,
                documents=[c.get("text", "") for c in candidates], top_n=top_k,
            )
            out = []
            for r in resp.results:
                c = candidates[r.index]
                c["rerank_score"] = r.relevance_score
                out.append(c)
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("Reranker failed (%s) — falling back to fusion order", e)

    return candidates[:top_k]
