# `/query` API Reference

Verified, citation-backed answers about Genemco equipment. The contract below is
**locked**. Every example on this page was captured from the running service
(`python -m api.query_app`) on 14 Sep 2026, loaded with 15,939 catalog records and
20 authoritative golden records.

## Endpoints

| Method | Path | Purpose | Shared secret |
|---|---|---|---|
| `POST` | `/query` | Ask a question about one machine | required when `QUERY_UPSTREAM_TOKEN` is set |
| `GET` | `/health` | Liveness and readiness | not required |

## Request

```json
{ "query": "What is the oil charge?", "model_code": "RXF-85-H", "top_k": 3 }
```

| Field | Type | Required | Constraints | Meaning |
|---|---|---|---|---|
| `query` | string | yes | 2–500 characters | The question |
| `model_code` | string | no | ≤ 120 characters | Model code or SKU. Matched exactly, hyphens included — `RXF-85-H` and `RXF-85H` are different machines. When omitted, codes are read from `query`. |
| `top_k` | integer | no | 1–10, default 3 | Maximum number of citations returned |

## Response

| Field | Type | Meaning |
|---|---|---|
| `answer` | string \| null | The answer, or `null` when it cannot be grounded |
| `verified` | boolean | `true` only when every citation is an engineering source |
| `confidence` | number 0–1 | Lowest confidence among the cited values; `0.0` when ungrounded |
| `citations` | array | Where the value came from; empty when ungrounded |
| `ungrounded` | boolean | `true` when no answer could be grounded |

### Citation

| Field | Type | Meaning |
|---|---|---|
| `manual` | string \| null | Source document — a manual filename, or `genemco_catalog_<SKU>` |
| `manual_slug` | string \| null | URL-safe form of `manual` |
| `source_page` | integer \| null | Page within the manual. Always `null` for catalog listings |
| `url` | string \| null | Where to view the source. Catalog: the product page. Manual: `null` until hosted manual URLs are configured |
| `quoted_span` | string \| null | Verbatim source text the value was read from. Never paraphrased; `null` if not kept at ingest |

## Guarantees

- `answer` is `null` **if and only if** `ungrounded` is `true`.
- Every number in `answer` is checked against the values of the specs it cites. If any
  number is not among them, the answer is withheld.
- `verified: true` requires every citation to be a service manual with a page number, or
  a nameplate. A catalog listing can ground an answer but can never verify it.
- A machine the system does not know, a spec that machine does not carry, or several
  matching machines that disagree all produce `ungrounded: true` — never a best guess.
- An ungrounded response is always exactly:

```json
{ "answer": null, "verified": false, "confidence": 0.0, "citations": [], "ungrounded": true }
```

## How a question is answered

1. **Resolve the machine** from `model_code`, or from codes found in `query`. Exact match only.
2. **Identify the spec** from the question's words. Manufacturer names never count as the
   spec asked for. Serial, part and certificate fields answer only when the question asks
   for them. A field must cover at least half of itself or of the question.
3. **Check agreement.** Every matching machine carrying that spec must state the same value;
   units must be equal or stated on one side only.
4. **Compose** the answer from the cited value alone.
5. **Numeric gate.** Every number in the answer must be a cited value.
6. **Cite** — manual sources first, one citation per distinct source location, up to `top_k`.

## Why an answer was withheld — `X-Query-Outcome`

The reason is sent in a response header so the locked body contract stays unchanged.

| Header value | Meaning |
|---|---|
| `ok` | Answered |
| `no_machine_named` | No `model_code`, and no model code found in `query` |
| `unknown_machine` | The model code or SKU is not in the data |
| `no_spec_named` | The question names no specification |
| `spec_not_carried` | The machine has no spec matching the question |
| `ambiguous_spec_in:<SKU>` | Two fields match equally and hold different values |
| `machines_disagree:<n>` | `n` matching machines state different values |
| `gate_rejected:[…]` | The composed answer contained numbers not in the cited values |

## Errors

| Status | When |
|---|---|
| `401` | `QUERY_UPSTREAM_TOKEN` is set and `X-Upstream-Token` is missing or wrong |
| `422` | Request validation failed (e.g. empty `query`, `top_k` out of range) |
| `503` | `/health` only — golden records not loaded yet |

---

## Examples (captured)

### 1. Verified — manual value with a page citation

