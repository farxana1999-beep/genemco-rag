# Genemco Catalog RAG — Live Demo Script

**Internal runbook** for driving the Milestone 1 demo. For the client-facing
document, send `Genemco-Milestone-1-Report.html` instead.

Blocks labelled **"Actual"** are real captured output. Blocks labelled
**"Expected shape"** or **"illustrative"** are fixtures used during development
before real data existed — they show the response format, not Genemco data.
The `RDB-222B` examples are synthetic; that golden record has been deleted.

---

## 0. Before the call — setup (5 min)

```bash
cd genemco-rag                      # the project root (contains core/, api/, tests/)
python -m venv venv
venv\Scripts\activate               # Windows;  source venv/bin/activate on macOS/Linux
pip install -r requirements.txt     # ~5 min, pulls Docling + torch
```

> **PowerShell users:** `curl` is an alias for `Invoke-WebRequest` and will **not**
> accept the flags below. Use `curl.exe` explicitly, or run the demo from Git Bash.
> All commands below are written for Git Bash / macOS / Linux.

Credentials — copy `.env.example` to `.env` and fill in the three keys:

```bash
cp .env.example .env
# SHOPIFY_ADMIN_TOKEN, OPENAI_API_KEY, PINECONE_API_KEY
```

**`.env` must live in the same folder as `core/` and `api/`.** `core/config.py`
loads it from the project root only.

---

## 1. Prove the guardrails — no credentials, no API spend (30 sec)

The contract's correctness rules are enforced in code and covered by tests.
This is the strongest opening: it runs offline and finishes instantly.

```bash
pytest tests/ -v
```

**Actual — 31 tests across 6 files:**

```
tests/test_faq_validator.py .......... 6 passed   numeric gate, anti-generic gate
tests/test_golden_record.py .......... 5 passed   precedence, 5% tolerance, provenance
tests/test_hybrid_tokens.py .......... 1 passed   model-token extraction
tests/test_manual_map.py ............ 10 passed   multi-model manual filtering
tests/test_pdf_linearize.py .......... 3 passed   table linearization, both layouts
tests/test_safety_split.py ........... 6 passed   safety text verbatim + FAQ quarantine

============================= 31 passed in 0.74s ==============================
```

**Talking point:** each test maps to a named SOW clause — nameplate precedence on
electrical fields, PDF precedence on performance fields, 5% conflict tolerance,
full provenance on every field, the FAQ numeric gate, and the rule that safety
text can never reach the FAQ generator.

---

## 2. Layout-aware PDF parsing (Milestone 1 deliverable)

Drop a real manual at `data/pdf_manuals/{SKU}.pdf`, then:

```bash
python -m ingestion.pdf_parser data/pdf_manuals/RDB-222B.pdf
```

**Expected shape** — one self-contained record per model/spec pair, with the model
re-attached to every row and the unit recovered from the column header:

```
MODEL          SPEC                         VALUE  UNIT   PAGE
RDB-222B       max_working_pressure         300.0  PSI    1
RWB-II-177     max_working_pressure         250.0  PSI    1
RDB-222B       displacement                1152.0  CFM    1
RDB-222B       motor_speed                 3550.0  RPM    1
RDB-222B       net_weight                  8600.0  lbs    1
```

**Talking point:** a multi-column performance table covering several machines is
linearized so no chunk ever mixes two models — the core retrieval-accuracy
requirement.

### Multi-model service manuals — validated on the real Frick manual

Run against **Form 070.410-IOM (NOV 2014), 68 pages**. Docling recovered **34
tables**; the four that matter came out clean:

| Table | Page | Shape | Keyed by | Result |
|---|---|---|---|---|
| Geometrical Swept Volume | 4 | 10×8 | `Compressor Model` (`XJF 151A/M/L/N`…) | ✅ extracted |
| Allowable Flange Loads | 5 | 12×7 | nozzle size NPS | ⚠️ not model-keyed |
| CH Coupling Data | 6 | 5×17 | coupling size | ⚠️ not model-keyed |
| Oil Charge | 7 | 4×3 | `RXF MODEL` (`85, 101`…) | ✅ extracted |
| Bolt torques | 31 | 3×6 | both tiers | ✅ extracted |
| Pressure Transducer Conversion | 34 | 43×9 | sensor voltage | ⏭️ skipped by design |

