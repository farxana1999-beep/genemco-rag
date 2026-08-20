"""Numeric validation gate tests — unverified numbers must be rejected."""
from core.schemas import FAQItem, GoldenRecord, ShopifyBlock, SpecValue
from faq.validator import _allowed_numbers, validate_faq_batch


def make_record():
    rec = GoldenRecord(sku="SIR07", shopify=ShopifyBlock(title="Frick RDB-222B Compressor"))
    rec.specs["model"] = SpecValue(value="RDB-222B", source="nameplate")
    rec.specs["max_working_pressure"] = SpecValue(value=300, unit="PSI", source="pdf_manual",
                                                 source_ref="manual.pdf#page=12")
    rec.specs["horsepower"] = SpecValue(value=250, unit="HP", source="pdf_manual")
    return rec


def test_valid_faq_passes():
    rec = make_record()
    item = FAQItem(question="What is the max working pressure of the RDB-222B?",
                   answer="The Frick RDB-222B has a maximum working pressure of 300 PSI.",
                   spec_field="max_working_pressure", value=300, unit="PSI", source_page=12)
    assert validate_faq_batch(rec, [item]) == [item]


def test_hallucinated_number_rejected():
    rec = make_record()
    item = FAQItem(question="What is the max working pressure of the RDB-222B?",
                   answer="The RDB-222B is rated for 350 PSI.",  # 350 not in specs
                   spec_field="max_working_pressure", value=300, unit="PSI")
    assert validate_faq_batch(rec, [item]) == []


def test_wrong_declared_value_rejected():
    rec = make_record()
    item = FAQItem(question="What HP is the RDB-222B?",
                   answer="The RDB-222B is a 250 HP unit.",
                   spec_field="horsepower", value=275, unit="HP")  # mismatch vs 250
    assert validate_faq_batch(rec, [item]) == []


def test_generic_faq_rejected():
    rec = make_record()
    item = FAQItem(question="How does a screw compressor work?",
                   answer="A screw compressor uses rotors to compress gas.",
                   spec_field="horsepower", value=250)
    assert validate_faq_batch(rec, [item]) == []


def test_unknown_spec_field_rejected():
    rec = make_record()
    item = FAQItem(question="What is the weight of the RDB-222B?",
                   answer="The RDB-222B weighs 250 lbs.",
                   spec_field="weight", value=250, unit="lbs")
    assert validate_faq_batch(rec, [item]) == []


def test_allowed_numbers_includes_string_embedded():
    rec = make_record()
    rec.specs["electrical"] = SpecValue(value="460/3/60", source="nameplate")
    allowed = _allowed_numbers(rec)
    assert 460.0 in allowed and 3.0 in allowed and 60.0 in allowed
