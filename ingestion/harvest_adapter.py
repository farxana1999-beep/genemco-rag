"""
Adapter: external catalog harvest (farzana_rag_sandbox.json) -> GoldenRecord.

WHY THIS EXISTS
---------------
The supplier ships two spec containers per record:

  specs         the original bundled scrape, e.g.
                {"Model": "RXF-85-H",
                 "Frick Bare Screw Compressor": "Model: XJF151L, Serial No: 0127L, ..."}
  atomic_specs  the same data flattened to one value per field

The flattening is lossy. When a key inside a bundled string collides with a
top-level key, the top-level value wins and the nested one is discarded --
218 nested pairs across 165 records. On a packaged compressor that erases the
COMPRESSOR tier entirely: WHB03's atomic_specs contains no mention of XJF151L,
only the package model RXF-85-H. That is precisely the distinction the rest of
this pipeline exists to preserve (XJF 151A/M/L/N have different swept volumes).

So this adapter reads BOTH containers and reconciles them, rather than trusting
atomic_specs alone.

PROVENANCE
----------
Everything here is scraped catalog listing data. It is NOT verified against any
engineering document, so it gets its own source value ("catalog_harvest") and a
confidence well below the nameplate review threshold (0.7). It must never be
mistaken for pdf_manual provenance.
"""
from __future__ import annotations

import re

from core.logging_utils import get_logger
from core.schemas import GoldenRecord, MergeMeta, ShopifyBlock, SpecValue
from ingestion.pdf_parser import _split_value_unit

log = get_logger("harvest_adapter")

# Confidence tiers. All sit below NAMEPLATE_CONFIDENCE_THRESHOLD (0.7) because
# nothing in this feed has been checked against a source of truth.
CONF_ATOMIC = 0.5      # supplier already flattened it to one value
CONF_RECOVERED = 0.4   # we re-parsed it out of a bundled string
CONF_COMPOUND = 0.35   # value still holds several facts; kept whole, not split

METHOD_ATOMIC = "catalog_atomic"
METHOD_RECOVERED = "catalog_bundle_recovered"
METHOD_COMPOUND = "catalog_compound"

# "RXF-85-H", "XJF151L", "TDSH233S", "VSS1201"
MODEL_TOKEN_RE = re.compile(r"\b[A-Z]{2,}[\s\-]?\d{2,}[A-Z0-9\-]*\b")
# "Model: XJF151L, Serial No: 0127L, Refrigerant: R717"
BUNDLE_PAIR_RE = re.compile(r"([A-Za-z][\w /.\-]*?)\s*:\s*([^,]+)")
# a sub-assembly bundle key, e.g. "Frick Bare Screw Compressor"
SUBASSEMBLY_HINTS = ("compressor", "motor", "starter", "separator", "pump",
                     "panel", "engine", "condenser", "evaporator", "valve")


# Fields that are IDENTIFIERS, not measurements. Never unit-split these: a serial
# like "0127L" would become the number 127 with unit "L", silently losing the
# leading zero and turning a machine's identity into arithmetic.
IDENTIFIER_TOKENS = {"serial", "part", "model", "order", "sku", "board", "drawing",
                     "no", "number", "code", "id", "ref", "tag"}


def _is_identifier_field(field: str) -> bool:
    return bool(IDENTIFIER_TOKENS & set(field.split("_")))


def _coerce_value(field: str, value):
    """
    Split "300 PSI" into (300.0, "PSI") only when that is genuinely safe.

    Refuses when the field is an identifier, and when the trailing text still
    contains digits -- "300 PSI @ 200 F" is a value plus a CONDITION, not a unit,
    and "11551B75755253R" is a serial, not 11551 of something.
    """
    if not isinstance(value, str):
        return value, None
    raw = value.strip()
    if _is_identifier_field(field):
        return raw, None
    val, unit = _split_value_unit(raw)
    if unit and re.search(r"\d", unit):
        return raw, None                      # trailing text is not a unit
    if re.match(r"^0\d", raw):
        return raw, None                      # leading-zero code, keep verbatim
    return val, unit


def _norm_field(name) -> str:
    """'Max Design Pressure' -> 'max_design_pressure'"""
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_") or "unknown_spec"


def _parse_bundle(value) -> list[tuple[str, str]]:
    """Pull 'k: v, k: v' pairs out of a bundled string. [] if it isn't one."""
    if not isinstance(value, str):
        return []
    pairs = [(k.strip(), v.strip()) for k, v in BUNDLE_PAIR_RE.findall(value)]
    return pairs if len(pairs) >= 2 else []


def extract_model_tiers(record: dict) -> dict[str, str]:
    """
    Recover both model tiers.

    Genemco titles follow a fixed convention:
        "Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V)"
         ^^^^^^^^ package model                          ^^^^^^^^ compressor model

    The parenthetical is the authority for the compressor tier, because that is
    the one atomic_specs destroys. Falls back to a sub-assembly bundle's own
    Model key when the title carries no parenthetical.
    """
    title = str(record.get("raw_model_code") or "")
    outer, _, rest = title.partition("(")
    inner = rest.rsplit(")", 1)[0] if rest else ""

    tiers: dict[str, str] = {}
    m = MODEL_TOKEN_RE.search(outer.upper())
    if m:
        tiers["package_model"] = m.group(0).strip()
    m = MODEL_TOKEN_RE.search(inner.upper())
    if m:
        tiers["compressor_model"] = m.group(0).strip()

    # Fallback: a sub-assembly bundle naming its own Model
    if "compressor_model" not in tiers:
        for bkey, bval in (record.get("specs") or {}).items():
            if not any(h in str(bkey).lower() for h in SUBASSEMBLY_HINTS):
                continue
            for k, v in _parse_bundle(bval):
                if k.strip().lower() in ("model", "model no", "model number"):
                    tiers["compressor_model"] = v.strip()
                    break
            if "compressor_model" in tiers:
                break

    # Never let the two tiers be the same string
    if tiers.get("compressor_model") == tiers.get("package_model"):
        tiers.pop("compressor_model", None)
    return tiers