Oil Charge, exactly as the manual prints it:

```
RXF MODEL   BASIC CHARGE (gallon)   ADDITIONAL FOR OIL COOLER (gallon)
12 - 19            10                        1
24 - 50            11                        1
58, 68             25                        3½
85, 101            36                        3½
```

**116 raw spec records** came out of the manual for all machines it covers;
**14** survived filtering for the RXF-85/XJF-151L that WHB03 actually is.

A service manual is not a one-SKU spec sheet. The Frick RXF manual
covers package models 12–101 **and** the XJF compressor models
95/120/151 fitted inside them. Two optional files handle this:

```jsonc
// data/manual_map.json — one manual, many SKUs
{ "070.410-IOM.pdf": ["WHB03", "WHB02"] }

// data/model_map.json — which machine each SKU actually is
{ "sku_models": { "WHB03": "RXF-85" },
  "rxf_to_xjf": { "RXF-85": "XJF-151" } }
```

`WHB03` resolves to `['RXF-85', 'XJF-151L']` and accepts rows for **both** tiers —
package-level oil charge *and* compressor-level swept volume — while rejecting
RXF-12, RXF-101, XJF-95 and XJF-120 rows in the same document.

The real manual labels rows six different ways, all of which are handled:

| Label in manual | Where | Matches RXF-85 / XJF-151L? |
|---|---|---|
| `85, 101` | oil charge, p7 | ✅ comma list |
| `12 - 19`, `24 - 50`, `58, 68` | oil charge, p7 | ❌ correctly excluded |
| `XJF 151L` | swept volume, p4 | ✅ |
| `XJF 151A` / `151M` / `151N` | swept volume, p4 | ❌ **different swept volumes** |
| `XJS/XJF 95M` | swept volume, p4 | ❌ correctly excluded |
| `58, 68, 85, 101` | bolt torque, p31 | ✅ |

The variant distinction matters: XJF 151A/M/L/N are 342/403/500/598 CFM. Matching
on `XJF-151` alone would silently merge all four, so a family-level alias is
dropped whenever a specific variant is known from the title.

`data/pdf_manuals/{SKU}.pdf` still works as the default — the manifest is additive.

### Safety text is quarantined from the FAQ generator

Spec tables and safety prose take different paths:

| Content | Route | Why |
|---|---|---|
| Spec tables | → `specs{}` → FAQ engine | numeric, groundable, gate-checkable |
| DANGER / WARNING / CAUTION blocks, procedures | → literature chunks → `/search` only | returned **verbatim** with page cite |

Safety chunks carry the manual's exact wording with **no header prepended**, so
`/search` quotes what the manual actually says. They hold no `specs{}` entries,
which makes them *structurally* unreachable from the FAQ generator — it reads
`specs{}` and nothing else. An LLM paraphrasing a DANGER block is a safety
problem, not a quality problem, so this is enforced by construction rather than
by prompt instruction.

### Graceful failure (SOW Section 3)

```bash
python -m ingestion.pdf_parser data/pdf_manuals/some_corrupt_file.pdf
```

Corrupt, truncated, empty, and password-locked PDFs all return `[]` with
`pdf_enrichment=False` and append one line to `logs/exceptions.jsonl`. **No
exception escapes, so one bad file never kills a batch run.** Verified against
corrupt / truncated / binary-garbage / zero-byte / missing files.

---

## 3. Start the API

```bash
uvicorn api.main:app --port 8000
```

Interactive docs: <http://localhost:8000/docs>

---

## 4. `GET /health`

```bash
curl -s http://127.0.0.1:8000/health
```

**Actual response:**

```json
{"status":"ok"}
```

---

## 5. `POST /telemetry/query` — diagnostic retrieval

> ⚠️ **The response below is no longer reproducible.** It was captured when a
> synthetic `RDB-222B` golden record existed; that record has since been deleted.
> Running this today returns `{"count":0,"results":[]}` because no *real* SKU has
> telemetry attached yet — telemetry arrives with Genemco's schema and payloads.
> The endpoint itself works and validates both sample payloads.

Works against `data/telemetry_payloads/*.json`. No LLM cost.

```bash
curl -s -X POST http://127.0.0.1:8000/telemetry/query \
  -H "Content-Type: application/json" \
  -d '{"alarm_code":"AL-007"}'
```

