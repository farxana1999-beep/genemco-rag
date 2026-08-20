"""
Phase 9 — Milestone 1 validation run: 100-SKU end-to-end test.
Usage:
    python -m scripts.run_milestone1                # first 100 SKUs from live sync
    python -m scripts.run_milestone1 --from-cache   # reuse saved raw Shopify pages
    python -m scripts.run_milestone1 --sample 100
Produces logs/milestone1_report.json — your Milestone 1 deliverable package data.
"""
import argparse
import json
from datetime import datetime, timezone

from core.config import settings
from core.logging_utils import get_logger
from ingestion.shopify_sync import load_raw_products, normalize_all, sync_catalog
from pipeline.vector_store import get_vector_store
from retrieval.hybrid_search import get_bm25
from scripts.ingest_sku import ingest_sku
from telemetry.schema_hook import load_test_payloads

log = get_logger("milestone1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--skip-vision", action="store_true", help="skip vision LLM calls (dry run)")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap Shopify pages fetched on a live sync (250 products/page)")
    ap.add_argument("--public", action="store_true",
                    help="STOPGAP: source the catalog from the public storefront feed")
    args = ap.parse_args()

    raw = (load_raw_products() if args.from_cache
           else sync_catalog(max_pages=args.max_pages, public=True if args.public else None))
    normalized = normalize_all(raw)
    sample = list(normalized.items())[: args.sample]
    log.info("Milestone 1 run: %s SKUs", len(sample))

    sync_modes: dict[str, int] = {}
    for _, b in sample:
        sync_modes[b.sync_mode] = sync_modes.get(b.sync_mode, 0) + 1
    if sync_modes.get("public_storefront"):
        log.warning("%s/%s SKUs sourced from the PUBLIC storefront — demo data, "
                    "re-sync with the Admin token before treating as authoritative",
                    sync_modes["public_storefront"], len(sample))

    if not sample:
        hint = (": data/shopify_raw/ is empty, run without --from-cache first"
                if args.from_cache else ": the Shopify sync returned no products")
        log.error("Nothing to ingest%s", hint)
        raise SystemExit(1)

    store = get_vector_store()
    store.init_index()
    bm25 = get_bm25()

    # Telemetry schema integration check (deliverable 6)
    telemetry_payloads = load_test_payloads()

    results = []
    for i, (sku, block) in enumerate(sample, 1):
        status = ingest_sku(sku, block, store=store, bm25=bm25, skip_vision=args.skip_vision)
        results.append(status)
        log.info("[%s/%s] %s ok=%s pdf=%s chunks=%s",
                 i, len(sample), sku, status["ok"], status["pdf_enrichment"], status["chunks"])

    ok = sum(1 for r in results if r["ok"])
    enriched = sum(1 for r in results if r["pdf_enrichment"])
    report = {
        "run": "milestone_1_validation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(sample),
        "sync_modes": sync_modes,
        "end_to_end_success": ok,
        "success_rate_pct": round(100 * ok / max(1, len(sample)), 1),
        "pdf_enrichment_count": enriched,
        "telemetry_payloads_validated": len(telemetry_payloads),
        "results": results,
    }
    out = settings.LOG_DIR / "milestone1_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nMilestone 1 report -> {out}")
    print(f"Success: {ok}/{len(sample)} ({report['success_rate_pct']}%)")


if __name__ == "__main__":
    main()
