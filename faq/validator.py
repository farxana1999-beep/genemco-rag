"""
Code-level validation gate (SOW Section 2):
  * Every number appearing in a generated answer must exist in that SKU's
    verified specs{} record — otherwise the FAQ is dropped.
  * FAQs missing BOTH a model-number reference and a numeric spec are rejected
    (kills generic output automatically).
  * Near-identical FAQs are deduped by embedding similarity before going live.
This is deliberately separate from Pydantic schema validation — schema says the
shape is right; this gate says the FACTS are right.
"""
import re

from core.logging_utils import get_logger, log_exception
from core.schemas import FAQItem, GoldenRecord

log = get_logger("faq_validator")

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[float]:
    return {float(n) for n in NUM_RE.findall(text.replace(",", ""))}


def _allowed_numbers(record: GoldenRecord) -> set[float]:
    allowed: set[float] = set()
    for sv in record.specs.values():
        try:
            allowed.add(float(sv.value))
        except (TypeError, ValueError):
            # pull numbers embedded in string values too ("460/3/60")
            allowed |= _numbers_in(str(sv.value))
    # Common derived tolerances: allow ints of floats (300.0 vs 300)
    allowed |= {float(int(v)) for v in allowed if float(v).is_integer()}
    return allowed


# Model-like token inside a product title: RXF-85-H, XJF151L, RDB-222B, VSS1201
TITLE_MODEL_TOKEN_RE = re.compile(r"\b[A-Z]{2,}[-\s]?\d{2,}[A-Z0-9\-]*\b")


def _model_strings(record: GoldenRecord) -> list[str]:
    """
    Strings whose presence proves an FAQ is about this specific machine.

    Includes model TOKENS pulled out of the Shopify title, not just the whole
    title: a real title is "Frick RXF-85-H Rotary Screw Compressor Package (Frick
    XJF151L, 175 HP 460 V)", and requiring that entire string to appear verbatim
    in an answer rejects every FAQ, including correct ones naming the RXF-85-H.
    The anti-generic intent is preserved — a token still has to be mentioned.
    """
    models: list[str] = []
    if "model" in record.specs:
        models.append(str(record.specs["model"].value))
    title = record.shopify.title
    if title:
        models.append(title)
        models.extend(TITLE_MODEL_TOKEN_RE.findall(title.upper()))
    return [m for m in models if m]


def validate_faq_item(record: GoldenRecord, item: FAQItem, allowed: set[float]) -> tuple[bool, str]:
    # 1. spec_field must exist in the record
    if item.spec_field not in record.specs:
        return False, f"spec_field '{item.spec_field}' not in golden record"

    # 2. declared value must match the record value
    recorded = record.specs[item.spec_field].value
    try:
        if float(item.value) != float(recorded):
            return False, f"declared value {item.value} != recorded {recorded}"
    except (TypeError, ValueError):
        if str(item.value).strip().lower() not in str(recorded).strip().lower():
            return False, f"declared value '{item.value}' not found in recorded '{recorded}'"

    # 3. EVERY number in the answer must exist in verified specs
    answer_numbers = _numbers_in(item.answer)
    unverified = {n for n in answer_numbers if n not in allowed}
    # allow years/pages that equal source_page (only when it IS a page number;
    # harvested records carry a URL there, which has no numeric meaning)
    if isinstance(item.source_page, int):
        unverified.discard(float(item.source_page))
    if unverified:
        return False, f"unverified numbers in answer: {sorted(unverified)}"

    # 4. anti-generic gate: must reference model AND contain a numeric spec
    text = f"{item.question} {item.answer}"
    has_model = any(m.lower() in text.lower() for m in _model_strings(record))
    if not has_model:
        return False, "no model-number reference — generic output rejected"
    if not answer_numbers:
        return False, "no numeric spec in answer — generic output rejected"

    return True, "ok"


def validate_faq_batch(record: GoldenRecord, items: list[FAQItem]) -> list[FAQItem]:
    allowed = _allowed_numbers(record)
    kept: list[FAQItem] = []
    for item in items:
        ok, reason = validate_faq_item(record, item, allowed)
        if ok:
            kept.append(item)
        else:
            log_exception(sku=record.sku, file=None, reason=f"faq_gate_reject: {reason}",
                          question=item.question)
    return kept


def dedup_faqs(items: list[FAQItem], similarity_threshold: float = 0.92) -> list[FAQItem]:
    """Drop near-identical FAQs by embedding cosine similarity."""
    if len(items) <= 1:
        return items
    try:
        from pipeline.embeddings import embed_texts_sync
        import math

        texts = [f"{i.question} {i.answer}" for i in items]
        vecs = embed_texts_sync(texts)

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0

        kept: list[FAQItem] = []
        kept_vecs: list[list[float]] = []
        for item, vec in zip(items, vecs):
            if all(cos(vec, kv) < similarity_threshold for kv in kept_vecs):
                kept.append(item)
                kept_vecs.append(vec)
        return kept
    except Exception as e:  # noqa: BLE001
        log.warning("FAQ dedup skipped (%s)", e)
        return items
