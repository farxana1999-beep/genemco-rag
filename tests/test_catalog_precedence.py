"""
Catalog-vs-manual precedence, and the FAQ quarantine.

Rule (Gerald, Milestone 1 close-out):
  * manual/nameplate ALWAYS win a technical spec; catalog never overwrites
  * catalog fills a gap only when the SKU has no manual at all
  * >5% numeric divergence is logged as a conflict, manual value stands
  * catalog specs may feed SEARCH, never the grounded FAQ layer
"""
from core.schemas import GoldenRecord, ShopifyBlock, SpecValue
from faq.generator import faq_eligible_specs
from ingestion.golden_record import merge_catalog_specs


def _manual_record(sku="WHB03"):
    r = GoldenRecord(sku=sku, shopify=ShopifyBlock(title="Frick RXF-85-H Package"))
    r.merge_meta.pdf_enrichment = True
    r.specs["basic_charge"] = SpecValue(value=36.0, unit="gallon", source="pdf_manual",
                                        source_ref="070.410-IOM.pdf#page=7",
                                        method="docling_table", confidence=0.9)
    r.specs["max_speed_rpm"] = SpecValue(value=4306.0, source="pdf_manual",
                                         source_ref="070.410-IOM.pdf#page=4",
                                         method="docling_table", confidence=0.9)
    return r


def _catalog_specs():
    mk = lambda v: SpecValue(value=v, source="catalog_harvest",
                             source_ref="https://genemco.com/products/x",
                             method="catalog_atomic", confidence=0.5)
    return {"max_speed_rpm": mk(3600.0), "refrigerant": mk("R-717")}


def test_manual_value_survives_catalog_merge():
    """The live WHB03 case: manual 4306 RPM must not become catalog 3600."""
    rec = _manual_record()
    merge_catalog_specs(rec, _catalog_specs())
    assert rec.specs["max_speed_rpm"].value == 4306.0
    assert rec.specs["max_speed_rpm"].source == "pdf_manual"


def test_oil_charge_and_its_page_citation_survive():
    rec = _manual_record()
    merge_catalog_specs(rec, _catalog_specs())
    sv = rec.specs["basic_charge"]
    assert (sv.value, sv.unit, sv.source) == (36.0, "gallon", "pdf_manual")
    assert "page=7" in sv.source_ref


def test_divergence_beyond_tolerance_is_logged_as_conflict():
    rec = _manual_record()
    merge_catalog_specs(rec, _catalog_specs())
    fields = [c.field for c in rec.merge_meta.conflicts]
    assert "max_speed_rpm" in fields
    c = next(c for c in rec.merge_meta.conflicts if c.field == "max_speed_rpm")
    assert "pdf_manual wins" in c.reason


def test_catalog_does_not_gap_fill_a_manual_backed_sku():
    """'refrigerant' is absent from the manual; catalog must NOT top it up."""
    rec = _manual_record()
    stats = merge_catalog_specs(rec, _catalog_specs())
    assert "refrigerant" not in rec.specs
    assert stats["gap_filled"] == 0
    assert stats["blocked_by_manual"] == 2


def test_catalog_does_gap_fill_when_no_manual_exists():
    rec = GoldenRecord(sku="GENF454", shopify=ShopifyBlock(title="Ammonia Globe Valve"))
    assert rec.merge_meta.pdf_enrichment is False
    stats = merge_catalog_specs(rec, _catalog_specs())
    assert rec.specs["refrigerant"].value == "R-717"
    assert rec.specs["refrigerant"].source == "catalog_harvest"
    assert stats["gap_filled"] == 2


def test_catalog_specs_are_barred_from_the_faq_layer():
    rec = GoldenRecord(sku="GENF454")
    merge_catalog_specs(rec, _catalog_specs())
    assert rec.specs                       # it has specs...
    assert faq_eligible_specs(rec) == {}   # ...but none may ground an FAQ


def test_manual_specs_remain_faq_eligible_after_a_catalog_merge():
    rec = _manual_record()
    merge_catalog_specs(rec, _catalog_specs())
    elig = faq_eligible_specs(rec)
    assert set(elig) == {"basic_charge", "max_speed_rpm"}
    assert all(sv.source == "pdf_manual" for sv in elig.values())


def test_catalog_merge_records_its_source():
    rec = _manual_record()
    merge_catalog_specs(rec, _catalog_specs())
    assert "catalog_harvest" in rec.merge_meta.sources_used
