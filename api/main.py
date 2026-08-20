"""
Phase 11 — Query API (FastAPI).
Endpoints per SOW Milestone 2:
  GET  /health            — liveness
  POST /search            — hybrid search + rerank (sales-engineer chat tool)
  GET  /faq/{sku}         — validated JSON-LD FAQPage schema for SEO injection
  POST /telemetry/query   — diagnostic queries against alarm/telemetry fields

Run:  uvicorn api.main:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.logging_utils import get_logger
from core.schemas import TelemetryQuery

log = get_logger("api")

app = FastAPI(
    title="Genemco Catalog RAG API",
    version="1.0.0",
    description="Hybrid semantic search, grounded SEO FAQs, and telemetry diagnostics for Genemco.com",
)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2)
    top_k: int = Field(8, ge=1, le=25)
    sku: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search")
def search(req: SearchRequest):
    from retrieval.hybrid_search import hybrid_search
    try:
        results = hybrid_search(req.query, top_k=req.top_k, sku=req.sku)
    except Exception as e:  # noqa: BLE001
        log.exception("search failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        "query": req.query,
        "results": [
            {
                "text": r.get("text"),
                "parent_context": r.get("parent_text"),
                "sku": r.get("sku"),
                "model": r.get("model"),
                "source_url": r.get("source_url"),
                "page": r.get("page"),
                "section": r.get("section"),
                "score": r.get("rerank_score", r.get("rrf", r.get("score"))),
            }
            for r in results
        ],
    }


@app.get("/faq/{sku}")
def faq(sku: str, regenerate: bool = False):
    """Returns JSON-LD FAQPage. Generates (and gates) on first call, caches to disk."""
    import json
    from core.config import settings
    from faq.generator import faq_batch_to_jsonld, generate_faqs
    from ingestion.golden_record import load_golden_record
    from core.schemas import FAQBatch

    cache = settings.DATA_DIR / "faq_cache" / f"{sku}.json"
    if cache.exists() and not regenerate:
        return json.loads(cache.read_text(encoding="utf-8"))

    record = load_golden_record(sku)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No golden record for SKU '{sku}'")
    batch = generate_faqs(record)
    jsonld = faq_batch_to_jsonld(batch)
    payload = {"sku": sku, "faq_count": len(batch.items), "jsonld": jsonld,
               "items": [i.model_dump() for i in batch.items]}
    # Only persist a non-empty result. An empty batch can mean the LLM call failed
    # (bad/missing key, rate limit, outage) — caching that would pin the SKU to an
    # empty FAQPage forever, since later calls would be served from disk.
    if batch.items:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        log.warning("SKU %s produced 0 FAQs — not caching (will retry on next call)", sku)
    return payload


@app.post("/telemetry/query")
def telemetry_query(q: TelemetryQuery):
    from telemetry.schema_hook import query_telemetry
    results = query_telemetry(q)
    return {"count": len(results), "results": results}
