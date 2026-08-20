"""
Phase 8 — Telemetry & Diagnostic JSON Schema Hook.
Per SOW: Genemco provides the pre-made Pydantic schema + test payloads to drop
directly into this endpoint. No live streaming / real-time protocol engines.

Drop-in point: create telemetry/genemco_schema.py exposing a class named
`TelemetrySchema` (a Pydantic BaseModel). If present, it automatically
overrides the default TelemetryAlarm schema. Test payloads go in
data/telemetry_payloads/*.json.

NOTE (5-hour cap, SOW Section 2): time spent integrating Genemco-provided
snippets here should be logged via core.logging_utils.log_adaptability_hours().
"""
import json
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger, log_exception
from core.schemas import TelemetryAlarm, TelemetryQuery

log = get_logger("telemetry")

PAYLOAD_DIR = settings.DATA_DIR / "telemetry_payloads"


def get_telemetry_schema():
    """Genemco-provided schema wins if present."""
    try:
        from telemetry.genemco_schema import TelemetrySchema  # type: ignore
        log.info("Using Genemco-provided telemetry schema")
        return TelemetrySchema
    except ImportError:
        return TelemetryAlarm


def load_test_payloads() -> list:
    """Validate every provided test payload against the active schema."""
    schema = get_telemetry_schema()
    payloads = []
    if not PAYLOAD_DIR.exists():
        return payloads
    for f in sorted(PAYLOAD_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            for item in items:
                payloads.append(schema.model_validate(item))
        except Exception as e:  # noqa: BLE001
            log_exception(sku=None, file=str(f), reason=f"telemetry_payload_invalid: {e}")
    log.info("Loaded %s valid telemetry payloads", len(payloads))
    return payloads


def attach_telemetry_to_record(record, payload) -> None:
    """Static telemetry attrs become fields on the SKU's golden record."""
    record.telemetry = payload.model_dump(exclude_none=True)
    if "telemetry" not in record.merge_meta.sources_used:
        record.merge_meta.sources_used.append("telemetry")


def query_telemetry(q: TelemetryQuery) -> list[dict]:
    """
    Diagnostic queries against alarm/telemetry fields stored on golden records.
    Matches on sku / model / alarm_code; free-text `question` falls through to
    a substring scan over telemetry values.
    """
    from ingestion.golden_record import load_golden_record
    results = []
    for path in settings.GOLDEN_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        tel = record.get("telemetry") or {}
        if not tel:
            continue
        if q.sku and record.get("sku") != q.sku:
            continue
        if q.model and str(tel.get("unit_model", "")).upper() != q.model.upper():
            continue
        if q.alarm_code and str(tel.get("alarm_code", "")).upper() != q.alarm_code.upper():
            continue
        if q.question:
            blob = json.dumps(tel).lower()
            if not any(tok in blob for tok in q.question.lower().split()):
                continue
        results.append({"sku": record.get("sku"), "telemetry": tel})
    return results
