"""
Verified query engine behind POST /query.

CONTRACT (locked)
    request   {query, model_code?, top_k?}
    response  {answer, verified, confidence, citations[], ungrounded}
    citation  {manual, manual_slug, source_page, url, quoted_span}

GUARANTEES
  * Every number in `answer` is checked against the values of the specs the
    answer cites -- the numeric verification gate -- before a response is built.
    If any number is not grounded, the answer is withheld: answer=null,
    ungrounded=true. Nothing unverified is ever returned as verified.
  * verified=true ONLY when every cited value comes from an authoritative source
    (pdf_manual with a page number, or nameplate). Catalog listings can ground an
    answer (ungrounded=false) but can never make it verified.
  * No guessing. An unknown machine, a spec the machine does not carry, or several
    matching machines that disagree on the value all return ungrounded=true.
  * quoted_span is the verbatim source text kept at ingest time. It is never
    reconstructed; when ingest did not keep it, it is null.

PLUG-IN SEAMS
  * GoldenStore reads two directories: authoritative golden records (manual /
    nameplate / shopify identity) and catalog search-layer records. When the
    manual corpus is ingested it writes more authoritative records into the first
    directory -- this module does not change. Manual precedence is applied at load
    time by the same merge_catalog_specs() the ingestion pipeline uses.
  * manual_urls.json ({manual_filename: url}) fills citation.url for manuals once
    their hosted locations are known (e.g. from corpus_manifest.csv source_url).
  * AnswerComposer: extractive by default -- no LLM, no API key, no per-query cost.
    An LLM composer can be dropped in; its output passes through the same gate.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from core.schemas import GoldenRecord, SpecValue
from faq.validator import _numbers_in
from ingestion.golden_record import AUTHORITATIVE_SPEC_SOURCES, merge_catalog_specs

log = logging.getLogger("verified_query")


# ------------------------------------------------------------------ contract
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    model_code: Optional[str] = Field(default=None, max_length=120)
    top_k: int = Field(default=3, ge=1, le=10)


class Citation(BaseModel):
    manual: Optional[str] = None
    manual_slug: Optional[str] = None
    source_page: Optional[int] = None
    url: Optional[str] = None
    quoted_span: Optional[str] = None


class QueryResponse(BaseModel):
    answer: Optional[str] = None
    verified: bool = False
    confidence: float = 0.0
    citations: list[Citation] = Field(default_factory=list)
    ungrounded: bool = True

    @classmethod
    def refuse(cls) -> "QueryResponse":
        """The only shape an unanswerable query may take: no answer, nothing verified."""
        return cls(answer=None, verified=False, confidence=0.0, citations=[], ungrounded=True)


# ------------------------------------------------------------------ helpers
# Machine / SKU codes: RXF-85-H, XJF151L, TDSH233S, GENF454. No inner spaces, so
# "175 HP 460 V" in a title never becomes a code.
CODE_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-?\d{2,}[A-Z0-9\-]*\b")
MODEL_FIELDS = {"model", "model_no", "model_number", "package_model", "compressor_model"}

# Fields naming an identity or a certificate rather than a performance value. They are
# only eligible when the question asks for that kind of identifier: otherwise "oil
# charge" on a Vilter package matches "certified_by_vilter_oil_separator" and answers
# with a certificate number.
IDENTIFIER_FIELD_TOKENS = {"serial", "part", "order", "drawing", "board", "tag", "sku",
                           "national", "certified", "certification", "sales"}

# A field must cover at least half of itself or half of the question to count as a
# match. One incidental shared word ("oil" in "certified_by_vilter_oil_separator") is
# not an answer to "oil charge".
MIN_COVERAGE = 0.5

STOPWORDS = {
    "what", "whats", "is", "are", "the", "of", "a", "an", "for", "on", "in", "and", "to",
    "does", "do", "how", "much", "many", "which", "its", "it", "this", "that", "with",
    "me", "tell", "give", "please", "value", "spec", "specs", "specification",
    "specifications", "about", "there", "has", "have", "by", "at", "be", "can", "you",
    "unit", "rating", "rated", "machine", "equipment", "product", "item",
}

# Phrase -> field-name tokens it implies. Field names come from the source headers
# ("BASIC CHARGE (gallon)" -> basic_charge), so a buyer's wording needs a bridge.
ALIASES: list[tuple[str, set[str]]] = [
    ("oil charge", {"basic", "charge"}),
    ("maximum", {"max"}),
    ("minimum", {"min"}),
    ("speed", {"rpm", "max"}),
    ("rpm", {"speed", "max"}),
    ("horsepower", {"hp", "horsepower"}),
    (" hp", {"hp", "horsepower"}),
    ("voltage", {"voltage", "volts", "volt"}),
    ("volt", {"voltage", "volts", "volt"}),
    ("pressure", {"pressure"}),
    ("weight", {"weight"}),
    ("serial", {"serial", "no"}),
    ("refrigerant", {"refrigerant"}),
    ("swept volume", {"geometrical", "swept", "volume"}),
    ("rotor diameter", {"rotor", "diameter"}),
    ("torque", {"torque"}),
    ("year", {"year", "built", "mfr"}),
]


def norm_code(code) -> str:
    """Upper-case and drop whitespace, but KEEP hyphens: RXF-85-H must not become RXF-85H."""
    return re.sub(r"\s+", "", str(code or "").upper()).strip("-.,;:()[]")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _field_tokens(field: str) -> set[str]:
    return {t for t in re.split(r"_+", field) if t}


def _brand_tokens(rec: GoldenRecord) -> set[str]:
    """
    Words naming the manufacturer, which must not count as spec intent.

    Catalog sub-assembly fields embed the brand ("frick_bare_screw_compressor__serial_no",
    "certified_by_vilter_oil_separator__serial_no"), so without this "What horsepower is
    the Vilter VSS1201?" matched a Vilter serial number. Brand = the title's words before
    its first model code, plus the vendor.
    """
    words: set[str] = set()
    title = rec.shopify.title or ""
    m = CODE_RE.search(title.upper())
    if m:
        words |= set(re.findall(r"[a-z]+", title[: m.start()].lower()))
    words |= set(re.findall(r"[a-z]+", (rec.shopify.vendor or "").lower()))
    return words


def _intent_tokens(query: str, codes: list[str]) -> set[str]:
    q = f" {query.lower()} "
    for c in codes:
        q = q.replace(str(c).lower(), " ")
    tokens = {t for t in re.findall(r"[a-z]+", q) if len(t) >= 2 and t not in STOPWORDS}
    for phrase, extra in ALIASES:
        if phrase in q:
            tokens |= extra
    return tokens


def same_fact(a: SpecValue, b: SpecValue) -> bool:
    """
    Do two specs state the same fact? Values must match; units must be equal or stated
    by only one side ("7000 RPM" and "7000" agree; "36 gallon" and "36 lbs" do not).
    """
    ua = (a.unit or "").strip().lower().rstrip(".")
    ub = (b.unit or "").strip().lower().rstrip(".")
    if ua and ub and ua != ub:
        return False
    try:
        return abs(float(a.value) - float(b.value)) < 1e-9
    except (TypeError, ValueError):
        norm = lambda v: re.sub(r"\s+", " ", str(v)).strip().lower()  # noqa: E731
        return norm(a.value) == norm(b.value)


def format_value(sv: SpecValue) -> str:
    v = sv.value
    if isinstance(v, bool):
        text = str(v)
    elif isinstance(v, (int, float)):
        text = str(int(v)) if float(v).is_integer() else ("%.10f" % v).rstrip("0").rstrip(".")
    else:
        text = str(v).strip()
    if sv.unit and sv.unit.strip().lower() not in text.lower():
        text = f"{text} {sv.unit.strip()}"
    return text


def grounding_numbers(sv: SpecValue) -> set[float]:
    """Numbers a cited spec legitimately supports."""
    nums: set[float] = set()
    try:
        nums.add(float(sv.value))
    except (TypeError, ValueError):
        pass
    nums |= _numbers_in(str(sv.value))
    nums |= {float(int(n)) for n in nums if float(n).is_integer()}
    return nums


def _mask_identifiers(text: str) -> str:
    """
    Drop identifier tokens before number extraction. A token that STARTS with a
    letter and contains a digit (XJF151L, TPC47, RXF-85-H, R-717) names a machine;
    it is not a quantity. Tokens starting with a digit (460V, 36) stay checked.
    """
    kept = []
    for tok in text.split():
        core = tok.strip(".,;:!?()[]{}\"'")
        if core and core[0].isalpha() and any(ch.isdigit() for ch in core):
            continue
        kept.append(tok)
    return " ".join(kept)


def verify_numbers(answer: str, grounding: set[float],
                   mask_spans: tuple[str, ...] = ()) -> tuple[bool, list[float]]:
    """
    The numeric verification gate. Every number in the answer -- other than inside
    the machine name and the spec label we printed from the source header -- must
    be one of the cited values. Returns (passed, unverified_numbers).
    """
    claim = answer
    for span in mask_spans:
        if span:
            claim = claim.replace(span, " ")
    nums = _numbers_in(_mask_identifiers(claim))
    bad = sorted(n for n in nums if n not in grounding)
    return (not bad), bad


# ------------------------------------------------------------------ composer
class AnswerComposer(Protocol):
    def compose(self, *, product: str, label: str, value: str) -> str: ...


class ExtractiveComposer:
    """Builds the answer from the cited value alone. Cannot invent a number."""

    def compose(self, *, product: str, label: str, value: str) -> str:
        return f"The {label} of {product} is {value}."


# ------------------------------------------------------------------ store
class GoldenStore:
    """In-memory view over authoritative + catalog golden records, precedence applied."""

    def __init__(self, authoritative_dir: Path, catalog_path: Optional[Path] = None,
                 manual_urls_path: Optional[Path] = None):
        self.authoritative_dir = Path(authoritative_dir)
        self.catalog_path = Path(catalog_path) if catalog_path else None
        self.manual_urls_path = Path(manual_urls_path) if manual_urls_path else None
        self._records: dict[str, GoldenRecord] = {}
        self._index: dict[str, set[str]] = {}
        self.manual_urls: dict[str, str] = {}
        self.stats: dict = {"loaded": False}
        self._lock = threading.Lock()

    @staticmethod
    def _read_source(path: Optional[Path]) -> tuple[dict[str, GoldenRecord], int]:
        """
        Read golden records from a directory of *.json or a single *.jsonl file.
        The catalog layer is ~16k records: one JSONL file loads far faster than 16k
        small files, and does not flood a synced folder with writes.
        """
        out, bad = {}, 0
        if not path:
            return out, bad
        if path.is_file() and path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        rec = GoldenRecord.model_validate_json(line)
                        out[rec.sku] = rec
                    except Exception as e:  # noqa: BLE001 - one bad line must not stop the service
                        bad += 1
                        log.warning("skipping unreadable record %s:%d: %s", path.name, n, e)
            return out, bad
        if path.is_dir():
            for p in sorted(path.glob("*.json")):
                try:
                    rec = GoldenRecord.model_validate_json(p.read_text(encoding="utf-8"))
                    out[rec.sku] = rec
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    log.warning("skipping unreadable golden record %s: %s", p.name, e)
        return out, bad

    @staticmethod
    def _codes_for(rec: GoldenRecord) -> set[str]:
        codes = {norm_code(rec.sku)}
        for field, sv in rec.specs.items():
            if (field in MODEL_FIELDS or field.endswith("__model")) and isinstance(sv.value, str):
                codes.add(norm_code(sv.value))
        if rec.shopify.title:
            codes |= {norm_code(c) for c in CODE_RE.findall(rec.shopify.title.upper())}
        return {c for c in codes if c}

    def load(self) -> dict:
        t0 = time.time()
        auth, bad_a = self._read_source(self.authoritative_dir)
        cat, bad_c = self._read_source(self.catalog_path)
        records: dict[str, GoldenRecord] = {}
        dual = conflicts = 0
        for sku, rec in auth.items():
            if sku in cat:
                dual += 1
                st = merge_catalog_specs(rec, cat[sku].specs, log_conflicts=False)
                conflicts += st["conflicts"]
                rec.shopify.url = rec.shopify.url or cat[sku].shopify.url
                rec.shopify.title = rec.shopify.title or cat[sku].shopify.title
            records[sku] = rec
        for sku, rec in cat.items():
            records.setdefault(sku, rec)

        index: dict[str, set[str]] = {}
        for sku, rec in records.items():
            for code in self._codes_for(rec):
                index.setdefault(code, set()).add(sku)

        urls: dict[str, str] = {}
        if self.manual_urls_path and self.manual_urls_path.is_file():
            try:
                urls = json.loads(self.manual_urls_path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("manual_urls.json unreadable: %s", e)

        with self._lock:
            self._records, self._index, self.manual_urls = records, index, urls
            self.stats = {
                "loaded": True, "records": len(records),
                "authoritative": len(auth), "catalog": len(cat),
                "dual_source": dual, "precedence_conflicts": conflicts,
                "unreadable_files": bad_a + bad_c,
                "load_seconds": round(time.time() - t0, 2),
            }
        return self.stats

    def get(self, sku: str) -> Optional[GoldenRecord]:
        return self._records.get(sku)

    def resolve(self, candidates: list[str]) -> tuple[Optional[str], list[str]]:
        """First candidate that names a known machine -> (as_typed, sorted skus)."""
        for cand in candidates:
            hit = self._index.get(norm_code(cand))
            if hit:
                return cand, sorted(hit)
        return None, []


# ------------------------------------------------------------------ engine
class VerifiedQueryEngine:
    def __init__(self, store: GoldenStore, composer: Optional[AnswerComposer] = None):
        self.store = store
        self.composer = composer or ExtractiveComposer()

    def _best_field(self, rec: GoldenRecord, intent: set[str]):
        """Best-matching spec in one record -> (field, SpecValue) | None | 'ambiguous'."""
        intent = intent - _brand_tokens(rec)
        if not intent:
            return None
        wants_identifier = bool(intent & IDENTIFIER_FIELD_TOKENS)
        scored = []
        for field, sv in rec.specs.items():
            ftoks = _field_tokens(field)
            if not wants_identifier and ftoks & IDENTIFIER_FIELD_TOKENS:
                continue
            score = len(intent & ftoks)
            if not score:
                continue
            if max(score / len(ftoks), score / len(intent)) < MIN_COVERAGE:
                continue
            scored.append((
                -score,                                       # more question words matched
                sv.source not in AUTHORITATIVE_SPEC_SOURCES,  # authoritative first
                -(score / len(ftoks)),                        # more specific field
                "__" in field,                                # top-level before recovered
                field, sv,
            ))
        if not scored:
            return None
        scored.sort(key=lambda t: t[:5])
        first = scored[0]
        if len(scored) > 1:
            second = scored[1]
            if first[:4] == second[:4] and not same_fact(first[5], second[5]):
                return "ambiguous"
        return first[4], first[5]

    def _citation(self, rec: GoldenRecord, sv: SpecValue) -> Citation:
        if sv.source == "pdf_manual":
            path, _, frag = str(sv.source_ref or "").partition("#page=")
            name = re.split(r"[\\/]", path)[-1] or None
            return Citation(
                manual=name,
                manual_slug=_slug(Path(name).stem) if name else None,
                source_page=int(frag) if frag.isdigit() else None,
                url=self.store.manual_urls.get(name) if name else None,
                quoted_span=sv.source_text,
            )
        if sv.source == "nameplate":
            return Citation(manual=str(sv.source_ref or "nameplate"),
                            manual_slug=_slug(str(sv.source_ref or "nameplate")),
                            source_page=None, url=None, quoted_span=sv.source_text)
        ref = str(sv.source_ref or "")
        doc = "genemco_catalog" if str(rec.sku).startswith("http") else f"genemco_catalog_{rec.sku}"
        return Citation(manual=doc, manual_slug=_slug(doc), source_page=None,
                        url=ref if ref.startswith("http") else rec.shopify.url,
                        quoted_span=sv.source_text)

    @staticmethod
    def _is_verified(sv: SpecValue, cit: Citation) -> bool:
        if sv.source == "pdf_manual":
            return cit.source_page is not None
        return sv.source == "nameplate" and bool(sv.source_ref)

    def run(self, req: QueryRequest) -> tuple[QueryResponse, str]:
        """Returns (response, reason). `reason` is for logs; the contract body is unchanged."""
        if req.model_code:
            candidates = [req.model_code]
        else:
            upper = req.query.upper()
            candidates = CODE_RE.findall(upper) + [
                t.strip(".,;:!?()[]\"'") for t in req.query.split() if any(ch.isdigit() for ch in t)
            ]
        product, skus = self.store.resolve(candidates)
        if not skus:
            return QueryResponse.refuse(), "unknown_machine" if candidates else "no_machine_named"

        intent = _intent_tokens(req.query, [product] + candidates)
        if not intent:
            return QueryResponse.refuse(), "no_spec_named"

        hits = []
        for sku in skus:
            rec = self.store.get(sku)
            best = self._best_field(rec, intent)
            if best == "ambiguous":
                return QueryResponse.refuse(), f"ambiguous_spec_in:{sku}"
            if best:
                hits.append((rec, best[0], best[1]))
        if not hits:
            return QueryResponse.refuse(), "spec_not_carried"

        for i in range(len(hits)):
            for j in range(i + 1, len(hits)):
                if not same_fact(hits[i][2], hits[j][2]):
                    return QueryResponse.refuse(), f"machines_disagree:{len(hits)}"

        hits.sort(key=lambda h: (h[2].source not in AUTHORITATIVE_SPEC_SOURCES, h[0].sku))
        # One citation per distinct source location. Two SKUs covered by the same
        # manual page would otherwise yield byte-identical citations.
        cited, seen = [], set()
        for rec, field, sv in hits:
            cit = self._citation(rec, sv)
            key = tuple(cit.model_dump().values())
            if key in seen:
                continue
            seen.add(key)
            cited.append((rec, field, sv, cit))
            if len(cited) >= req.top_k:
                break

        # Among equally authoritative citations, state the value from one that carries
        # its unit ("7000 RPM" rather than a bare "7000").
        lead_auth = cited[0][2].source in AUTHORITATIVE_SPEC_SOURCES
        _, field0, sv0, _ = next((h for h in cited if h[2].unit and
                                  (h[2].source in AUTHORITATIVE_SPEC_SOURCES) == lead_auth), cited[0])
        label = field0.replace("__", " ").replace("_", " ")
        answer = self.composer.compose(product=product, label=label, value=format_value(sv0))

        grounding: set[float] = set()
        for _, _, sv, _ in cited:
            grounding |= grounding_numbers(sv)
        passed, bad = verify_numbers(answer, grounding, mask_spans=(label, product))
        if not passed:
            return QueryResponse.refuse(), f"gate_rejected:{bad}"

        verified = all(self._is_verified(sv, cit) for _, _, sv, cit in cited)
        confidence = min(float(sv.confidence or 0.0) for _, _, sv, _ in cited)
        return QueryResponse(answer=answer, verified=verified, confidence=round(confidence, 3),
                             citations=[cit for *_, cit in cited], ungrounded=False), "ok"


# ------------------------------------------------------------------ wiring
def store_from_env() -> GoldenStore:
    """Directories are configurable so a deployment can point at its own data volume."""
    from core.config import settings
    return GoldenStore(
        authoritative_dir=Path(os.getenv("GENEMCO_GOLDEN_DIR", str(settings.GOLDEN_DIR))),
        catalog_path=Path(os.getenv("GENEMCO_CATALOG_PATH",
                                    str(settings.DATA_DIR / "search_layer" / "catalog_golden_records.jsonl"))),
        manual_urls_path=Path(os.getenv("GENEMCO_MANUAL_URLS",
                                        str(settings.DATA_DIR / "manual_urls.json"))),
    )


_engine: Optional[VerifiedQueryEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> VerifiedQueryEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            store = store_from_env()
            stats = store.load()
            log.info("golden store loaded: %s", stats)
            _engine = VerifiedQueryEngine(store)
    return _engine
