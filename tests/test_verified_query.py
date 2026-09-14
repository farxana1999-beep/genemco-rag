"""
Verified /query: locked contract shape, the numeric gate, manual precedence,
hyphen-exact machine resolution, and every refusal path.
"""
import json

import pytest
from fastapi.testclient import TestClient

from core.schemas import GoldenRecord, ShopifyBlock, SpecValue
from pipeline.chunking import chunks_for_catalog
from retrieval import verified_query as vq
from retrieval.verified_query import GoldenStore, QueryRequest, VerifiedQueryEngine, verify_numbers

MANUAL = "data/pdf_manuals/070.410-IOM.pdf"
T03 = "Frick RXF-85-H Rotary Screw Compressor Package (Frick XJF151L, 175 HP 460 V)"
T02 = "Frick RXF-85H Rotary Screw Compressor Package (Frick XJF151L, 206 HP 460 V)"


def _pdf(value, unit, page, text):
    return SpecValue(value=value, unit=unit, source="pdf_manual", source_ref=f"{MANUAL}#page={page}",
                     method="docling_table", confidence=0.9, source_text=text)


def _cat(value, sku, unit=None, text=None):
    return SpecValue(value=value, unit=unit, source="catalog_harvest",
                     source_ref=f"https://www.genemco.com/products/{sku.lower()}",
                     method="catalog_atomic", confidence=0.5, source_text=text)


def _rec(sku, title, specs, manual=False):
    r = GoldenRecord(sku=sku, shopify=ShopifyBlock(title=title, sync_mode="public_storefront",
                                                   url=f"https://www.genemco.com/products/{sku.lower()}"))
    r.merge_meta.pdf_enrichment = manual
    r.specs = specs
    return r


@pytest.fixture
def store(tmp_path):
    auth = tmp_path / "golden"
    auth.mkdir()
    for r in (
        _rec("WHB03", T03, {"basic_charge": _pdf(36.0, "gallon", 7, "85, 101 | BASIC CHARGE (gallon): 36"),
                            "max_speed_rpm": _pdf(4306.0, None, 4, "XJF 151L | Max Speed Rpm: 4,306")}, manual=True),
        _rec("WHB02", T02, {"max_speed_rpm": _pdf(4306.0, None, 4, "XJF 151L | Max Speed Rpm: 4,306")}, manual=True),
    ):
        (auth / f"{r.sku}.json").write_text(r.model_dump_json(), encoding="utf-8")
    catalog = [
        _rec("WHB03", T03, {"max_speed_rpm": _cat(3600.0, "WHB03", text="Max Speed RPM: 3600")}),
        _rec("TPC47", "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", "TPC47"), "max_rpm": _cat(7000.0, "TPC47", "RPM", "Max RPM: 7000 RPM")}),
        _rec("TPC45", "Frick TDSH233S Rotary Bare Screw Compressor",
             {"model": _cat("TDSH233S", "TPC45"), "max_rpm": _cat(4500.0, "TPC45")}),
        _rec("TPC42", "Frick TDSH233S Rotary Bare Screw Compressor",
             {"model": _cat("TDSH233S", "TPC42"), "max_rpm": _cat(3600.0, "TPC42")}),
        _rec("GENF454", "Ammonia Shut-Off Globe Valve (8 in)", {"size": _cat(8.0, "GENF454", "in", "Size: 8 in")}),
    ]
    cat_path = tmp_path / "catalog.jsonl"
    cat_path.write_text("\n".join(r.model_dump_json() for r in catalog) + "\n", encoding="utf-8")
    s = GoldenStore(auth, cat_path, tmp_path / "manual_urls.json")
    s.load()
    return s


def ask(store, query, model_code=None, top_k=3, composer=None):
    return VerifiedQueryEngine(store, composer).run(
        QueryRequest(query=query, model_code=model_code, top_k=top_k))


# ----------------------------------------------------------------- answers
def test_verified_answer_from_manual_with_page_citation(store):
    res, why = ask(store, "What is the oil charge?", "RXF-85-H")
    assert why == "ok"
    assert res.ungrounded is False and res.verified is True
    assert "36 gallon" in res.answer
    c = res.citations[0]
    assert (c.manual, c.manual_slug, c.source_page) == ("070.410-IOM.pdf", "070-410-iom", 7)
    assert c.quoted_span == "85, 101 | BASIC CHARGE (gallon): 36"
    assert res.confidence == 0.9


def test_catalog_answer_is_grounded_but_never_verified(store):
    res, why = ask(store, "What is the max RPM of TPC47?")
    assert why == "ok"
    assert res.ungrounded is False and res.verified is False
    assert "7000 RPM" in res.answer and res.confidence == 0.5
    c = res.citations[0]
    assert c.source_page is None and c.url.startswith("https://")
    assert c.manual == "genemco_catalog_TPC47" and c.quoted_span == "Max RPM: 7000 RPM"


