# Genemco Catalog RAG — Architecture & Data Ingestion Pipeline

End-to-end RAG vector database architecture and data ingestion engine for Genemco.com,
built to SOW v3.1. Ingests Shopify catalog metadata, layout-parsed PDF manuals,
vision-parsed nameplate photos, curated technical literature (Stream C), and static
compressor telemetry schemas to power hybrid semantic search, diagnostic retrieval,
and grounded SEO FAQ generation.

## Architecture

```
Shopify Admin API ─┐
PDF manuals (Docling) ─┼─► Golden Record Builder ─► Chunking (+context headers)
Nameplate photos (Vision LLM) ─┘        │                    │
Telemetry schema (Genemco) ─────────────┘          Embeddings (async batched)
                                                             │
                                              Pinecone/Weaviate + BM25 index
                                                             │
                                    Hybrid Search (filter + dense + BM25 + rerank)
                                                             │
                              FastAPI: /search  /faq/{sku}  /telemetry/query
```

## Quick start

```bash
# 1. Environment
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Credentials
cp .env.example .env
# ...edit .env with the keys from Gerald (Shopify token, OpenAI, Pinecone)

# 3. Sanity check (no API keys needed)
pytest tests/ -v

# 4. Milestone 1 — 100-SKU validation run
python -m scripts.run_milestone1
#   --from-cache     reuse previously downloaded Shopify pages
#   --skip-vision    skip nameplate vision calls (cheap dry run)
# Produces logs/milestone1_report.json (deliverable package data)

# 5. Milestone 2 — full catalog ingestion
python -m scripts.run_full_ingestion --stream A    # 6,084 active SKUs, 85% gate
python -m scripts.run_full_ingestion --stream B    # 9,800 historical, best-effort
python -m scripts.run_full_ingestion --stream C    # up to 15,000 literature pairs

# 6. Query API
uvicorn api.main:app --reload --port 8000
# Docs: http://localhost:8000/docs
```

## Data file conventions

| Input | Location |
|---|---|
| PDF manual for a SKU | `data/pdf_manuals/{SKU}.pdf` |
| Nameplate photo | `data/nameplates/{SKU}.jpg` (also .jpeg/.png/.webp) |
| Stream C pairs | `data/stream_c/**/{name}.pdf` + `{name}.json` sidecar |
| Telemetry test payloads | `data/telemetry_payloads/*.json` |
| Optional stream SKU lists | `data/stream_a_skus.csv`, `data/stream_b_skus.csv` (column: `sku`) |

Genemco-provided telemetry Pydantic schema: save as `telemetry/genemco_schema.py`
exposing a class `TelemetrySchema` — it will automatically override the built-in
default (Frick Quantum HD-style fields).

## API endpoints

- `POST /search` — `{"query": "max pressure on RDB-222B", "top_k": 8}` → hybrid search
  (model-token metadata filter → dense top-50 → BM25 top-50 → RRF merge → cross-encoder rerank)
- `GET /faq/{sku}` — validated, gated FAQ set + JSON-LD `FAQPage` schema for SEO injection
  (`?regenerate=true` to rebuild)
- `POST /telemetry/query` — diagnostic queries against telemetry/alarm fields
- `GET /health`

## SOW guardrails implemented

- **Source precedence**: nameplate wins electrical/serial; PDF wins dimensional/performance;
  conflicts beyond 5% tolerance logged to `merge_meta.conflicts` + `logs/conflicts.jsonl`,
  never silently overwritten.
- **Provenance**: every spec field is `{value, unit, source, source_ref, method, confidence}`.
- **FAQ numeric gate**: every number in every generated answer must exist in the SKU's
  verified specs, or the FAQ is dropped. Generic FAQs (no model + no number) auto-rejected.
  Near-duplicates deduped by embedding similarity.
- **Graceful PDF failure**: corrupt/locked/unparseable PDFs → `pdf_enrichment: false` +
  `logs/exceptions.jsonl` entry; never crashes a batch.
- **Low-confidence nameplates** → `logs/review_queue.jsonl`.
- **Async batching + rate-limit handling** on all embedding calls (tenacity + asyncio-throttle).
- **Zero fine-tuning**: pure external search index; ingested data never trains model weights.
- **Ops logs for contract governance**: `logs/delay_log.jsonl` (Section 7 delay tracking) and
  `logs/adaptability_hours.jsonl` (Section 2 five-hour cap tracking) — append via
  `core.logging_utils.log_delay()` / `log_adaptability_hours()`.

## Reports produced

- `logs/milestone1_report.json` — 100-SKU end-to-end validation evidence
- `logs/stream_A_report.json` — completion rate vs the 85% gate
- `logs/stream_B_report.json`, `logs/stream_C_report.json` — best-effort evidence
- `logs/exceptions.jsonl` — automated exception report (SOW Section 3)

## Maintenance

- **Re-run ingestion for one SKU**: `python -c "from scripts.ingest_sku import ingest_sku; ..."`
  or re-run the stream script — chunk IDs are deterministic, so upserts overwrite cleanly.
- **Add new SKUs**: they appear automatically on the next Shopify sync + stream run.
- **Rotate API keys**: edit `.env`, restart the API. No keys are ever stored in code or data.
- **Switch vector backend**: set `VECTOR_BACKEND=weaviate` + Weaviate creds; no code change.

## Milestone 2 — verified query service

| Document | Purpose |
|---|---|
| [docs/HANDOVER.md](docs/HANDOVER.md) | Architecture, schemas, field mappings, run logs, maintenance |
| [docs/QUERY_API.md](docs/QUERY_API.md) | `POST /query` contract with captured examples |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running the service behind the Worker |
| [reports/milestone2_streamA_report.md](reports/milestone2_streamA_report.md) | Stream A completion evidence |

```bash
pip install -r requirements-query.txt
python -m api.query_app                        # GET /health, POST /query on 127.0.0.1:9000
python -m scripts.run_catalog_search_layer     # rebuild the catalog search layer
```