**Actual response:**

```json
{
  "count": 1,
  "results": [
    {
      "sku": "RDB-222B",
      "telemetry": {
        "alarm_code": "AL-007",
        "alarm_description": "Low suction pressure cutout",
        "suction_pressure_psi": 4.1,
        "discharge_pressure_psi": 152.3,
        "oil_pressure_psi": 48.0,
        "motor_amps": 198.5,
        "unit_model": "RDB-222B"
      }
    }
  ]
}
```

Also filterable by `sku`, `model`, or free-text `question`:

```bash
curl -s -X POST http://127.0.0.1:8000/telemetry/query \
  -H "Content-Type: application/json" -d '{"sku":"RDB-222B"}'

curl -s -X POST http://127.0.0.1:8000/telemetry/query \
  -H "Content-Type: application/json" -d '{"alarm_code":"AL-999"}'
# -> {"count":0,"results":[]}
```

**Talking point:** when Genemco supplies its own Pydantic schema, drop it in as
`telemetry/genemco_schema.py` exposing `TelemetrySchema` — it overrides the
built-in default automatically, no code change.

---

## 6. `POST /search` — hybrid search

**Real, captured against 20 ingested Genemco SKUs.**

### 6a. Exact model-number query

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"Frick RXF-85-H","top_k":2}'
```

**Actual response** (abridged to the fields that matter):

```json
{
  "query": "Frick RXF-85-H",
  "results": [
    {
      "text": "This is a specification for Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V) (SKU: WHB03). Product: … Manufacturer: Frick. Type: RXF-85H. Tags: Refrigeration, Screw Compressor Package, Screw Compressors. Availability: in_stock.",
      "sku": "WHB03",
      "source_url": "https://genemco.com/products/frick-rxf-85-h-rotary-screw-compressor-package-frick-xjf151l-175-hp-460-v-whb03",
      "section": "identity",
      "score": 9.1947
    },
    {
      "sku": "WHB02",
      "text": "… Frick RXF-85H Rotary Screw Compressor Package (Frick XJF151L, 206 HP 460 V) (SKU: WHB02) …",
      "source_url": "https://genemco.com/products/frick-rxf-85h-rotary-screw-compressor-package-frick-xjf151l-206-hp-460-v-whb02",
      "score": 8.2170
    }
  ]
}
```

**Talking point (re-verified 9 Aug against the 590-chunk index):** the exact unit
scores **9.1947**, and *every one* of the top 12 results is WHB03 — its identity
chunk then its individual specs. The near-twin `RXF-85H` (WHB02) does not appear
at all. `RXF-85-H` and `RXF-85H` differ by one hyphen and are held distinct.

> Earlier in development, when the index held only identity chunks, WHB02 ranked
> second at 8.22 with the next product at −9.52. Adding the manual's spec chunks
> changed ranks 2-12; only the 9.1947 top score is unchanged.

### 6b. Natural-language spec query

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"500 HP 460 volt ammonia screw compressor package","top_k":2}'
```

**Actual result:** `SIRA02` (7.078) and `SIRA03` (7.072) — both
*Vilter VSS1201 Rotary Screw Compressor Package (500 Hp, 460 V)*. No model number
appeared in the query at all; this is pure semantic matching on the spec language.

### 6c. Safety warning returned VERBATIM with page citation