def test_catalog_value_cannot_override_manual_value(store):
    res, _ = ask(store, "max speed", "RXF-85-H")
    assert res.verified is True
    assert "4306" in res.answer and "3600" not in res.answer


def test_agreeing_machines_on_one_manual_page_give_one_citation(store):
    """WHB02 and WHB03 share page 4 of the same manual: one citation, not two identical ones."""
    res, why = ask(store, "max speed", "XJF151L")
    assert why == "ok" and res.verified
    assert len(res.citations) == 1 and res.citations[0].source_page == 4


def test_top_k_caps_distinct_citations(tmp_path):
    store = _vilter_store(tmp_path, [
        _rec(sku, "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", sku), "max_rpm": _cat(7000.0, sku, "RPM")})
        for sku in ("TPC47", "TPC46", "TPC45")
    ])
    assert len(ask(store, "max rpm", "XJB151", top_k=3)[0].citations) == 3
    assert len(ask(store, "max rpm", "XJB151", top_k=1)[0].citations) == 1


def test_manual_urls_fill_citation_url(store):
    store.manual_urls_path.write_text(json.dumps({"070.410-IOM.pdf": "https://docs.example.invalid/070.410-IOM.pdf"}))
    store.load()
    res, _ = ask(store, "oil charge", "RXF-85-H")
    assert res.citations[0].url == "https://docs.example.invalid/070.410-IOM.pdf"


# ----------------------------------------------------------------- refusals
def test_model_code_resolution_is_hyphen_exact(store):
    """RXF-85-H carries an oil charge; RXF-85H does not. They must never be merged."""
    res, why = ask(store, "oil charge", "RXF-85H")
    assert res.ungrounded and res.answer is None and why == "spec_not_carried"


def test_unknown_machine_is_refused(store):
    res, why = ask(store, "oil charge", "RXF-999")
    assert res.ungrounded and res.answer is None and why == "unknown_machine"


def test_no_machine_named_is_refused(store):
    res, why = ask(store, "what is the oil charge")
    assert res.ungrounded and why == "no_machine_named"


def test_spec_the_machine_does_not_carry_is_refused(store):
    res, why = ask(store, "oil charge", "GENF454")
    assert res.ungrounded and res.answer is None and why == "spec_not_carried"


def test_machines_that_disagree_are_refused_not_guessed(store):
    res, why = ask(store, "max rpm", "TDSH233S")
    assert res.ungrounded and res.answer is None and why.startswith("machines_disagree")


class LyingComposer:
    def compose(self, *, product, label, value):
        return f"The {label} of {product} is 50 gallon."


def test_gate_withholds_a_fabricated_number(store):
    res, why = ask(store, "oil charge", "RXF-85-H", composer=LyingComposer())
    assert res.answer is None and res.ungrounded and res.verified is False
    assert res.citations == [] and res.confidence == 0.0
    assert why.startswith("gate_rejected")


# ----------------------------------------------------------------- gate unit
def test_gate_masks_identifiers_but_checks_quantities():
    assert verify_numbers("The max rpm of XJB151 (TPC47) is 7000 RPM.", {7000.0}, ("max rpm",))[0]
    ok, bad = verify_numbers("The max rpm of XJB151 is 7500 RPM.", {7000.0}, ("max rpm",))
    assert not ok and bad == [7500.0]
    assert not verify_numbers("Rated 460V.", {480.0})[0]   # number glued to a unit is still checked


def test_contract_shape_is_exact(store):
    res, _ = ask(store, "oil charge", "RXF-85-H")
    body = json.loads(res.model_dump_json())
    assert list(body) == ["answer", "verified", "confidence", "citations", "ungrounded"]
    assert list(body["citations"][0]) == ["manual", "manual_slug", "source_page", "url", "quoted_span"]
    assert json.loads(vq.QueryResponse.refuse().model_dump_json()) == {
        "answer": None, "verified": False, "confidence": 0.0, "citations": [], "ungrounded": True}


def test_catalog_chunks_are_namespaced_and_typed():
    rec = _rec("WHB03", T03, {"max_speed_rpm": _cat(3600.0, "WHB03")})
    chunks = chunks_for_catalog(rec)
    assert chunks and all(c.doc_type == "catalog" and c.chunk_id.startswith("cat_") for c in chunks)


# ----------------------------------------------------------------- HTTP
@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(vq, "_engine", VerifiedQueryEngine(store))
    monkeypatch.delenv("QUERY_UPSTREAM_TOKEN", raising=False)
    from api.query_app import app
    with TestClient(app) as c:
        yield c


