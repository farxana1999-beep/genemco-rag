"""
Phase 3 — Vision LLM nameplate extraction.
Sends equipment nameplate photos to GPT-4o or Claude with a tightly scoped
prompt + structured output so the response is guaranteed schema-valid.
Extractions under the confidence threshold are routed to review_queue.jsonl
instead of being silently ingested.
"""
import base64
import json
import mimetypes
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.logging_utils import get_logger, log_exception, log_review
from core.schemas import NameplateExtraction

log = get_logger("nameplate_vision")

SYSTEM_PROMPT = (
    "You are an industrial equipment data extraction system. You will be shown a "
    "photo of a physical equipment nameplate (compressor, chiller, evaporator, etc.). "
    "Extract ONLY what is actually visible and legible on the nameplate. "
    "NEVER guess, infer, or fill in typical values. If a field is not visible or "
    "not legible, return null for it. Return a per-field confidence score (0-1) in "
    "field_confidence and an overall confidence score. Respond ONLY with JSON "
    "matching the provided schema — no prose, no markdown fences."
)

JSON_SCHEMA_HINT = json.dumps(NameplateExtraction.model_json_schema(), indent=2)


def _encode_image(image_path: Path) -> tuple[str, str]:
    media_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return media_type, data


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=45))
def _call_openai_vision(image_path: Path) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    media_type, b64 = _encode_image(image_path)
    resp = client.chat.completions.create(
        model=settings.VISION_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nSchema:\n" + JSON_SCHEMA_HINT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the nameplate data from this photo."},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                ],
            },
        ],
        max_tokens=1200,
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=45))
def _call_anthropic_vision(image_path: Path) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    media_type, b64 = _encode_image(image_path)
    resp = client.messages.create(
        model=settings.VISION_MODEL,
        max_tokens=1200,
        system=SYSTEM_PROMPT + "\n\nSchema:\n" + JSON_SCHEMA_HINT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": "Extract the nameplate data from this photo. JSON only."},
                ],
            }
        ],
    )
    text = resp.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def extract_nameplate(sku: str, image_path: Path) -> NameplateExtraction | None:
    """
    Returns a validated NameplateExtraction, or None on hard failure.
    Low-confidence results are returned AND queued for human review.
    """
    if not image_path.exists():
        log_exception(sku=sku, file=str(image_path), reason="nameplate_image_not_found")
        return None
    try:
        raw = (
            _call_anthropic_vision(image_path)
            if settings.VISION_PROVIDER == "anthropic"
            else _call_openai_vision(image_path)
        )
        extraction = NameplateExtraction.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        log_exception(sku=sku, file=str(image_path), reason=f"nameplate_extraction_failure: {e}")
        return None

    if extraction.confidence < settings.NAMEPLATE_CONFIDENCE_THRESHOLD:
        log_review(
            sku=sku,
            file=str(image_path),
            reason=f"confidence {extraction.confidence:.2f} < threshold {settings.NAMEPLATE_CONFIDENCE_THRESHOLD}",
            payload=extraction.model_dump(),
        )
        log.warning("SKU %s nameplate below confidence threshold -> review queue", sku)
    return extraction


if __name__ == "__main__":
    import sys
    result = extract_nameplate(sys.argv[1], Path(sys.argv[2]))
    print(result.model_dump_json(indent=2) if result else "extraction failed")
