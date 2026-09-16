# Milestone 2 — Stream A Completion Report

**Acceptance criterion (a): 85%+ completion.**
Measured 16 Sep 2026 against the master catalog dataset loaded in the live `/query`
service on `genemco-harvester`.

---

## Scope of this measurement — read first

This report measures **completion over the full master catalog dataset loaded in the
deployed service — all 15,939 records**. That population was chosen by Gerald on
16 Sep 2026, who directed that the validation run against the loaded catalog.

**It is not a measurement against a separately supplied active-SKU subset.** No
independent active-SKU list was ever provided to this project, and none was used here.
Nothing in this report should be read as evidence that a filtered "active" set of roughly
6,084 SKUs was validated, because no such list exists on our side.

What follows from that, stated plainly:

- Every record in the loaded dataset was processed, so **any subset drawn from this
  dataset — including whatever set Genemco considers active — is also complete at 100%.**
- What this measurement **cannot** show is whether an active SKU exists that is *absent
  from the master dataset altogether*. A SKU that was never in the catalog export cannot
  appear as a failure here; it would simply be invisible. Confirming that would require
  the active-SKU list or Shopify Admin API access, neither of which we hold.

That gap is the one real residual risk, and it is unchanged by this report.

---

## The dataset measured

| | |
|---|---|
| File | `data/search_layer/catalog_golden_records.jsonl` |
| Size | 30,384,738 bytes |
| SHA-256 | `6b525f6ad425d5c734ecf2114369e362e128f007e5889d7d3008d2988f3ba15f` |
| Source | catalog harvest `farzana_rag_sandbox.json` (15,939 source records) |
| Built by | `scripts/run_catalog_search_layer.py`, run 14 Sep 2026 |

This is the same file running in production: the checksum above matches the
`catalog_golden_records.jsonl` entry in `MANIFEST.sha256` inside
`genemco-query-service-20260914-bc6facf.tar.gz`, the bundle Muiz deployed on
`genemco-harvester` on 16 Sep 2026. At startup the live service reported
`records: 15939, catalog: 15939, authoritative: 20`.

---

## Result

| Measure | Value |
|---|---|
| Total records in the loaded catalog dataset | **15,939** |
| Successfully processed through Golden Record | **15,939** |
| Failed | **0** |
| **Completion rate** | **100.0%** |
| Gate | 85% |
| **Pass / fail** | **PASS** — 15.0 percentage points above the gate |

To fail the gate, 2,391 of the 15,939 records would have had to fail. None did.

### Supporting counts

| Measure | Value |
|---|---|
| Records with at least one extracted spec | 15,937 (99.99%) |
| Records with identity only, no specs | 2 (`TEST1`, `TEST3` — placeholder listings in the catalog, not real equipment) |
| Specs produced | 72,724 |
| Search chunks produced | 88,663 |
| Duplicate SKUs in the output | 0 |
| Run time | 8.9 s |

### Logged notes — carried, not failures

| Note | Count | Meaning |
|---|---|---|
| `duplicate_serial_flagged` | 353 | The catalog lists one serial number against more than one SKU. Carried through as a source warning, never silently corrected |
| `url_fallback_id` | 38 | 19 SKUs are reused across listings; the product URL was used to keep identities distinct |
| `identity_only_no_specs` | 2 | The two test listings above |

### Manual precedence on dual-source SKUs

| Measure | Value |
|---|---|
| SKUs with both a manual-derived record and a catalog record | 20 |
| Catalog values blocked by a manual value | 30 |
| Catalog gap-fills on manual-backed SKUs | 90 |
| Conflicts logged, manual wins | 3 |
| **Manual spec sets altered** | **0** |

---

## What "successfully processed" means

A record counts as complete when the catalog adapter produced a Golden Record **and** its
search-layer chunks, with every outcome logged — the SOW definition of a successful run or
a valid logged fallback. A record with no extractable specs still completes with its
identity chunk, and is counted separately above rather than hidden in the headline number.

Completion is a measure of **pipeline coverage**, not of source accuracy. It says every
record was processed and nothing was dropped. It does not claim the catalog's own values
are correct — where listings contradict each other (XJB151 at 7,000 RPM on two listings
and 700 RPM on a third), `/query` refuses to answer rather than guess, and the fix is a
catalog correction on Genemco's side.

---

## How these numbers were verified

Counted directly from the shipped data file and the per-SKU execution log, not copied
from the earlier run summary:

| Check | Method | Result |
|---|---|---|
| Record count | Line count of `catalog_golden_records.jsonl` | 15,939 |
| Source coverage | Record count of the harvest input | 15,939 — nothing dropped before processing |
| Failures | `ok` flag across all 15,939 entries in `logs/milestone2_streamA_report_detail.json` | 0 false, 0 errors |
| Spec totals | Summed from the output records and cross-checked against the detail log | 72,724 both ways |
| Identity | SHA-256 of the file against the deployed bundle manifest | identical |

No re-ingestion was needed or run: nothing was unprocessed.

---

## Status of criterion (a)

**PASS** against the population Gerald specified — the full master catalog dataset loaded
in the service, 15,939 of 15,939 records complete, 100.0%, against an 85% gate.

**Still open, for the record:** no independent active-SKU list has been supplied, so no
measurement here speaks to active SKUs that may exist outside the master dataset. If that
list or a Shopify Admin API token arrives later, this same check can be re-run against it
in minutes.