The strongest demo for a service-parts business. Ask in plain language:

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"dangers disassembling the demand pump","top_k":2,"sku":"WHB03"}'
```

**Actual top result — `section: safety:DANGER`, `page: 30`, score 0.4510:**

> DANGER BEFORE OPENING ANY VIKING PUMP LIQUID CHAM- BER (PUMPING CHAMBER,
> RESERVOIR, JACKET, ETC.) ENSURE: 1. THAT ANY PRESSURE IN THE CHAMBER HAS BEEN
> COMPLETELY VENTED THROUGH SUCTION OR DIS- CHARGE LINES OR OTHER APPROPRIATE
> OPENINGS OR CONNECTIONS. 2. THAT THE DRIVING MEANS (MOTOR, TURBINE, ENGINE,
> ETC.) HAS BEEN 'LOCKED OUT' OR MADE NON-OPERATIONAL SO THAT IT CANNOT BE
> STARTED WHILE WORK IS BEING DONE ON THE PUMP. FAILURE TO FOLLOW ABOVE LISTED
> PRECAUTIONARY MEASURES MAY RESULT IN SERIOUS INJURY OR DEATH.

The second result is the same warning as printed on **page 27**, in the DEMAND
PUMP DISASSEMBLY section.

**Talking points:**
- The text is the manual's own words, not a summary. No LLM touched it.
- `page: 30` lets a technician verify it against the paper manual.
- This warning is retrievable but **structurally cannot become an FAQ** — it
  holds no `specs{}` entry, and the FAQ generator reads only `specs{}`.
- 96 safety panels were extracted from this manual — every DANGER, WARNING,
  CAUTION and NOTICE in it. They are stored against each SKU the manual covers,
  so the index holds 192 rows for 96 distinct panels; report the panel count
  as **96**, not 192.

### 6d. Two more, verified

| Query | Top hit | Score |
|---|---|---|
| `8 inch ammonia shut-off globe valve` | `GENF454` — Ammonia Shut-Off Globe Valve (8 in) | 7.61 |
| `end suction centrifugal pump 1200 GPM` | `IRS01A` — Scot 106 End Suction Centrifugal Motor Pump (1200 GPM Max) | 8.88 |

In the pump query the correct unit scores **8.8760** while the next candidate sits
at **−8.7896** — a decisive separation. (Re-verified 9 Aug; the second-place
candidate is now a safety chunk rather than another product, so the old −10.32
figure no longer applies.)

**Talking point:** the flow is model-token metadata filter → dense top-50 → BM25
top-50 → reciprocal-rank fusion → cross-encoder rerank. The BM25 leg is what keeps
an exact string like `VSS1201` from being blurred by embedding similarity.

---

## 7. `GET /faq/{sku}` — grounded, gated SEO FAQs

### Real output for WHB03, every number traced to a page

```bash
curl -s http://127.0.0.1:8000/faq/WHB03
```

**5 FAQs survived all gates.** Actual items:

| Question | Answer | Spec field | Page |
|---|---|---|---|
| What is the basic charge capacity of the Frick RXF-85-H…? | The basic charge capacity is **36.0 gallons**. | `basic_charge` | **7** |
| What is the maximum speed of the Frick RXF-85-H…? | The maximum speed is **4306.0 RPM**. | `max_speed_rpm` | 4 |
| What is the rotor diameter of the Frick RXF-85-H…? | The rotor diameter is **151.0 mm**. | `rotor_diameter_mm` | 4 |
| Torque, discharge flange to separator flange? | **80.0 ft-lb** | `compressor_..._torque` | 31 |
| Motor flange bolt size? | **5/8 or 3/4 inches** | `motor_flange_..._bolt_size` | 31 |

Every figure is a value that exists in the SKU's `specs{}`, each carrying the
manual page it came from. Nothing was paraphrased or inferred.

### The gate rejecting real LLM output

One FAQ the model produced was **dropped**:

```
faq_gate_reject: unverified numbers in answer: [3550.0]
Q: What is the CFM rating at 3550 RPM for the Frick RXF-85-H?
```

The manual's column header is *"Cfm 3550 Rpm"*, so `3550` exists only in the
**field name** — never as a verified value. The gate refused it. That is the rule
working exactly as written: *every number in an answer must exist in that SKU's
verified specs.* Worth a decision (see report) on whether header-embedded
qualifiers should be admitted.

```bash
curl -s http://127.0.0.1:8000/faq/NOPE-123
# {"detail":"No golden record for SKU 'NOPE-123'"}
```

A SKU with no manual returns `faq_count: 0` before making any LLM call — no specs
in, no FAQs out, and no cost.

### Shape of the response

```json
{
  "sku": "RDB-222B",
  "faq_count": 3,
  "jsonld": {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": [
      {
        "@type": "Question",
        "name": "What is the maximum working pressure of the Frick RDB-222B?",
        "acceptedAnswer": {
          "@type": "Answer",
          "text": "The Frick RDB-222B has a maximum working pressure of 300 PSI."
        }
      }
    ]
  },
  "items": [
    {
      "question": "What is the maximum working pressure of the Frick RDB-222B?",
      "answer": "The Frick RDB-222B has a maximum working pressure of 300 PSI.",
      "spec_field": "max_working_pressure",
      "value": 300.0,
      "unit": "PSI",
      "source_page": 1
    }
  ]
}
```

`?regenerate=true` forces a rebuild. The `jsonld` block drops straight into a
Shopify product template as a `<script type="application/ld+json">` tag.

### The money demo — show the gate rejecting bad output

Every number in a generated answer must already exist in that SKU's verified
specs, or the FAQ is discarded. Verified live:

| Candidate FAQ | Verdict | Reason |
|---|---|---|
| "…maximum working pressure of 300 PSI." | **KEPT** | ok |
| "…rated to 350 PSI." | **DROPPED** | unverified numbers in answer: [350.0] |
| "How does a screw compressor work?" | **DROPPED** | no model-number reference — generic output rejected |
| "…shipping weight…" (`spec_field: shipping_weight`) | **DROPPED** | spec_field 'shipping_weight' not in golden record |
| "…250 HP unit." declared as `value: 275` | **DROPPED** | declared value 275.0 != recorded 250.0 |

Every rejection is written to `logs/exceptions.jsonl` with its reason, so the
gate is auditable rather than a black box.

---

## 8. Golden Record — provenance and conflict handling

### A real ingested record, enriched from the Frick manual

```bash
cat data/golden_records/WHB03.json
```

14 spec fields, every one carrying full provenance back to a page:

```
FIELD                                       VALUE      UNIT    SOURCE      METHOD         CONF  REF
basic_charge                                 36.0      gallon  pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=7
additional_for_oil_cooler                     3.5      gallon  pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=7
cfm_3550_rpm                                500.0      -       pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=4
max_speed_rpm                              4306.0      -       pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=4
rotor_diameter_mm                           151.0      -       pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=4
rotor_l_d                                     1.6      -       pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=4
geometrical_swept_volume_..._ft_rev       0.14075      -       pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=4
motor_flange_to_compressor_tunnel_torque    145.0      ft-lb   pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=31
motor_flange_to_compressor_tunnel_bolt_size  5/8 or 3/4 in.    pdf_manual  docling_table  0.9   070.410-IOM.pdf#page=31
...
merge_meta: pdf_enrichment=true  sources_used=['shopify','pdf_manual']  conflicts=0
```

**303 → 286 chunks** per SKU: 15 identity/spec + 271 literature, of which **96 are
safety panels**. `conflicts: 0` — with only one source of technical data there is
nothing to disagree with; conflicts appear once nameplate photos arrive.

### The earlier, pre-manual state (for contrast)

```json
{
  "sku": "WHB03",
  "shopify": {
    "title": "Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V)",
    "vendor": "Frick",
    "product_type": "RXF-85H",
    "url": "https://genemco.com/products/frick-rxf-85-h-rotary-screw-compressor-package-frick-xjf151l-175-hp-460-v-whb03",
    "tags": ["Refrigeration", "Screw Compressor Package", "Screw Compressors"],
    "inventory_status": "in_stock",
    "sync_mode": "public_storefront"
  },
  "specs": {},
  "merge_meta": {
    "conflicts": [], "sources_used": ["shopify"],
    "pdf_enrichment": false, "nameplate_enrichment": false
  }
}
```

`specs: {}` and `pdf_enrichment: false` are accurate reporting — no manual was
supplied for this SKU, and the pipeline says so rather than guessing.

### With a manual and nameplate supplied

> The block below is **illustrative**, captured from an offline run using a
> fabricated nameplate, to show what the merge produces once real inputs arrive.

Every spec field carries full provenance:

```json
"max_working_pressure": {
  "value": 300.0,
  "unit": "PSI",
  "source": "pdf_manual",
  "source_ref": "data/pdf_manuals/RDB-222B.pdf#page=1",
  "method": "docling_table",
  "confidence": 0.9
}
```

Precedence and conflicts, verified end-to-end:

```
PRECEDENCE
  voltage               575.0  V     nameplate   vision_llm     0.95   <- electrical: nameplate wins
  serial_number       SN-9981        nameplate   vision_llm     0.88
  max_working_pressure  300.0  PSI   pdf_manual  docling_table  0.9    <- performance: PDF wins
  displacement         1152.0  CFM   pdf_manual  docling_table  0.9

