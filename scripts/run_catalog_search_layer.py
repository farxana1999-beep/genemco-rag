"""
Catalog harvest -> SEARCH layer. Milestone 2 Stream A evidence run.

Destination rules (Milestone 1 close-out, client-approved):
  * catalog records feed the SEARCH layer only -- never the grounded FAQ specs{}
  * authoritative (manual / nameplate) golden records are READ, never written
  * where a SKU has both, precedence is applied (manual wins) and divergences
    beyond tolerance are logged to logs/conflicts.jsonl

Outputs
  data/search_layer/catalog_golden_records.jsonl   one golden record per line
  data/search_layer/catalog_chunks.jsonl           search chunks, doc_type=catalog
  logs/<report>_detail.json                        per-SKU outcomes
  reports/<report>.json and reports/<report>.md    shareable summary

No embeddings and no upserts. Vectors are a separate, approval-gated step.

Usage
  python -m scripts.run_catalog_search_layer
  python -m scripts.run_catalog_search_layer --sku-list data/stream_a_skus.csv
"""
import argparse
import copy
import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger
from core.schemas import GoldenRecord
from ingestion.golden_record import AUTHORITATIVE_SPEC_SOURCES, merge_catalog_specs
from ingestion.harvest_adapter import build_golden_record as build_catalog_record
from pipeline.chunking import chunks_for_catalog

log = get_logger("catalog_search_layer")

GATE_PCT = 85.0
STREAM_A_EXPECTED = 6084


def _load_sku_list(path: Path) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [r["sku"].strip() for r in csv.DictReader(f) if (r.get("sku") or "").strip()]


def _load_authoritative() -> dict[str, GoldenRecord]:
    out = {}
    for p in settings.GOLDEN_DIR.glob("*.json"):
        try:
            rec = GoldenRecord.model_validate_json(p.read_text(encoding="utf-8"))
            out[rec.sku] = rec
        except Exception as e:  # noqa: BLE001
            log.warning("unreadable authoritative record %s: %s", p.name, e)
    return out


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(settings.DATA_DIR.parent).as_posix()
    except ValueError:
        return str(path)