RXF-85-H is SKU WHB03. The oil charge comes from the Frick 070.410-IOM service manual, page 7, and the quoted span is the table row it was read from.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-85-H"}'
```

`HTTP 200` · `X-Query-Outcome: ok`

```json
{
  "answer": "The basic charge of RXF-85-H is 36 gallon.",
  "verified": true,
  "confidence": 0.9,
  "citations": [
    {
      "manual": "070.410-IOM.pdf",
      "manual_slug": "070-410-iom",
      "source_page": 7,
      "url": null,
      "quoted_span": "85, 101 | BASIC CHARGE (gallon): 36"
    }
  ],
  "ungrounded": false
}
```

### 2. Verified — two machines agree on one manual page

The XJF151L compressor is fitted to both WHB02 and WHB03. Both carry 4306 RPM from the same manual page, so the answer comes with a single citation rather than two identical ones.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What is the max speed?", "model_code": "XJF151L"}'
```

`HTTP 200` · `X-Query-Outcome: ok`

```json
{
  "answer": "The max speed rpm of XJF151L is 4306.",
  "verified": true,
  "confidence": 0.9,
  "citations": [
    {
      "manual": "070.410-IOM.pdf",
      "manual_slug": "070-410-iom",
      "source_page": 4,
      "url": null,
      "quoted_span": "XJF 151L | Max Speed Rpm: 4,306"
    }
  ],
  "ungrounded": false
}
```

### 3. Grounded, not verified — catalog listing

No manual covers TPC47, so the value comes from its Genemco product page. It is grounded — `ungrounded: false`, with a real quote and URL — but `verified` stays `false`, with no page.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What is the max design pressure?", "model_code": "TPC47"}'
```

`HTTP 200` · `X-Query-Outcome: ok`

```json
{
  "answer": "The max design pressure of TPC47 is 350 PSI.",
  "verified": false,
  "confidence": 0.5,
  "citations": [
    {
      "manual": "genemco_catalog_TPC47",
      "manual_slug": "genemco-catalog-tpc47",
      "source_page": null,
      "url": "https://www.genemco.com/products/frick-xjb151-rotary-bare-screw-compressor-tpc47",
      "quoted_span": "Max Design Pressure: 350 PSI"
    }
  ],
  "ungrounded": false
}
```

### 4. `ungrounded` — catalog listings contradict each other

Three listings match XJB151. TPC46 and TPC47 state 7,000 RPM; TPC28 states 700 RPM. The service refuses rather than choosing one. This is a data correction for the catalog.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What is the max RPM of the Frick XJB151?"}'
```

`HTTP 200` · `X-Query-Outcome: machines_disagree:3`

```json
{
  "answer": null,
  "verified": false,
  "confidence": 0.0,
  "citations": [],
  "ungrounded": true
}
```

### 5. `ungrounded` — the value appears only in a product title

The VSS1201 listings show "500 Hp" in their titles but carry no horsepower spec. Titles are not treated as sources, so no answer is given.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What horsepower is the Vilter VSS1201?"}'
```

`HTTP 200` · `X-Query-Outcome: spec_not_carried`

```json
{
  "answer": null,
  "verified": false,
  "confidence": 0.0,
  "citations": [],
  "ungrounded": true
}
```

### 6. `ungrounded` — unknown machine

No machine with this model code exists in the data.

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: $QUERY_UPSTREAM_TOKEN" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-999"}'
```

`HTTP 200` · `X-Query-Outcome: unknown_machine`

```json
{
  "answer": null,
  "verified": false,
  "confidence": 0.0,
  "citations": [],
  "ungrounded": true
}
```

### 7. Rejected without the shared secret

```bash
curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-85-H"}'
```

`HTTP 401`

```json
{
  "detail": "unauthorized"
}
```

### 8. Health

```bash
curl -s http://127.0.0.1:9000/health
```

`HTTP 200`

```json
{
  "status": "ok",
  "store": {
    "loaded": true,
    "records": 15939,
    "authoritative": 20,
    "catalog": 15939,
    "dual_source": 20,
    "precedence_conflicts": 3,
    "unreadable_files": 0,
    "load_seconds": 1.02
  }
}
```

---

## When the manual corpus is ingested

The contract does not change. Manual-derived golden records are written to the
authoritative directory (`GENEMCO_GOLDEN_DIR`) and picked up when the service restarts.
More answers become `verified: true`, and manual precedence continues to override
catalog values for the same SKU. Add `data/manual_urls.json` — built from
`corpus_manifest.csv` `source_url` — to populate `citation.url` for manuals.