def test_health_reports_ready(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["store"]["records"] == 6


def test_query_roundtrip_over_http(client):
    r = client.post("/query", json={"query": "oil charge", "model_code": "RXF-85-H"})
    assert r.status_code == 200 and r.json()["verified"] is True
    assert r.headers["X-Query-Outcome"] == "ok"


def test_ungrounded_over_http_has_no_answer(client):
    r = client.post("/query", json={"query": "oil charge", "model_code": "GENF454"})
    assert r.status_code == 200
    assert r.json() == {"answer": None, "verified": False, "confidence": 0.0, "citations": [], "ungrounded": True}
    assert r.headers["X-Query-Outcome"] == "spec_not_carried"


def test_upstream_token_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("QUERY_UPSTREAM_TOKEN", "test-only-shared-secret")
    body = {"query": "oil charge", "model_code": "RXF-85-H"}
    assert client.post("/query", json=body).status_code == 401
    assert client.post("/query", json=body, headers={"X-Upstream-Token": "wrong"}).status_code == 401
    assert client.post("/query", json=body, headers={"X-Upstream-Token": "test-only-shared-secret"}).status_code == 200
    assert client.get("/health").status_code == 200      # health stays open for the proxy


def test_request_validation(client):
    assert client.post("/query", json={"query": "oil charge", "top_k": 99}).status_code == 422
    assert client.post("/query", json={"query": ""}).status_code == 422


# ----------------------------------------------------------------- matching safety
def _vilter_store(tmp_path, records):
    auth = tmp_path / "golden_empty"
    auth.mkdir()
    cat = tmp_path / "vilter.jsonl"
    cat.write_text("\n".join(r.model_dump_json() for r in records) + "\n", encoding="utf-8")
    s = GoldenStore(auth, cat)
    s.load()
    return s


TV = "Vilter VSS1201 Rotary Screw Compressor Package (Vilter VSS1201, 500 Hp, 460 V)"


def test_brand_word_never_pulls_a_serial_number(tmp_path):
    """Regression: 'horsepower' on a single Vilter record answered with a serial number."""
    store = _vilter_store(tmp_path, [_rec("SIRA01", TV, {
        "model": _cat("VSS1201", "SIRA01"),
        "certified_by_vilter_oil_separator__serial_no": _cat("3-244237-1", "SIRA01"),
    })])
    res, why = ask(store, "What horsepower is the Vilter VSS1201?")
    assert res.ungrounded and res.answer is None and why == "spec_not_carried"


def test_incidental_shared_word_is_not_a_match(tmp_path):
    store = _vilter_store(tmp_path, [_rec("SIRA03", TV, {
        "model": _cat("VSS1201", "SIRA03"),
        "certified_by_vilter_oil_separator": _cat("National Board: A3755", "SIRA03"),
    })])
    res, why = ask(store, "What is the oil charge?", "SIRA03")
    assert res.ungrounded and why == "spec_not_carried"


def test_identifier_fields_answer_when_asked_for(tmp_path):
    store = _vilter_store(tmp_path, [_rec("SIRA01", TV, {
        "model": _cat("VSS1201", "SIRA01"), "serial_no": _cat("3-244237-1", "SIRA01", text="Serial No: 3-244237-1"),
    })])
    res, why = ask(store, "What is the serial number?", "SIRA01")
    assert why == "ok" and "3-244237-1" in res.answer and res.verified is False


def test_same_value_with_and_without_unit_agrees(tmp_path):
    store = _vilter_store(tmp_path, [
        _rec("TPC47", "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", "TPC47"), "max_rpm": _cat(7000.0, "TPC47", "RPM", "Max RPM: 7000 RPM")}),
        _rec("TPC46", "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", "TPC46"), "max_rpm": _cat(7000.0, "TPC46", None, "Max RPM: 7000")}),
    ])
    res, why = ask(store, "What is the max RPM of the Frick XJB151?")
    assert why == "ok" and res.answer.endswith("7000 RPM.") and len(res.citations) == 2


def test_real_catalog_inconsistency_is_refused(tmp_path):
    """TPC28 lists 700 RPM where its XJB151 siblings list 7000 -- refuse, never pick one."""
    store = _vilter_store(tmp_path, [
        _rec("TPC47", "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", "TPC47"), "max_rpm": _cat(7000.0, "TPC47", "RPM")}),
        _rec("TPC28", "Frick XJB151 Rotary Bare Screw Compressor",
             {"model": _cat("XJB151", "TPC28"), "max_speed_rpm": _cat(700.0, "TPC28", "RPM")}),
    ])
    res, why = ask(store, "What is the max RPM of the Frick XJB151?")
    assert res.ungrounded and why.startswith("machines_disagree")


def test_conflicting_units_disagree(tmp_path):
    from retrieval.verified_query import same_fact
    assert same_fact(_cat(7000.0, "A", "RPM"), _cat(7000.0, "B", None))
    assert not same_fact(_cat(36.0, "A", "gallon"), _cat(36.0, "B", "lbs"))