def _manual_fingerprint(rec: GoldenRecord) -> dict:
    return {f: (sv.value, sv.unit, sv.source_ref) for f, sv in rec.specs.items()
            if sv.source in AUTHORITATIVE_SPEC_SOURCES}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harvest", default=str(settings.DATA_DIR / "staging_sample" / "farzana_rag_sandbox.json"))
    ap.add_argument("--sku-list", default=None,
                    help="CSV with a 'sku' column defining the active (Stream A) set")
    ap.add_argument("--out-dir", default=str(settings.DATA_DIR / "search_layer"))
    ap.add_argument("--report", default="milestone2_streamA_report")
    args = ap.parse_args()

    t0 = time.time()
    harvest = json.loads(Path(args.harvest).read_text(encoding="utf-8"))
    by_id: dict[str, dict] = {}
    duplicate_ids = 0
    for r in harvest:
        rid = str(r.get("id") or r.get("sku") or "").strip()
        duplicate_ids += rid in by_id
        by_id[rid] = r

    sku_list = Path(args.sku_list) if args.sku_list else settings.DATA_DIR / "stream_a_skus.csv"
    if sku_list.is_file():
        wanted = _load_sku_list(sku_list)
        population = f"Stream A SKU list ({sku_list.name})"
        population_defined = True
    else:
        wanted = list(by_id)
        population = "entire catalog harvest -- no Stream A SKU list available"
        population_defined = False

    authoritative = _load_authoritative()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec_path = out_dir / "catalog_golden_records.jsonl"
    chunk_path = out_dir / "catalog_chunks.jsonl"
    tmp_rec, tmp_chunk = rec_path.with_suffix(".jsonl.tmp"), chunk_path.with_suffix(".jsonl.tmp")

    results, failures, notes = [], Counter(), Counter()
    dual = {"skus": 0, "blocked_by_manual": 0, "gap_filled": 0, "conflicts": 0, "manual_specs_altered": 0}
    specs_total = chunks_total = 0

    # Write to temp files and swap at the end, so a crash never leaves a half-written layer.
    with tmp_rec.open("w", encoding="utf-8") as fr, tmp_chunk.open("w", encoding="utf-8") as fc:
        for sku in wanted:
            row = {"sku": sku, "ok": False, "specs": 0, "chunks": 0,
                   "dual_source": False, "source_warnings": 0, "error": None}
            src = by_id.get(sku)
            if src is None:
                row["error"] = "not_in_harvest"
                failures["not_in_harvest"] += 1
                results.append(row)
                continue
            try:
                rec = build_catalog_record(src)
                chunks = chunks_for_catalog(rec)
                if sku in authoritative:
                    row["dual_source"] = True
                    dual["skus"] += 1
                    manual = authoritative[sku]
                    merged = copy.deepcopy(manual)      # never mutate the authoritative record
                    st = merge_catalog_specs(merged, rec.specs)
                    dual["blocked_by_manual"] += st["blocked_by_manual"]
                    dual["gap_filled"] += st["gap_filled"]
                    dual["conflicts"] += st["conflicts"]
                    if _manual_fingerprint(manual) != _manual_fingerprint(merged):
                        dual["manual_specs_altered"] += 1
                fr.write(rec.model_dump_json() + "\n")
                for c in chunks:
                    fc.write(c.model_dump_json() + "\n")
                row.update(ok=True, specs=len(rec.specs), chunks=len(chunks),
                           source_warnings=len(rec.merge_meta.source_warnings))
                specs_total += len(rec.specs)
                chunks_total += len(chunks)
                if not rec.specs:
                    notes["identity_only_no_specs"] += 1
                if str(sku).startswith("http"):
                    notes["url_fallback_id"] += 1
                if any("serial_not_unique" in w for w in rec.merge_meta.source_warnings):
                    notes["duplicate_serial_flagged"] += 1
            except Exception as e:  # noqa: BLE001 -- one bad record must not stop the run
                row["error"] = f"{type(e).__name__}: {e}"
                failures[type(e).__name__] += 1
            results.append(row)
    tmp_rec.replace(rec_path)
    tmp_chunk.replace(chunk_path)

    total = len(wanted)
    completed = sum(1 for r in results if r["ok"])
    with_specs = sum(1 for r in results if r["ok"] and r["specs"] > 0)
    failed = total - completed
    rate = round(100 * completed / max(1, total), 2)
    strict_rate = round(100 * with_specs / max(1, total), 2)
    # If the true Stream A set is a subset of this population, the worst case is that
    # every failure lands inside it. That bound holds without knowing the subset.
    s = STREAM_A_EXPECTED
    worst_subset_rate = round(100 * (s - min(failed, s)) / s, 2) if total >= s else None

    report = {
        "run": "milestone2_stream_a_catalog_search_layer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "population": population,
        "population_defined": population_defined,
        "stream_a_expected_skus": STREAM_A_EXPECTED,
        "harvest_file": Path(args.harvest).name,
        "harvest_records": len(harvest),
        "duplicate_ids_in_harvest": duplicate_ids,
        "processed": total,
        "completed": completed,
        "failed": failed,
        "completion_rate_pct": rate,
        "completed_with_specs": with_specs,
        "completed_with_specs_rate_pct": strict_rate,
        "gate_pct": GATE_PCT,
        "gate_passed_on_processed_population": rate >= GATE_PCT,
        "worst_case_rate_for_any_6084_subset_pct": worst_subset_rate,
        "failures_by_category": dict(failures),
        "notes": dict(notes),
        "dual_source_precedence": dual,
        "specs_total": specs_total,
        "chunks_total": chunks_total,
        "destination": "search layer only -- no embeddings, no upsert, FAQ layer untouched",
        "outputs": {"golden_records": _rel(rec_path), "chunks": _rel(chunk_path)},
        "elapsed_seconds": round(time.time() - t0, 2),
    }

    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (settings.LOG_DIR / f"{args.report}_detail.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    reports_dir = settings.DATA_DIR.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{args.report}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (reports_dir / f"{args.report}.md").write_text(_markdown(report), encoding="utf-8")

    print(json.dumps({k: report[k] for k in (
        "population", "processed", "completed", "failed", "completion_rate_pct",
        "completed_with_specs_rate_pct", "gate_passed_on_processed_population",
        "worst_case_rate_for_any_6084_subset_pct", "failures_by_category", "notes",
        "dual_source_precedence", "chunks_total", "elapsed_seconds")}, indent=2))


def _markdown(r: dict) -> str:
    verdict = "PASS" if r["gate_passed_on_processed_population"] else "FAIL"
    lines = [
        "# Milestone 2 — Stream A Completion Report",
        "",
        f"_Generated {r['timestamp']} by `scripts/run_catalog_search_layer.py`_",
        "",
        "## Result",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Population processed | {r['population']} |",
        f"| Records processed | {r['processed']:,} |",
        f"| Completed | {r['completed']:,} |",
        f"| Failed | {r['failed']:,} |",
        f"| **Completion rate** | **{r['completion_rate_pct']}%** |",
        f"| Completed with at least one spec | {r['completed_with_specs']:,} ({r['completed_with_specs_rate_pct']}%) |",
        f"| 85% gate on processed population | **{verdict}** |",
        f"| Specs produced | {r['specs_total']:,} |",
        f"| Search chunks produced | {r['chunks_total']:,} |",
        f"| Run time | {r['elapsed_seconds']} s |",
        "",
    ]
    if not r["population_defined"]:
        bound = r["worst_case_rate_for_any_6084_subset_pct"]
        lines += [
            "## The 6,084 active-SKU set is not yet defined",
            "",
            "No data held for this project identifies which SKUs are active. The catalog",
            "harvest carries no inventory status, and the Shopify Admin API token has not been",
            "supplied. The run was therefore executed over the **entire harvest**, a superset",
            f"that includes active and historical SKUs ({r['harvest_records']:,} records).",
            "",
            f"Worst case, if every failure fell inside the active set, any {r['stream_a_expected_skus']:,}-SKU",
            f"subset would still complete at **{bound}%**. That bound holds regardless of which",
            "SKUs are active, **provided every active SKU is present in the harvest** — which",
            "cannot be confirmed until a Stream A SKU list or Admin API access is available.",
            "",
        ]
    lines += [
        "## What “complete” means here",
        "",
        "A record is complete when the catalog adapter produced a Golden Record and its",
        "search-layer chunks with every outcome logged — the SOW definition of a successful",
        "run or a valid logged fallback. Records with no extractable specs still complete",
        "with their identity chunk and are counted separately above.",
        "",
        "## Failures by category",
        "",
    ]
    if r["failures_by_category"]:
        lines += ["| Category | Count |", "|---|---|"]
        lines += [f"| {k} | {v:,} |" for k, v in sorted(r["failures_by_category"].items())]
    else:
        lines.append("None.")
    d = r["dual_source_precedence"]
    lines += [
        "",
        "## Logged notes (not failures)",
        "",
        "| Note | Count |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(r["notes"].items())] or ["| none | 0 |"]
    lines += [
        "",
        "## Manual precedence on dual-source SKUs",
        "",
        "SKUs that have both a manual-derived Golden Record and a catalog record.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Dual-source SKUs | {d['skus']} |",
        f"| Catalog values blocked by manual | {d['blocked_by_manual']} |",
        f"| Catalog gap-fills on manual-backed SKUs | {d['gap_filled']} |",
        f"| Conflicts logged (manual wins) | {d['conflicts']} |",
        f"| **Manual spec sets altered** | **{d['manual_specs_altered']}** |",
        "",
        "## Destination",
        "",
        f"{r['destination']}.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
