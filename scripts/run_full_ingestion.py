"""
Phase 10 — Milestone 2 full catalog ingestion across all three streams.

Stream A: Active SKUs (~6,084)   -> 85%+ completion gate (pdf_enrichment true
                                    OR a valid logged fallback counts as complete)
Stream B: Historical (~9,800)    -> best-effort, zero penalty
Stream C: Literature (<=15,000 pairs) -> pathlib glob over PDF+JSON sidecar pairs,
                                    best-effort, zero penalty

Usage:
    python -m scripts.run_full_ingestion --stream A
    python -m scripts.run_full_ingestion --stream B
    python -m scripts.run_full_ingestion --stream C
    python -m scripts.run_full_ingestion --stream all

Stream A/B SKU lists: provide CSV files data/stream_a_skus.csv / data/stream_b_skus.csv
(single column 'sku'); if absent, Stream A defaults to all in-stock synced SKUs and
Stream B to out-of-stock ones.
Produces logs/streamX_report.json for each stream — your 85% evidence.
"""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger, log_exception
from ingestion.shopify_sync import load_raw_products, normalize_all, sync_all_products
from pipeline.chunking import chunks_from_literature
from pipeline.embeddings import embed_texts_sync
from pipeline.vector_store import get_vector_store
from retrieval.hybrid_search import get_bm25
from scripts.ingest_sku import ingest_sku

log = get_logger("full_ingestion")


def _load_sku_list(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return [row["sku"].strip() for row in csv.DictReader(f) if row.get("sku", "").strip()]


def _report(name: str, payload: dict) -> None:
    out = settings.LOG_DIR / f"{name}_report.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Report -> %s", out)


def run_sku_stream(stream: str, normalized: dict, sku_list: list[str] | None,
                   completion_gate: float | None) -> dict:
    store = get_vector_store()
    store.init_index()
    bm25 = get_bm25()

    if sku_list is None:
        wanted = {
            "A": [s for s, b in normalized.items() if b.inventory_status == "in_stock"],
            "B": [s for s, b in normalized.items() if b.inventory_status != "in_stock"],
        }[stream]
    else:
        wanted = sku_list

    results = []
    for i, sku in enumerate(wanted, 1):
        block = normalized.get(sku)
        if block is None:
            log_exception(sku=sku, file=None, reason="sku_not_in_shopify_sync")
            results.append({"sku": sku, "ok": False, "error": "not_in_shopify"})
            continue
        results.append(ingest_sku(sku, block, store=store, bm25=bm25))
        if i % 25 == 0:
            log.info("Stream %s progress: %s/%s", stream, i, len(wanted))

    # 'Complete' = successful run with enrichment OR a valid logged fallback
    complete = sum(1 for r in results if r["ok"])
    rate = round(100 * complete / max(1, len(results)), 2)
    report = {
        "stream": stream,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "complete": complete,
        "completion_rate_pct": rate,
        "gate_pct": completion_gate,
        "gate_passed": (rate >= completion_gate) if completion_gate else None,
        "results": results,
    }
    _report(f"stream_{stream}", report)
    print(f"Stream {stream}: {complete}/{len(results)} ({rate}%)"
          + (f" — gate {'PASSED' if report['gate_passed'] else 'NOT MET'}" if completion_gate else ""))
    return report


def run_stream_c() -> dict:
    """Dynamic directory-traversal loop over PDF + JSON sidecar pairs (SOW Section 3)."""
    store = get_vector_store()
    store.init_index()
    bm25 = get_bm25()

    pairs = []
    for pdf in sorted(settings.STREAM_C_DIR.glob("**/*.pdf")):
        sidecar = pdf.with_suffix(".json")
        if sidecar.exists():
            pairs.append((pdf, sidecar))
        else:
            log_exception(sku=None, file=str(pdf), reason="stream_c_missing_sidecar")
    pairs = pairs[: settings.STREAM_C_MAX_PAIRS]
    log.info("Stream C: %s valid PDF+JSON pairs found (cap %s)", len(pairs), settings.STREAM_C_MAX_PAIRS)

    processed, failed = 0, 0
    for i, (pdf, sidecar_path) in enumerate(pairs, 1):
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sections = _extract_text_sections(pdf)
            chunks = chunks_from_literature(sections, sidecar)
            if chunks:
                vectors = embed_texts_sync([c.text for c in chunks])
                store.upsert(chunks, vectors)
                bm25.add_documents([
                    {"id": c.chunk_id, "text": c.text, "sku": c.sku, "model": c.model,
                     "source_url": c.source_url, "section": c.section,
                     "page": c.page, "parent_text": c.parent_text}
                    for c in chunks
                ])
            processed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log_exception(sku=None, file=str(pdf), reason=f"stream_c_pair_failure: {e}")
        if i % 50 == 0:
            log.info("Stream C progress: %s/%s", i, len(pairs))

    report = {
        "stream": "C",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pairs_found": len(pairs),
        "processed": processed,
        "failed": failed,
    }
    _report("stream_C", report)
    print(f"Stream C: processed {processed}/{len(pairs)} pairs (best-effort, zero penalty)")
    return report


def _extract_text_sections(pdf: Path, max_chars: int = 2500) -> list[tuple[str, int]]:
    """Lightweight text extraction for literature (tables not needed here)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf))
        sections = []
        for page_no, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            for i in range(0, len(text), max_chars):
                sections.append((text[i:i + max_chars], page_no))
        return sections
    except Exception as e:  # noqa: BLE001
        log_exception(sku=None, file=str(pdf), reason=f"stream_c_text_extract_failure: {e}")
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--from-cache", action="store_true")
    args = ap.parse_args()

    normalized = {}
    if args.stream in ("A", "B", "all"):
        raw = load_raw_products() if args.from_cache else sync_all_products()
        normalized = normalize_all(raw)

    if args.stream in ("A", "all"):
        run_sku_stream("A", normalized, _load_sku_list(settings.DATA_DIR / "stream_a_skus.csv"),
                       completion_gate=85.0)
    if args.stream in ("B", "all"):
        run_sku_stream("B", normalized, _load_sku_list(settings.DATA_DIR / "stream_b_skus.csv"),
                       completion_gate=None)
    if args.stream in ("C", "all"):
        run_stream_c()


if __name__ == "__main__":
    main()
