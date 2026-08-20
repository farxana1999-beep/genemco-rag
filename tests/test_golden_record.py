"""Merge precedence + conflict logging tests (SOW Section 2 rules)."""
from core.schemas import NameplateExtraction, ShopifyBlock, SpecRecord
from ingestion.golden_record import build_golden_record


def make_inputs():
    shopify = ShopifyBlock(title="Frick RDB-222B Rotary Screw Compressor", vendor="Frick")
    pdf = [
        SpecRecord(model="RDB-222B", spec="max_working_pressure", value=300, unit="PSI",
                   source_file="manual.pdf", source_page=12),
        SpecRecord(model="RDB-222B", spec="horsepower", value=250, unit="HP",
                   source_file="manual.pdf", source_page=12),
        SpecRecord(model="RDB-222B", spec="voltage", value=440, unit="V",
                   source_file="manual.pdf", source_page=13),
    ]
    nameplate = NameplateExtraction(
        model="RDB-222B", serial_number="SN-9981", voltage=460.0, horsepower=250.0,
        confidence=0.93, field_confidence={"voltage": 0.95, "serial_number": 0.9},
    )
    return shopify, pdf, nameplate


def test_nameplate_wins_electrical():
    shopify, pdf, nameplate = make_inputs()
    rec = build_golden_record("SIR07", shopify, pdf, nameplate, pdf_enrichment_ok=True)
    assert rec.specs["voltage"].value == 460.0
    assert rec.specs["voltage"].source == "nameplate"


def test_pdf_wins_performance():
    shopify, pdf, nameplate = make_inputs()
    rec = build_golden_record("SIR07", shopify, pdf, nameplate, pdf_enrichment_ok=True)
    assert rec.specs["max_working_pressure"].value == 300
    assert rec.specs["max_working_pressure"].source == "pdf_manual"


def test_conflict_logged_not_silently_overwritten():
    shopify, pdf, nameplate = make_inputs()
    rec = build_golden_record("SIR07", shopify, pdf, nameplate, pdf_enrichment_ok=True)
    # 440 vs 460 differs by ~4.3% < 5% tolerance -> no conflict expected
    conflict_fields = [c.field for c in rec.merge_meta.conflicts]
    assert "voltage" not in conflict_fields
    # horsepower identical -> no conflict
    assert "horsepower" not in conflict_fields


def test_conflict_beyond_tolerance():
    shopify, pdf, nameplate = make_inputs()
    nameplate.voltage = 575.0  # >5% away from 440
    rec = build_golden_record("SIR07", shopify, pdf, nameplate, pdf_enrichment_ok=True)
    conflict_fields = [c.field for c in rec.merge_meta.conflicts]
    assert "voltage" in conflict_fields
    # chosen value should still be nameplate (electrical priority), conflict logged alongside
    assert rec.specs["voltage"].value == 575.0


def test_provenance_on_every_field():
    shopify, pdf, nameplate = make_inputs()
    rec = build_golden_record("SIR07", shopify, pdf, nameplate, pdf_enrichment_ok=True)
    for field, sv in rec.specs.items():
        assert sv.source in {"shopify", "pdf_manual", "nameplate", "telemetry", "stream_c"}
        assert sv.value is not None
