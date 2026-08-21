"""
Phase 4 — Golden Record builder.
Merges Shopify identity data + PDF spec records + nameplate extraction into one
authoritative record per SKU with SOW-exact precedence rules:

  * Electrical / serial fields  -> NAMEPLATE wins over PDF manual
  * Dimensional / performance   -> PDF MANUAL wins over nameplate
  * Numeric disagreement beyond tolerance (default 5%) -> logged to
    merge_meta.conflicts for human review, never silently overwritten.

Every spec field carries {value, unit, source, source_ref, method, confidence}.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger, log_conflict
from core.schemas import (
    GoldenRecord, MergeConflict, NameplateExtraction, ShopifyBlock, SpecRecord, SpecValue,
)

log = get_logger("golden_record")

# Fields where the physical nameplate is authoritative
NAMEPLATE_PRIORITY_FIELDS = {
    "serial_number", "voltage", "amperage", "horsepower", "phase", "hertz", "rpm",
    "year", "manufacturer", "refrigerant",
}
# Everything else (dimensions, capacities, pressures from performance tables)
# defaults to PDF-manual priority.

NAMEPLATE_FIELD_UNITS = {
    "voltage": "V", "amperage": "A", "horsepower": "HP", "hertz": "Hz",
    "rpm": "RPM", "max_working_pressure": None,  # unit carried separately
}


def _nameplate_to_specvalues(np: NameplateExtraction, source_ref: str) -> dict[str, SpecValue]:
    out: dict[str, SpecValue] = {}
    data = np.model_dump(exclude={"field_confidence", "confidence", "notes"})
    for field, value in data.items():
        if value is None:
            continue
        unit = np.max_working_pressure_unit if field == "max_working_pressure" else NAMEPLATE_FIELD_UNITS.get(field)
        if field == "max_working_pressure_unit":
            continue
        out[field] = SpecValue(
            value=value, unit=unit, source="nameplate", source_ref=source_ref,
            method="vision_llm",
            confidence=np.field_confidence.get(field, np.confidence),
        )
    return out


def _pdf_to_specvalues(records: list[SpecRecord], accepted_models: list[str] | None
                       ) -> dict[str, SpecValue]:
    """
    Keep only records describing this SKU's machine; last write wins within PDF.

    A multi-model service manual (e.g. Frick RXF, package models 12-101 plus XJF
    compressor models) yields rows for every machine it covers. Without this
    filter a single SKU would absorb all of them.
    """
    from ingestion.manual_map import model_matches

    out: dict[str, SpecValue] = {}
    for r in records:
        if accepted_models and r.model and not model_matches(r.model, accepted_models):
            continue
        ref = f"{r.source_file}#page={r.source_page}" if r.source_file else None
        out[r.spec] = SpecValue(
            value=r.value, unit=r.unit, source="pdf_manual",
            source_ref=ref, method="docling_table", confidence=0.9,
        )
    return out


def _values_conflict(a: SpecValue, b: SpecValue) -> bool:
    """Numeric values conflict when they differ beyond the tolerance percentage."""
    try:
        fa, fb = float(a.value), float(b.value)
    except (TypeError, ValueError):
        return str(a.value).strip().lower() != str(b.value).strip().lower()
    if fa == 0 and fb == 0:
        return False
    base = max(abs(fa), abs(fb))
    return (abs(fa - fb) / base) * 100.0 > settings.NUMERIC_CONFLICT_TOLERANCE_PCT


def apply_precedence(field: str, pdf_val: SpecValue | None, np_val: SpecValue | None
                     ) -> tuple[SpecValue | None, MergeConflict | None]:
    if pdf_val and not np_val:
        return pdf_val, None
    if np_val and not pdf_val:
        return np_val, None
    if not pdf_val and not np_val:
        return None, None

    nameplate_wins = field in NAMEPLATE_PRIORITY_FIELDS
    chosen = np_val if nameplate_wins else pdf_val

    conflict = None
    if _values_conflict(pdf_val, np_val):
        conflict = MergeConflict(
            field=field,
            candidates=[pdf_val.model_dump(), np_val.model_dump()],
            reason=f"pdf vs nameplate disagree beyond {settings.NUMERIC_CONFLICT_TOLERANCE_PCT}% tolerance",
        )
    return chosen, conflict


def build_golden_record(
    sku: str,
    shopify_block: ShopifyBlock,
    pdf_records: list[SpecRecord] | None = None,
    nameplate: NameplateExtraction | None = None,
    pdf_enrichment_ok: bool = False,
    telemetry_fields: dict | None = None,
    accepted_models: list[str] | None = None,
) -> GoldenRecord:
    record = GoldenRecord(sku=sku, shopify=shopify_block)
    record.merge_meta.sources_used.append("shopify")

    np_specs: dict[str, SpecValue] = {}
    if nameplate is not None:
        np_specs = _nameplate_to_specvalues(nameplate, source_ref=f"nameplate:{sku}")
        record.merge_meta.nameplate_enrichment = bool(np_specs)
        if np_specs:
            record.merge_meta.sources_used.append("nameplate")

    # Explicit aliases win (manual_map/model_map); otherwise fall back to the
    # nameplate-derived model, which is what a single-SKU spec sheet relies on.
    if not accepted_models and np_specs.get("model"):
        accepted_models = [str(np_specs["model"].value)]

    pdf_specs: dict[str, SpecValue] = {}
    if pdf_records:
        pdf_specs = _pdf_to_specvalues(pdf_records, accepted_models)
        record.merge_meta.pdf_enrichment = pdf_enrichment_ok and bool(pdf_specs)
        if pdf_specs:
            record.merge_meta.sources_used.append("pdf_manual")
    else:
        record.merge_meta.pdf_enrichment = False

    for field in sorted(set(pdf_specs) | set(np_specs)):
        chosen, conflict = apply_precedence(field, pdf_specs.get(field), np_specs.get(field))
        if chosen is not None:
            record.specs[field] = chosen
        if conflict is not None:
            record.merge_meta.conflicts.append(conflict)
            log_conflict(sku=sku, field=field, candidates=conflict.candidates)

    if telemetry_fields:
        record.telemetry = telemetry_fields

    record.last_built = datetime.now(timezone.utc).isoformat()
    return record


# Sources that are authoritative for TECHNICAL specs. Catalog listings are not:
# they are scraped from a product page and verified against nothing.
AUTHORITATIVE_SPEC_SOURCES = {"pdf_manual", "nameplate"}


def _conflict_reason(kept: SpecValue, rejected: SpecValue) -> str:
    """Describe the disagreement accurately -- numeric tolerance vs text mismatch."""
    try:
        float(kept.value); float(rejected.value)
        how = f"differ beyond {settings.NUMERIC_CONFLICT_TOLERANCE_PCT}% numeric tolerance"
    except (TypeError, ValueError):
        how = "text values do not match"
    return (f"{rejected.source} vs {kept.source} {how}; {kept.source} wins "
            f"(authoritative for technical specs)")


def merge_catalog_specs(record: GoldenRecord, catalog_specs: dict[str, SpecValue]) -> dict:
    """
    Fold catalog_harvest specs into an existing Golden Record WITHOUT ever
    overwriting manual or nameplate data.

    Rules (SOW precedence, extended for catalog_harvest):
      * manual/nameplate ALWAYS win a technical field -- catalog never overwrites
      * a numeric disagreement beyond tolerance is logged as a conflict, and the
        manual value stands (WHB03: manual 4306 RPM vs catalog 3600 RPM)
      * catalog fills a gap ONLY when the SKU has no manual at all
      * commercial identity (title, url, stock) lives in shopify{}, not here

    Returns a small stats dict for run reporting.
    """
    has_manual = (record.merge_meta.pdf_enrichment
                  or record.merge_meta.nameplate_enrichment)
    stats = {"gap_filled": 0, "blocked_by_manual": 0, "conflicts": 0, "skipped_existing": 0}

    for field, cv in catalog_specs.items():
        existing = record.specs.get(field)

        if existing and existing.source in AUTHORITATIVE_SPEC_SOURCES:
            stats["blocked_by_manual"] += 1
            if _values_conflict(existing, cv):
                conflict = MergeConflict(
                    field=field,
                    candidates=[existing.model_dump(), cv.model_dump()],
                    reason=_conflict_reason(existing, cv),
                )
                record.merge_meta.conflicts.append(conflict)
                log_conflict(sku=record.sku, field=field, candidates=conflict.candidates)
                stats["conflicts"] += 1
            continue

        if existing:
            stats["skipped_existing"] += 1
            continue

        if has_manual:
            # Manual-backed SKU: its technical spec set is the manual's, not the
            # catalog's. Anything the manual omits stays omitted rather than being
            # topped up from an unverified source.
            stats["blocked_by_manual"] += 1
            continue

        record.specs[field] = cv
        stats["gap_filled"] += 1

    if "catalog_harvest" not in record.merge_meta.sources_used:
        record.merge_meta.sources_used.append("catalog_harvest")
    return stats


def save_golden_record(record: GoldenRecord) -> Path:
    path = settings.GOLDEN_DIR / f"{_safe(record.sku)}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_golden_record(sku: str) -> GoldenRecord | None:
    path = settings.GOLDEN_DIR / f"{_safe(sku)}.json"
    if not path.exists():
        return None
    return GoldenRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _safe(sku: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in sku)
