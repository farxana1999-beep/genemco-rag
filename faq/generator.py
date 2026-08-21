"""
Phase 7 — Grounded FAQ generation.
Only the SKU's structured specs{} payload is ever passed to the LLM — never raw
manual text. Output is schema-constrained (FAQItem) and then must pass the
code-level numeric validation gate in faq/validator.py before it goes anywhere.
"""
import json

from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.logging_utils import get_logger, log_exception
from core.schemas import FAQBatch, FAQItem, GoldenRecord
from faq.validator import validate_faq_batch, dedup_faqs

log = get_logger("faq_generator")

SYSTEM_PROMPT = """You generate factual product FAQs for an industrial equipment catalog.
RULES (non-negotiable):
1. Use ONLY the spec values provided in the JSON payload. Never invent, round, convert, or infer values.
2. Every FAQ must reference the product model number AND contain at least one numeric spec.
3. Do not write generic educational content (e.g. "how does a compressor work").
4. Return ONLY a JSON object: {"items": [{"question","answer","spec_field","value","unit","source_page"}, ...]}
5. spec_field must be the exact key from the payload. value must be the exact value from the payload.
"""


# Grounded FAQs are built ONLY from sources verified against a document. Catalog
# listings are scraped from a product page and checked against nothing, so they are
# structurally barred from the FAQ layer -- the same quarantine used for safety text.
FAQ_ELIGIBLE_SOURCES = {"pdf_manual", "nameplate", "telemetry"}


def faq_eligible_specs(record: GoldenRecord) -> dict:
    """The subset of specs a grounded FAQ may be built from."""
    return {f: sv for f, sv in record.specs.items()
            if sv.source in FAQ_ELIGIBLE_SOURCES}


def _spec_payload(record: GoldenRecord) -> dict:
    payload = {}
    for field, sv in faq_eligible_specs(record).items():
        page = None
        if sv.source_ref and "#page=" in str(sv.source_ref):
            try:
                page = int(str(sv.source_ref).split("#page=")[1])
            except ValueError:
                page = None
        payload[field] = {"value": sv.value, "unit": sv.unit, "source_page": page}
    return payload


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=45))
def _call_llm(record: GoldenRecord, max_items: int) -> list[dict]:
    from openai import OpenAI
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    user_msg = json.dumps({
        "sku": record.sku,
        "title": record.shopify.title,
        "manufacturer": record.shopify.vendor,
        "specs": _spec_payload(record),
        "max_faqs": max_items,
    }, default=str)

    resp = client.chat.completions.create(
        model=settings.FAQ_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content).get("items", [])


def generate_faqs(record: GoldenRecord, max_items: int = 6) -> FAQBatch:
    """Generate + schema-validate + numeric-gate + dedup. Returns only surviving FAQs."""
    eligible = faq_eligible_specs(record)
    if not eligible:
        log.info("SKU %s has no FAQ-eligible specs (catalog-only or empty) — "
                 "skipping FAQ generation, no LLM call", record.sku)
        return FAQBatch(sku=record.sku, items=[])

    try:
        raw_items = _call_llm(record, max_items)
    except Exception as e:  # noqa: BLE001
        log_exception(sku=record.sku, file=None, reason=f"faq_llm_failure: {e}")
        return FAQBatch(sku=record.sku, items=[])

    # Pydantic schema gate
    items: list[FAQItem] = []
    for raw in raw_items:
        try:
            items.append(FAQItem.model_validate(raw))
        except Exception as e:  # noqa: BLE001
            log_exception(sku=record.sku, file=None, reason=f"faq_schema_reject: {e}", item=raw)

    # Code-level numeric validation gate + anti-generic gate
    items = validate_faq_batch(record, items)
    # Embedding-similarity dedup (SEO duplicate-content protection)
    items = dedup_faqs(items)

    log.info("SKU %s: %s FAQs survived all gates", record.sku, len(items))
    return FAQBatch(sku=record.sku, items=items)


def faq_batch_to_jsonld(batch: FAQBatch) -> dict:
    """Structured JSON-LD FAQPage schema for SEO injection."""
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item.question,
                "acceptedAnswer": {"@type": "Answer", "text": item.answer},
            }
            for item in batch.items
        ],
    }
