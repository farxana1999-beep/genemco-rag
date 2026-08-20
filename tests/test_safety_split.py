"""
Safety/procedure text must stay verbatim, carry a page cite, and be structurally
unable to reach the FAQ generator.
"""
from core.schemas import GoldenRecord, ShopifyBlock, SpecValue
from faq.generator import _spec_payload
from ingestion.pdf_parser import _classify_block
from pipeline.chunking import chunks_from_manual_blocks

DANGER_TEXT = (
    "DANGER: Do not attempt to disassemble the demand pump while the system is "
    "under pressure. Isolate the pump, relieve all refrigerant pressure to zero "
    "psig and verify with a calibrated gauge before removing any fasteners. "
    "Failure to do so will result in death or serious injury."
)


def make_record():
    rec = GoldenRecord(sku="WHB03", shopify=ShopifyBlock(
        title="Frick RXF-85-H Rotary Screw Compressor Package",
        url="https://genemco.com/products/frick-rxf-85-h"))
    rec.specs["oil_charge"] = SpecValue(value=36, unit="gal", source="pdf_manual",
                                        source_ref="070.410-IOM.pdf#page=42")
    return rec


def test_classifies_signal_words():
    assert _classify_block(DANGER_TEXT) == ("safety", "DANGER")
    warn = "WARNING: Rotating shaft. Keep guards in place during operation of the unit."
    assert _classify_block(warn) == ("safety", "WARNING")


def test_classifies_procedures():
    proc = ("Demand pump removal: drain the oil separator, disconnect the suction "
            "line, and remove the four mounting bolts before lifting the pump clear.")
    kind, _ = _classify_block(proc)
    assert kind == "procedure"


def test_short_noise_ignored():
    assert _classify_block("Fig. 12") is None
    assert _classify_block("") is None


def test_safety_chunk_is_verbatim_with_page():
    rec = make_record()
    blocks = [{"text": DANGER_TEXT, "page": 88, "kind": "safety", "label": "DANGER"}]
    chunks = chunks_from_manual_blocks(blocks, rec, "070.410-IOM.pdf")
    assert len(chunks) == 1
    c = chunks[0]
    # text is byte-identical to the manual's wording - no header prepended
    assert c.text == DANGER_TEXT
    assert not c.text.startswith("This is a specification")
    assert c.page == 88
    assert c.doc_type == "literature"
    assert c.section == "safety:DANGER"
    assert c.sku == "WHB03"


def test_safety_text_never_reaches_faq_generator():
    """The FAQ generator reads specs{} only; literature chunks live outside it."""
    rec = make_record()
    blocks = [{"text": DANGER_TEXT, "page": 88, "kind": "safety", "label": "DANGER"}]
    chunks_from_manual_blocks(blocks, rec, "070.410-IOM.pdf")

    payload = _spec_payload(rec)
    flat = str(payload).lower()
    assert "danger" not in flat
    assert "disassemble" not in flat
    assert set(payload) == {"oil_charge"}


def test_deterministic_chunk_ids():
    rec = make_record()
    blocks = [{"text": DANGER_TEXT, "page": 88, "kind": "safety", "label": "DANGER"}]
    a = chunks_from_manual_blocks(blocks, rec, "070.410-IOM.pdf")
    b = chunks_from_manual_blocks(blocks, rec, "070.410-IOM.pdf")
    assert a[0].chunk_id == b[0].chunk_id
