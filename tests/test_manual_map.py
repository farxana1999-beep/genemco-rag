"""Multi-model manual -> SKU mapping and model-alias matching."""
from core.schemas import ShopifyBlock, SpecRecord
from ingestion.golden_record import build_golden_record
from ingestion.manual_map import model_matches, normalize_model, _models_from_title


def test_normalize_model():
    assert normalize_model("RXF-85") == "RXF85"
    assert normalize_model("RXF 85") == "RXF85"
    assert normalize_model("xjf151l") == "XJF151L"


def test_exact_and_suffix_match():
    accepted = ["RXF-85", "XJF-151"]
    assert model_matches("RXF-85", accepted)
    assert model_matches("RXF 85", accepted)
    assert model_matches("RXF-85H", accepted)      # build-letter variant
    assert model_matches("XJF151L", accepted)


def test_bare_size_number_matches():
    # The RXF manual numbers packages 12-101, so table headers are often bare sizes
    assert model_matches("85", ["RXF-85"])
    assert not model_matches("50", ["RXF-85"])


def test_other_models_rejected():
    accepted = ["RXF-85", "XJF-151"]
    for other in ("RXF-101", "RXF-12", "XJF-95", "XJF-120", "RWB-II-177"):
        assert not model_matches(other, accepted), other


def test_no_constraint_keeps_everything():
    assert model_matches("ANYTHING", [])


def test_models_from_title_picks_up_both_tiers():
    """Variant letters must survive: XJF 151A/M/L/N have different swept volumes."""
    title = "Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V)"
    assert _models_from_title(title) == ["RXF-85", "XJF-151L"]


def test_family_alias_pruned_when_variant_known():
    from ingestion.manual_map import models_for_sku
    # rxf_to_xjf would add the family-level XJF-151, which matches 151A/M/L/N alike.
    # The title says 151L, so only that specific variant should survive.
    models = models_for_sku(
        "WHB03",
        title="Frick RXF-85-H Package (Frick XJF151L, 175 HP 460 V)",
    )
    assert "XJF-151L" in models
    assert "XJF-151" not in models


def test_variant_precision_in_matching():
    accepted = ["RXF-85", "XJF-151L"]
    assert model_matches("XJF 151L", accepted)
    for other in ("XJF 151A", "XJF 151M", "XJF 151N"):
        assert not model_matches(other, accepted), other


def test_oil_charge_row_labels():
    """Real Frick oil-charge table labels: ranges and comma lists."""
    accepted = ["RXF-85"]
    assert model_matches("85, 101", accepted)
    for other in ("12 - 19", "24 - 50", "58, 68"):
        assert not model_matches(other, accepted), other
    assert model_matches("12 - 19", ["RXF-19"])


def test_multi_model_manual_only_contributes_matching_rows():
    """One manual covering many machines must not dump every row into one SKU."""
    shopify = ShopifyBlock(title="Frick RXF-85-H Rotary Screw Compressor Package")
    records = [
        SpecRecord(model="RXF-85", spec="oil_charge", value=36, unit="gal",
                   source_file="070.410-IOM.pdf", source_page=42),
        SpecRecord(model="RXF-101", spec="oil_charge", value=50, unit="gal",
                   source_file="070.410-IOM.pdf", source_page=42),
        SpecRecord(model="RXF-12", spec="oil_charge", value=8, unit="gal",
                   source_file="070.410-IOM.pdf", source_page=42),
        SpecRecord(model="XJF-151", spec="swept_volume", value=151.0, unit="cfm",
                   source_file="070.410-IOM.pdf", source_page=17),
        SpecRecord(model="XJF-95", spec="swept_volume", value=95.0, unit="cfm",
                   source_file="070.410-IOM.pdf", source_page=17),
    ]
    rec = build_golden_record(
        "WHB03", shopify, records, None, pdf_enrichment_ok=True,
        accepted_models=["RXF-85", "XJF-151"],
    )
    # picks up its own package-level AND compressor-level rows
    assert rec.specs["oil_charge"].value == 36
    assert rec.specs["swept_volume"].value == 151.0
    # and nothing from the other machines in the same manual
    assert rec.specs["oil_charge"].value != 50
    assert rec.specs["swept_volume"].value != 95.0