CONFLICTS: 1 logged (never silently overwritten)
  - voltage: 460.0 (pdf_manual) vs 575.0 (nameplate)
    reason: pdf vs nameplate disagree beyond 5.0% tolerance
```

**Talking point:** the nameplate value wins *and* the disagreement is preserved in
`merge_meta.conflicts` plus `logs/conflicts.jsonl`. Nothing is thrown away silently.

Nameplate extractions scoring below 0.7 confidence are routed to
`logs/review_queue.jsonl` instead of being ingested unreviewed.

---

## 9. Milestone 1 validation run

```bash
python -m scripts.run_milestone1 --sample 20 --skip-vision --public --max-pages 1
```

Flags: `--from-cache` reuses saved pages (no API call), `--skip-vision` skips paid
vision calls, `--max-pages N` caps a live sync, `--sample N` sets the SKU count,
`--public` uses the storefront stopgap (see below).

**Actual `logs/milestone1_report.json`:**

```json
{
  "run": "milestone_1_validation",
  "timestamp": "2026-08-09T18:43:48.913081+00:00",
  "sample_size": 20,
  "sync_modes": {"public_storefront": 20},
  "end_to_end_success": 20,
  "success_rate_pct": 100.0,
  "pdf_enrichment_count": 2,
  "telemetry_payloads_validated": 2
}
```

**100% end-to-end success, 0 conflicts, 4 exceptions — all intentional skips**
(the manual's table of contents and its pressure-transducer grid, once per
compressor SKU). `pdf_enrichment_count: 2` — WHB03 and WHB02, the two SKUs the
Frick manual covers. The run produced **590 chunks** in total, including **96
distinct safety panels** (stored once per covered SKU, so 192 rows in the index).

### Public storefront stopgap

While the Shopify **Admin** API token is pending, `--public` (or
`PUBLIC_CATALOG_MODE=true`) sources the catalog from the unauthenticated
`genemco.com/products.json` feed. The Admin API remains the default and is
unchanged — this is a clean switch, not a replacement.

Every record sourced this way is stamped:

```json
"shopify": { "sync_mode": "public_storefront", … }
```

so demo data stays distinguishable, and the report carries a `sync_modes` tally.
The public feed omits Admin-only data (real inventory counts, cost, unpublished
products), so **it is never authoritative** — the run logs a warning saying so.

---

## 10. Infrastructure status (verified live)

| Component | Status | Evidence |
|---|---|---|
| OpenAI embeddings | **working** | `text-embedding-3-large`, 3 strings → dim **3072**, matches `EMBEDDING_DIM` |
| Pinecone index | **working** | index `genemco-rag` created, dim 3072, metric cosine, ready=true |
| Pinecone upsert/query | **working** | 3 vectors upserted, queried back with metadata filter, self-match scored 1.0000, then deleted |
| Shopify **public** storefront | **working** | 20 real SKUs ingested, stamped `public_storefront` |
| Shopify **Admin** API | **blocked** | `401 Unauthorized` — token still a placeholder, pending from client |

Live index state after the Milestone 1 run: **590 vectors, 590 BM25 documents,
20 golden records** — all in sync. (590 = 18 identity-only SKUs + 286 chunks each
for WHB03 and WHB02, which have the Frick manual attached.)

### What changes when the Admin token arrives

Chunk IDs are `sha1(sku + field)` — independent of content and sync mode — so
re-running ingestion overwrites cleanly:

| Store | Re-sync behaviour | Verified |
|---|---|---|
| Pinecone | upsert by deterministic id — overwrites in place | ✅ |
| `data/golden_records/{sku}.json` | written by path — overwritten | ✅ |
| BM25 corpus | upsert by id — **fixed**, previously kept stale text | ✅ |

No purge step is needed. One caveat: if a future re-sync produces *fewer* spec
fields for a SKU than a previous run, the now-orphaned chunks from the old run
remain in Pinecone. That does not apply to the public→Admin transition (public
records have zero specs, so Admin can only add).

---

## Demo order that lands best

1. `pytest` — the contract rules are enforced and tested (30 sec, offline)
2. PDF parse — the hardest deliverable, working (Section 2)
3. Corrupt PDF — batch survives (Section 3)
4. `/health` + `/telemetry/query` — API is live
5. `/search` — hybrid retrieval with exact model matching
6. `/faq/{sku}` — grounded output, then the rejection table above
7. Golden record JSON — provenance and the logged conflict