def _provenance_index(record: dict) -> dict:
    """spec_field -> its verified_specs entry, for per-field source_url/source_doc."""
    idx = {}
    for e in record.get("verified_specs") or []:
        f = _norm_field(e.get("spec_field"))
        if f not in idx:
            idx[f] = e
    return idx


def _mk(value, field: str, record: dict, prov: dict, method: str, conf: float,
        source_text: str | None = None) -> SpecValue:
    entry = prov.get(field, {})
    # source_page in this feed is a URL, not a page number -- carry it as the
    # reference string. It becomes a real page number when PDFs land in Sprint 2.
    ref = (entry.get("source_page") or entry.get("source_url")
           or record.get("source_url") or record.get("source_page"))
    val, unit = _coerce_value(field, value)
    return SpecValue(value=val, unit=unit, source="catalog_harvest",
                     source_ref=ref, method=method, confidence=conf,
                     source_text=source_text)


def build_golden_record(record: dict, shopify_block: ShopifyBlock | None = None) -> GoldenRecord:
    """
    Build a GoldenRecord from one harvest record.

    Identity is the supplier's immutable `id` (the Shopify SKU, with a source-URL
    fallback for the 19 SKUs Genemco's own catalog reuses) -- never
    clean_model_code, which collapses RXF-85-H and RXF-85H onto one key.
    """
    sku = str(record.get("id") or record.get("sku") or "").strip()
    if not sku:
        raise ValueError("harvest record has no usable id/sku")

    prov = _provenance_index(record)
    atomic = record.get("atomic_specs") or {}
    bundled = record.get("specs") or {}
    specs: dict[str, SpecValue] = {}
    compound_fields: list[str] = []
    recovered_fields: list[str] = []

    # ---- 1. atomic_specs: the supplier's flattened view
    for k, v in atomic.items():
        field = _norm_field(k)
        if _parse_bundle(v):
            # Still holds several facts. Keep it whole (splitting these shreds
            # values like "Inlets: (2) 4 in"), but mark it and recover the pairs
            # under a namespace below.
            specs[field] = _mk(v, field, record, prov, METHOD_COMPOUND, CONF_COMPOUND,
                               source_text=f"{k}: {v}")
            compound_fields.append(field)
            for nk, nv in _parse_bundle(v):
                nested = f"{field}__{_norm_field(nk)}"
                if nested not in specs:
                    specs[nested] = _mk(nv, nested, record, prov,
                                        METHOD_RECOVERED, CONF_RECOVERED,
                                        source_text=f"{nk}: {nv}")
                    recovered_fields.append(nested)
        else:
            specs[field] = _mk(v, field, record, prov, METHOD_ATOMIC, CONF_ATOMIC,
                               source_text=f"{k}: {v}")

    # ---- 2. bundled specs: recover anything the flattening dropped
    for bkey, bval in bundled.items():
        pairs = _parse_bundle(bval)
        if not pairs:
            continue
        prefix = _norm_field(bkey)
        for nk, nv in pairs:
            plain = _norm_field(nk)
            # Already preserved verbatim by the supplier's flattening? skip.
            if plain in {_norm_field(a) for a in atomic} and \
               str(atomic.get(nk, atomic.get(plain, ""))).strip() == nv.strip():
                continue
            nested = f"{prefix}__{plain}"
            if nested in specs:
                continue
            specs[nested] = _mk(nv, nested, record, prov, METHOD_RECOVERED, CONF_RECOVERED,
                                source_text=f"{nk}: {nv}")
            recovered_fields.append(nested)

    # ---- 3. canonical model tiers, so BOTH survive as first-class fields
    for field, val in extract_model_tiers(record).items():
        specs[field] = _mk(val, field, record, prov, METHOD_RECOVERED, CONF_RECOVERED,
                           source_text=record.get("raw_model_code"))

    # ---- 4. assemble
    block = shopify_block or ShopifyBlock(
        title=record.get("raw_model_code"),
        url=record.get("source_url") or record.get("source_page"),
        product_type=record.get("equipment_category"),
        sync_mode="public_storefront",
    )
    meta = MergeMeta(sources_used=["catalog_harvest"])
    meta.source_warnings = list(record.get("source_data_warnings") or [])
    if compound_fields:
        meta.source_warnings.append(
            f"compound_values_not_split: {sorted(compound_fields)}")
    if recovered_fields:
        meta.source_warnings.append(
            f"recovered_from_bundled_specs: {len(recovered_fields)} field(s)")

    gr = GoldenRecord(sku=sku, shopify=block, merge_meta=meta)
    gr.specs = specs
    return gr


def gate_target(record: dict):
    """
    The supplier's numeric-gate target, normalised to our field naming AND coerced
    the same way the spec itself was.

    Without the coercion the gate compares the supplier's raw "7000 RPM" against
    the stored 7000.0 and rejects a record that is actually correct -- a false
    negative created by the adapter, not by the data.
    """
    if not record.get("requires_numeric_gate"):
        return None
    f = record.get("spec_field")
    if not f:
        return None
    field = _norm_field(f)
    value, _unit = _coerce_value(field, record.get("value"))
    return field, value
