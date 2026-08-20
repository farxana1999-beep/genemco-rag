# Genemco Sprint 1 — Final Handover

**Delivered:** 2026-08-12 (v4 — purified + schema-hardened + atomic specs)
**Scope:** Phase 1 "The Great Harvest" — bulk technical asset collection for the DUO Engine (Sprint 2).

> **v2 Update (domain-level culling):** A final QA pass purged 32 off-domain
> files (NASA/IRS/USGS/government papers, Vishay/semiconductor datasheets,
> academic journals) that passed the Sonar bridge's PDF gate but carried zero
> industrial-refrigeration value. All were re-audited (1:1 pairing PASS) and
> the manifest + SHA-256 were regenerated to match the shipped files exactly.
>
> **v3 Update (schema hardening for Farzana's guardrails):** The evaluation set
> `farzana_rag_sandbox.json` was rebuilt to unblock full ingestion:
> (1) `verified_specs` array carries ground-truth field/value pairs so the
> numeric gate has data to validate against; (2) primary identity is now the
> immutable Shopify SKU (`id`), with the source-page URL used only for the 19
> SKUs Genemco's own catalog reuses across distinct machines — `clean_model_code`
> is now strictly a coarse/fuzzy filter; (3) every spec value carries
> `source_doc` + `source_url` + `source_page` provenance (61,361 entries, 0
> missing) for 1-to-1 FAQ-layer traceability. A compressed `.zip` copy is also
> provided for lighter downloads.
>
> **v4 Update (atomic specs + serial integrity flags):** Per Farzana's second
> validation pass, bundled spec strings (e.g. `Model: XJF151N, Serial No: ...,
> Refrigerant: R-717`) are now flattened into atomic key-value pairs in a new
> `atomic_specs` object (66,334 entries; original bundled `specs` preserved
> untouched), so her pipeline gets one value per field with no adapter. Genuinely
> ambiguous list-like values (`Inlets: (2) 4 in`, dimensions, power specs) are
> intentionally NOT split to avoid shredding. Records whose catalog serial number
> duplicates across distinct units are flagged via `source_data_warnings:
> ["serial_not_unique: ..."]` (353 records) rather than silently altered — a
> source-side integrity note, since these are shared serials in Genemco's own
> catalog export, not a defect introduced by the harvest.

---

## Directory Map

```
/Genemco-Sprint-1-Final-Handover/
├── CORPUS_V1/
│   ├── gencorpus_v1.tar.00          PDF + JSON sidecar archive, part 1/5 (2.0 GB)
│   ├── gencorpus_v1.tar.01          part 2/5 (2.0 GB)
│   ├── gencorpus_v1.tar.02          part 3/5 (2.0 GB)
│   ├── gencorpus_v1.tar.03          part 4/5 (2.0 GB)
│   ├── gencorpus_v1.tar.04          part 5/5 (0.8 GB)
│   ├── corpus_manifest.csv          Master map: 2,583 docs, traceability + confidence
│   └── corpus.sha256                5,166 per-file SHA-256 hashes (2,583 PDFs + 2,583 sidecars)
└── EVALUATION_SETS/
    ├── farzana_rag_sandbox.json     15,939 records (verified_specs + atomic_specs + provenance + integrity flags)
    ├── farzana_rag_sandbox.json.zip  compressed copy (3.6 MB) for lighter downloads
    └── company_raw_semrush_dump.json  5,232 ranked SEO queries (25 categories) — market-demand proof
```

## Reconstructing the Corpus

```bash
# 1. Join the split archive
cat gencorpus_v1.tar.00 gencorpus_v1.tar.01 gencorpus_v1.tar.02 \
    gencorpus_v1.tar.03 gencorpus_v1.tar.04 > genemco_corpus_v1.tar

# 2. Extract
tar -xf genemco_corpus_v1.tar
# -> ./scraped_data/rag_corpus/  (docs/ = PDFs, metadata/ = JSON sidecars)

# 3. Verify integrity (ideally on the extracted tree, against original filenames)
cd scraped_data/rag_corpus && sha256sum -c ../../corpus.sha256
```

Checksum note: `corpus.sha256` was generated over the **original loose files**
(`scraped_data/rag_corpus/...`), so verification is performed on the extracted
tree, not on the .tar container.

## Key Numbers

| Metric | Value |
|---|---|
| Tracked URLs (Sonar bridge) | 5,107 |
| Harvested (kept) | 2,612 |
| Culled (low-value / flyers) | 1,366 |
| Invalid content | 133 |
| Duplicates | 65 |
| Failed | 69 |
| Purged (off-domain, v2 QA) | 32 |
| PDFs + sidecars on disk | 2,583 + 2,583 |
| Corpus size | ~8.8 GB |
| Golden Record audit | PASS (1:1 pairing, schema-clean) |
| Confidence: High / Medium / Low | 915 / 784 / 884 |
| Traceable (query -> PDF) | 2,583 / 2,583 |

## What goes where (Sprint 2 inputs)

- **DUO Knowledge Base** = `CORPUS_V1` (manuals + sidecars). Vectorize the PDFs.
- **DUO Evaluation set (Farzana's question bank)** = `EVALUATION_SETS/farzana_rag_sandbox.json`.
  15,939 catalog records, each with immutable `id` (Shopify SKU; URL fallback for
   reused SKUs), `raw_model_code`, `clean_model_code` (fuzzy filter only),
   `specs` (original), `atomic_specs` (bundled strings flattened to one value per
   field), `verified_specs` (field/value + `source_doc`/`source_url`/`source_page`
   provenance), top-level `spec_field`/`value` (numeric-gate target), question,
   answer, token_count (cl100k_base), `source_data_warnings` (flag for duplicate
   serials), and `requires_numeric_gate`. Use to test retrieval accuracy of
   manual vs. question.
- **Market-demand proof** = `company_raw_semrush_dump.json`. Answers *"why these
  2,612 manuals?"* with ranked global search volume.

## Confidence tiers (corpus_manifest.csv)

- **High** = model number verified in PDF text + page_count > 10 + technical
  keyword density >= 1%. These are the flagship specs/IOMs — train/query first.
- **Medium** = model verified but short doc (page_count <= 10) or thin
  technical text. Valid, useful; lower body.
- **Low** = model family unverifiable from the PDF (family_verified missing or
  false), or sidecar without a paired PDF at scan time. Re-check manually
  before relying on it.

## Source of truth

- `scanledger.json` holds the full chain of custody (per-URL state, parent
  query, priority tier) — retained on the build VM.