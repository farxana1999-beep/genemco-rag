"""
Catalog-harvest adapter tests.

The supplier's atomic_specs flattening drops nested values whose key collides
with a top-level key, which erases the compressor-model tier on packaged units.
These tests pin the reconciliation that recovers it.
"""
from core.schemas import FAQItem
from ingestion.harvest_adapter import (
    _coerce_value, build_golden_record, extract_model_tiers,
)

# Shaped exactly like a real record, incl. the lossy flattening.
PACKAGED = {
    "id": "WHB03",
    "sku": "WHB03",
    "source_doc": "genemco_catalog_WHB03",
    "source_url": "https://www.genemco.com/products/frick-rxf-85-h-whb03",
    "source_page": "https://www.genemco.com/products/frick-rxf-85-h-whb03",
    "raw_model_code": "Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V)",
    "clean_model_code": "frickrxf85h",
    "equipment_category": "Screw Compressor Package",
    "specs": {
        "Model": "RXF-85-H",
        "Serial No": "S0095SFMNTHAA03",
        "Max Design Pressure": "300 PSI",
        "Frick Bare Screw Compressor":
            "Model: XJF151L, Serial No: 0127L, Refrigerant: R717, Max Speed RPM: 3600",
    },
    "atomic_specs": {
        "Model": "RXF-85-H",                 # package model overwrote XJF151L
        "Serial No": "S0095SFMNTHAA03",      # package serial overwrote 0127L
        "Max Design Pressure": "300 PSI",
        "Max Speed RPM": "3600",
    },
    "verified_specs": [
        {"spec_field": "Model", "spec_value": "RXF-85-H",
         "source_doc": "genemco_catalog_WHB03",
         "source_url": "https://www.genemco.com/products/frick-rxf-85-h-whb03",
         "source_page": "https://www.genemco.com/products/frick-rxf-85-h-whb03"},
    ],
    "spec_field": "Max Design Pressure",
    "value": "300 PSI",
    "requires_numeric_gate": True,
    "source_data_warnings": ["serial_not_unique: S0095SFMNTHAA03"],
}


def test_both_model_tiers_survive():
    """The whole point: package AND compressor model must both be present."""
    gr = build_golden_record(PACKAGED)
    assert gr.specs["package_model"].value == "RXF-85-H"
    assert gr.specs["compressor_model"].value == "XJF151L"


def test_compressor_tier_absent_from_atomic_specs_alone():
    """Guard the regression: atomic_specs by itself contains no XJF at all."""
    assert not any("XJF" in str(v).upper() for v in PACKAGED["atomic_specs"].values())


def test_nested_value_dropped_by_supplier_is_recovered():
    gr = build_golden_record(PACKAGED)
    assert gr.specs["frick_bare_screw_compressor__model"].value == "XJF151L"
    assert gr.specs["frick_bare_screw_compressor__serial_no"].value == "0127L"
    # the package-level values are untouched
    assert gr.specs["model"].value == "RXF-85-H"
    assert gr.specs["serial_no"].value == "S0095SFMNTHAA03"


def test_identity_is_the_sku_not_the_clean_code():
    gr = build_golden_record(PACKAGED)
    assert gr.sku == "WHB03"
    assert gr.sku != PACKAGED["clean_model_code"]


def test_hyphen_variants_stay_distinct():
    a = dict(PACKAGED, id="WHB03", raw_model_code="Frick RXF-85-H Package (Frick XJF151L)")
    b = dict(PACKAGED, id="WHB02", raw_model_code="Frick RXF-85H Package (Frick XJF151L)")
    ga, gb = build_golden_record(a), build_golden_record(b)
    assert ga.sku != gb.sku
    assert ga.specs["package_model"].value != gb.specs["package_model"].value


def test_serials_are_never_turned_into_numbers():
    """'0127L' must not become 127.0 with unit 'L' — that destroys identity."""
    assert _coerce_value("serial_no", "0127L") == ("0127L", None)
    assert _coerce_value("frick__serial_no", "11551B75755253R") == ("11551B75755253R", None)
    assert _coerce_value("part_no", "480158") == ("480158", None)
    gr = build_golden_record(PACKAGED)
    assert gr.specs["frick_bare_screw_compressor__serial_no"].value == "0127L"


def test_real_units_still_split():
    assert _coerce_value("max_design_pressure", "300 PSI") == (300.0, "PSI")


def test_condition_suffix_is_not_treated_as_a_unit():
    """'300 PSI @ 200 F' is a value plus a condition; keep it whole."""
    assert _coerce_value("mawp", "300 PSI @ 200 F") == ("300 PSI @ 200 F", None)


def test_harvest_provenance_is_distinct_and_low_confidence():
    gr = build_golden_record(PACKAGED)
    for field, sv in gr.specs.items():
        assert sv.source == "catalog_harvest", field
        assert sv.confidence is not None and sv.confidence < 0.7, field
        assert sv.source_ref and str(sv.source_ref).startswith("http"), field


def test_source_warnings_are_carried_not_corrected():
    gr = build_golden_record(PACKAGED)
    assert any("serial_not_unique" in w for w in gr.merge_meta.source_warnings)
    # the duplicated serial is preserved verbatim, not altered
    assert gr.specs["serial_no"].value == "S0095SFMNTHAA03"


def test_compound_value_kept_whole_and_flagged():
    rec = dict(PACKAGED, atomic_specs={"Inlets Outlets": "Inlet: 4 in, Outlet: 6 in"})
    gr = build_golden_record(rec)
    assert gr.specs["inlets_outlets"].method == "catalog_compound"
    assert gr.specs["inlets_outlets"].value == "Inlet: 4 in, Outlet: 6 in"
    # ...and its parts are still recoverable alongside it
    assert gr.specs["inlets_outlets__inlet"].value == 4.0


def test_faq_item_accepts_a_url_source_page():
    """Harvested specs cite a URL; FAQItem must tolerate that, not just int pages."""
    item = FAQItem(question="q", answer="a 300", spec_field="max_design_pressure",
                   value="300 PSI", source_page="https://example.invalid/p")
    assert item.source_page == "https://example.invalid/p"


def test_model_tiers_not_duplicated_when_no_parenthetical():
    rec = dict(PACKAGED, raw_model_code="Frick XJB151 Rotary Bare Screw Compressor",
               specs={"Model": "XJB151"}, atomic_specs={"Model": "XJB151"})
    tiers = extract_model_tiers(rec)
    assert tiers.get("package_model") == "XJB151"
    assert "compressor_model" not in tiers
