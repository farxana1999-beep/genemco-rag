"""
All Pydantic models used across the pipeline.
These mirror SOW v3.1 Section 2 exactly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- Nameplates
class NameplateExtraction(BaseModel):
    """Strict schema for vision-LLM nameplate parsing (SOW: Vision-Parsed Nameplates)."""
    model: Optional[str] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    voltage: Optional[float] = None
    amperage: Optional[float] = None
    horsepower: Optional[float] = None
    rpm: Optional[float] = None
    phase: Optional[str] = None
    hertz: Optional[float] = None
    max_working_pressure: Optional[float] = None
    max_working_pressure_unit: Optional[str] = None
    refrigerant: Optional[str] = None
    year: Optional[int] = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, description="Overall extraction confidence 0-1")
    notes: Optional[str] = None


# ---------------------------------------------------------------- PDF specs
class SpecRecord(BaseModel):
    """A linearized table row: Model | Spec | Value | Unit (SOW: Layout-Aware PDF Parsing)."""
    model: str
    spec: str
    value: float | str
    unit: Optional[str] = None
    source_file: Optional[str] = None
    # int  -> page number within a PDF manual
    # str  -> source URL, for specs harvested from a web catalog
    source_page: Optional[int | str] = None


# ---------------------------------------------------------------- Golden Record
class SpecValue(BaseModel):
    """Every technical field carries full provenance — never a bare value."""
    value: Any
    unit: Optional[str] = None
    # "catalog_harvest" = scraped Genemco catalog listings supplied by an external
    # contractor. Deliberately its own value: these are NOT verified against any
    # engineering document, and must never be mistaken for pdf_manual provenance.
    source: Literal["shopify", "pdf_manual", "nameplate", "telemetry", "stream_c",
                    "catalog_harvest"]
    source_ref: Optional[str] = None      # file path / page / image name
    method: Optional[str] = None          # e.g. "docling_table", "vision_llm"
    confidence: Optional[float] = None


class MergeConflict(BaseModel):
    field: str
    candidates: list[dict]
    reason: str


class MergeMeta(BaseModel):
    conflicts: list[MergeConflict] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    pdf_enrichment: bool = False
    nameplate_enrichment: bool = False
    # Integrity notes raised by the SOURCE data (e.g. a serial number the supplier
    # reuses across distinct machines). Surfaced, never silently corrected.
    source_warnings: list[str] = Field(default_factory=list)


class ShopifyBlock(BaseModel):
    """Primary identity layer — authoritative for commercial fields."""
    product_id: Optional[int] = None
    title: Optional[str] = None
    handle: Optional[str] = None
    url: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    vendor: Optional[str] = None          # manufacturer
    product_type: Optional[str] = None
    variants: list[dict] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    inventory_status: Optional[str] = None
    # Provenance of this identity block. "public_storefront" records come from the
    # unauthenticated /products.json feed and lack Admin-only fields (real inventory
    # counts, cost, unpublished products) — never treat them as authoritative.
    sync_mode: Literal["admin_api", "public_storefront"] = "admin_api"


class GoldenRecord(BaseModel):
    sku: str
    shopify: ShopifyBlock = Field(default_factory=ShopifyBlock)
    specs: dict[str, SpecValue] = Field(default_factory=dict)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    merge_meta: MergeMeta = Field(default_factory=MergeMeta)
    record_version: int = 1
    last_built: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------- FAQ
class FAQItem(BaseModel):
    """Schema-constrained FAQ output (SOW: Grounded FAQ Generation)."""
    question: str
    answer: str
    spec_field: str
    value: float | str
    unit: Optional[str] = None
    # int  -> page number within a PDF manual
    # str  -> source URL, for specs harvested from a web catalog
    source_page: Optional[int | str] = None


class FAQBatch(BaseModel):
    sku: str
    items: list[FAQItem]


# ---------------------------------------------------------------- Telemetry
class TelemetryAlarm(BaseModel):
    """
    Default compressor telemetry/alarm schema (Frick Quantum HD style).
    NOTE: per SOW, Genemco provides the final pre-made Pydantic schema — drop it
    into telemetry/genemco_schema.py and it will override this default automatically.
    """
    alarm_code: Optional[str] = None
    alarm_description: Optional[str] = None
    suction_pressure_psi: Optional[float] = None
    discharge_pressure_psi: Optional[float] = None
    oil_pressure_psi: Optional[float] = None
    suction_temp_f: Optional[float] = None
    discharge_temp_f: Optional[float] = None
    oil_temp_f: Optional[float] = None
    motor_amps: Optional[float] = None
    slide_valve_pct: Optional[float] = None
    timestamp: Optional[str] = None
    unit_model: Optional[str] = None


class TelemetryQuery(BaseModel):
    sku: Optional[str] = None
    model: Optional[str] = None
    alarm_code: Optional[str] = None
    question: Optional[str] = None


# ---------------------------------------------------------------- Chunks
class Chunk(BaseModel):
    chunk_id: str
    text: str                              # includes contextual header line
    parent_text: Optional[str] = None      # small-to-big retrieval
    sku: Optional[str] = None
    model: Optional[str] = None
    source_url: Optional[str] = None
    page: Optional[int] = None
    section: Optional[str] = None
    # "catalog" = search-layer chunk built from scraped catalog listings. Kept
    # distinct from "spec" so the grounded FAQ layer can never pick it up.
    doc_type: Literal["spec", "faq", "literature", "telemetry", "shopify",
                      "catalog"] = "spec"
