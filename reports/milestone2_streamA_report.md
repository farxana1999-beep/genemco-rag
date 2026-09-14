# Milestone 2 — Stream A Completion Report

_Generated 2026-09-14T13:16:12.236105+00:00 by `scripts/run_catalog_search_layer.py`_

## Result

| Measure | Value |
|---|---|
| Population processed | entire catalog harvest -- no Stream A SKU list available |
| Records processed | 15,939 |
| Completed | 15,939 |
| Failed | 0 |
| **Completion rate** | **100.0%** |
| Completed with at least one spec | 15,937 (99.99%) |
| 85% gate on processed population | **PASS** |
| Specs produced | 72,724 |
| Search chunks produced | 88,663 |
| Run time | 8.93 s |

## The 6,084 active-SKU set is not yet defined

No data held for this project identifies which SKUs are active. The catalog
harvest carries no inventory status, and the Shopify Admin API token has not been
supplied. The run was therefore executed over the **entire harvest**, a superset
that includes active and historical SKUs (15,939 records).

Worst case, if every failure fell inside the active set, any 6,084-SKU
subset would still complete at **100.0%**. That bound holds regardless of which
SKUs are active, **provided every active SKU is present in the harvest** — which
cannot be confirmed until a Stream A SKU list or Admin API access is available.

## What “complete” means here

A record is complete when the catalog adapter produced a Golden Record and its
search-layer chunks with every outcome logged — the SOW definition of a successful
run or a valid logged fallback. Records with no extractable specs still complete
with their identity chunk and are counted separately above.

## Failures by category

None.

## Logged notes (not failures)

| Note | Count |
|---|---|
| duplicate_serial_flagged | 353 |
| identity_only_no_specs | 2 |
| url_fallback_id | 38 |

## Manual precedence on dual-source SKUs

SKUs that have both a manual-derived Golden Record and a catalog record.

| Measure | Value |
|---|---|
| Dual-source SKUs | 20 |
| Catalog values blocked by manual | 30 |
| Catalog gap-fills on manual-backed SKUs | 90 |
| Conflicts logged (manual wins) | 3 |
| **Manual spec sets altered** | **0** |

## Destination

search layer only -- no embeddings, no upsert, FAQ layer untouched.
