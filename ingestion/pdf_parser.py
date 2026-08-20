"""
Phase 2 — Layout-aware PDF parsing.
Primary engine: Docling (open-source, strong table structure recovery).
Extracts multi-column performance tables while preserving row/column header
relationships, then linearizes each row into self-contained SpecRecords:
    Model | Spec | Value | Unit
Unparseable / password-locked / corrupted PDFs fail gracefully:
they are logged to logs/exceptions.jsonl and the SKU gets pdf_enrichment=False.
One bad PDF must NEVER crash a batch.
"""
import re
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger, log_exception
from core.schemas import SpecRecord

log = get_logger("pdf_parser")

# Model-number-like token, e.g. RDB-222B, TDSH355L, RWB-II-177, XJF 151L, RXF 85.
# The separator must be optional AND allow a space: the Frick RXF manual writes
# "XJS/XJF 95M" and "XJF 151A", which a no-space pattern misses entirely.
MODEL_TOKEN_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*[\s\-–]?\d{2,}[A-Z]*\b")

# Service manuals often label a row with bare sizes instead of full model strings:
# "85, 101" or "12 - 19" (Frick RXF oil-charge table, models numbered 12-101).
# Only used when the surrounding table has already been identified as model-keyed,
# so plain numeric lookup tables (e.g. flange loads by nozzle size) are unaffected.
BARE_MODEL_RE = re.compile(r"^\d{1,3}(\s*[-–—]\s*\d{1,3}|(\s*,\s*\d{1,3})+)?$")
NUMERIC_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")

# Common unit patterns appearing in industrial spec tables
UNIT_HINTS = {
    "psi", "psig", "psia", "bar", "kpa", "hp", "kw", "kva", "rpm", "cfm", "scfm", "acfm",
    "gpm", "tons", "ton", "tr", "btu", "btu/hr",
    "v", "volts", "a", "amps", "hz", "lbs", "lb", "kg", "in", "in.", "mm", "ft", "m",
    "gal", "gallon", "gallons", "l", "°f", "°c", "f", "c",
    # torque / load units used throughout industrial IOM manuals
    "ft-lb", "ft-lbs", "ft-lbf", "lb-ft", "nm", "n-m", "lbf", "in-lb", "in-lbs", "kgm",
}


def _clean(s) -> str:
    # Docling can emit repeated column labels, in which case row[col] is a Series
    # rather than a cell. Take the first value so the row still linearizes instead
    # of raising "truth value of a Series is ambiguous" and losing the table.
    if hasattr(s, "iloc"):
        s = s.iloc[0] if len(s) else ""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


# Vulgar fractions appear in oil-charge style tables ("3½ gallon")
VULGAR_FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3,
                    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875}


def _split_value_unit(cell: str) -> tuple[float | str, str | None]:
    """'300 PSI' -> (300.0, 'PSI'); '460V' -> (460.0, 'V'); 'Ammonia' -> ('Ammonia', None)
    Also '3½' -> (3.5, None) and '3-1/2 gal' -> (3.5, 'gal')."""
    cell = _clean(cell)

    # 3½ / ½  (optionally followed by a unit)
    m = re.match(rf"^(-?\d*)\s*([{''.join(VULGAR_FRACTIONS)}])\s*([A-Za-z°%/]+.*)?$", cell)
    if m:
        whole = float(m.group(1)) if m.group(1) else 0.0
        return whole + VULGAR_FRACTIONS[m.group(2)], _clean(m.group(3)) or None

    # 3-1/2 or 3 1/2
    m = re.match(r"^(-?\d+)[\s\-](\d+)/(\d+)\s*([A-Za-z°%/]+.*)?$", cell)
    if m:
        val = float(m.group(1)) + float(m.group(2)) / float(m.group(3))
        return val, _clean(m.group(4)) or None

    m = re.match(r"^(-?\d+(?:[.,]\d+)?)\s*([A-Za-z°%/]+.*)?$", cell)
    if m:
        tail = _clean(m.group(2)) if m.group(2) else None
        # '5/8 or 3/4' is a bolt size, not 5 with unit '/8 or 3/4'. A tail that
        # opens with '/' means the number was the numerator of a fraction, so the
        # cell is really a compound string — keep it whole rather than inventing
        # a value that is off by a factor of eight.
        if tail and tail.startswith("/"):
            return cell, None
        return float(m.group(1).replace(",", "")), tail
    return cell, None


def _unit_from_label(label: str) -> str | None:
    """
    'Max Working Pressure (PSI)' -> 'PSI'.
    Industrial spec tables usually state the unit once in the column/row label and
    leave the cells bare. _normalize_spec_name() strips that parenthetical, so
    recover it here — otherwise the SpecValue would carry a unit-less number.
    Only accepts recognised unit tokens so '(Note 2)' style asides are ignored.
    """
    for candidate in re.findall(r"\(([^)]*)\)", label or ""):
        token = _clean(candidate)
        if token and token.lower() in UNIT_HINTS:
            return token
    return None


# Reference/lookup grids that are not per-model specs. The Frick RXF pressure
# transducer conversion table is 43x9 of voltage->PSIG readings; linearizing it
# would bury a SKU's real specs under hundreds of meaningless fields.
TABLE_SKIP_PATTERNS = (
    re.compile(r"transducer\s+conversion", re.I),
    re.compile(r"\bsensor\b.*\bvoltage\b", re.I),
)

# One Docling conversion per file, shared by the table pass and the text pass.
# Converting a 68-page manual takes minutes; ingesting several SKUs that share
# one manual must not pay that cost repeatedly.
_DOC_CACHE: dict[tuple, object] = {}


def _convert(pdf_path: Path, sku: str | None = None):
    """Convert once and memoize on (path, size, mtime). Returns None on failure."""
    try:
        st = pdf_path.stat()
        key = (str(pdf_path.resolve()), st.st_size, st.st_mtime)
    except OSError as e:
        log_exception(sku=sku, file=str(pdf_path), reason=f"pdf_stat_failure: {e}")
        return None
    if key in _DOC_CACHE:
        return _DOC_CACHE[key]
    try:
        from docling.document_converter import DocumentConverter  # lazy import

        doc = DocumentConverter().convert(str(pdf_path)).document
    except Exception as e:  # noqa: BLE001
        log_exception(sku=sku, file=str(pdf_path),
                      reason=f"pdf_parse_failure: {type(e).__name__}: {e}")
        return None
    _DOC_CACHE[key] = doc
    return doc


def _skip_table(df, page_no: int | None, pdf_path: Path, sku: str | None) -> bool:
    label = " ".join(str(c) for c in df.columns)
    for pat in TABLE_SKIP_PATTERNS:
        if pat.search(label):
            log_exception(sku=sku, file=str(pdf_path),
                          reason=f"table_skipped_reference_grid: page={page_no}")
            return True
    if df.shape[0] > settings.PDF_MAX_TABLE_ROWS:
        log_exception(sku=sku, file=str(pdf_path),
                      reason=(f"table_skipped_too_large: page={page_no} "
                              f"rows={df.shape[0]} cap={settings.PDF_MAX_TABLE_ROWS}"))
        return True
    return False


def parse_pdf_tables(pdf_path: Path, sku: str | None = None) -> list[SpecRecord]:
    """
    Parse a single PDF with Docling and return linearized spec records.
    Raises nothing — returns [] on failure and logs the exception.
    `sku` is recorded on exception entries so the Section 3 exception report
    identifies which SKU each bad PDF belongs to.
    """
    doc = _convert(pdf_path, sku=sku)
    if doc is None:
        return []

    records: list[SpecRecord] = []
    for table in getattr(doc, "tables", []):
        page_no = None
        try:
            page_no = table.prov[0].page_no if table.prov else None
        except Exception:  # noqa: BLE001
            pass
        # Per-table isolation: one malformed table must not abort the tables that
        # follow it. A document-level try/except silently cost every table after
        # the first failure — half a 34-table manual.
        try:
            df = table.export_to_dataframe()
            if _skip_table(df, page_no, pdf_path, sku):
                continue
            records.extend(_linearize_table(df, str(pdf_path), page_no))
        except Exception as e:  # noqa: BLE001
            log_exception(sku=sku, file=str(pdf_path),
                          reason=f"table_linearize_failure: page={page_no}: {e}")

    log.info("Parsed %s spec records from %s", len(records), pdf_path.name)
    return records


def _linearize_table(df, source_file: str, page_no: int | None) -> list[SpecRecord]:
    """
    Convert a table DataFrame into self-contained rows.
    Two common industrial layouts are handled:
      A) model rows x spec columns  (models down the first column)
      B) spec rows x model columns  (models across the header row)
    A row whose model was only stated once in a merged/header cell is
    re-attached to every record — the critical 'reattach the model' step.
    """
    records: list[SpecRecord] = []
    if df is None or df.empty:
        return records

    df = df.fillna("").astype(str)
    header = [_clean(c) for c in df.columns]
    first_col = [_clean(v) for v in df.iloc[:, 0]]

    # Only trust bare sizes ("85", "85, 101", "12 - 19") as models when the column
    # actually says so. Otherwise a numeric lookup table keyed by nozzle size or
    # bolt diameter would masquerade as a model column.
    key_header = (header[0] if header else "").upper()
    bare_ok = "MODEL" in key_header or bool(re.search(r"\b(RXF|XJF|XJS)\b", key_header))

    def _is_model(label: str, allow_bare: bool) -> bool:
        if not label:
            return False
        if MODEL_TOKEN_RE.search(label.upper()):
            return True
        return bool(allow_bare and BARE_MODEL_RE.match(label.strip()))

    header_models = [h for h in header[1:] if _is_model(h, False)]
    firstcol_models = [v for v in first_col if _is_model(v, bare_ok)]

    if len(header_models) >= max(1, len(firstcol_models)):
        # Layout B: specs in first column, one model per remaining column
        for _, row in df.iterrows():
            spec_name = _clean(row.iloc[0])
            if not spec_name:
                continue
            for col in df.columns[1:]:
                model = _clean(col)
                if not MODEL_TOKEN_RE.search(model):
                    continue
                cell = _clean(row[col])
                if not cell:
                    continue
                value, unit = _split_value_unit(cell)
                records.append(SpecRecord(
                    model=model, spec=_normalize_spec_name(spec_name),
                    value=value, unit=unit or _unit_from_label(spec_name),
                    source_file=source_file, source_page=page_no,
                ))
    else:
        # Layout A: models in first column, specs across the header
        current_model = None
        for _, row in df.iterrows():
            cand = _clean(row.iloc[0])
            if _is_model(cand, bare_ok):
                current_model = cand  # reattach for continuation rows
            model = current_model
            if not model:
                continue
            for col in df.columns[1:]:
                spec_name = _clean(col)
                cell = _clean(row[col])
                if not spec_name or not cell:
                    continue
                value, unit = _split_value_unit(cell)
                records.append(SpecRecord(
                    model=model, spec=_normalize_spec_name(spec_name),
                    value=value, unit=unit or _unit_from_label(spec_name),
                    source_file=source_file, source_page=page_no,
                ))
    return records


def _normalize_spec_name(name: str) -> str:
    """'Max. Working Pressure (PSI)' -> 'max_working_pressure'"""
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return name or "unknown_spec"


# ------------------------------------------------------------------ safety / procedures
# Signal words are standardised across industrial IOM manuals (ANSI Z535).
SAFETY_MARKERS = ("DANGER", "WARNING", "CAUTION", "NOTICE")
PROCEDURE_HINTS = (
    "disassembl", "assembl", "removal", "install", "replace", "troubleshoot",
    "maintenance", "lubricat", "start-up", "startup", "shutdown", "procedure",
)


def _repair_glyphs(text: str) -> str:
    """
    Repair characters that survive PDF extraction as non-text artifacts.

    The manual prints "NON-OPERATIONAL" but breaks it across a line with a
    SOFT HYPHEN (U+00AD), which renders as a stray box. U+FFFD (replacement
    char) becomes a hyphen only between letters, because elsewhere it stands
    in for other glyphs (e.g. the fraction in the oil-charge table) that must
    not silently turn into hyphens. Encoding repair, never rewording.
    """
    text = text.replace("\u00ad\n", "").replace("\u00ad ", "-").replace("\u00ad", "")
    text = re.sub(r"(?<=[A-Za-z])\ufffd(\s*)(?=[A-Za-z])", r"-\1", text)
    return text


# Table-of-contents / index entries: "DEMAND PUMP DISASSEMBLY. ......... 27",
# "Bleed Valve, 25". They repeat every heading in the manual verbatim, so they
# lexically outrank the real passage they point at and push actual safety text
# down the results.
TOC_LEADER_RE = re.compile(r"\.{4,}\s*\d+|\.{6,}")
INDEX_ENTRY_RE = re.compile(r"^[A-Za-z][\w\s/&'\-]{2,40},\s*\d+(\s*,\s*\d+)*$")
NAV_SECTIONS = {"TABLE OF CONTENTS", "INDEX", "CONTENTS"}


def _is_navigation(text: str, section: str | None) -> bool:
    flat = " ".join(text.split())
    if TOC_LEADER_RE.search(flat):
        return True
    if INDEX_ENTRY_RE.match(flat):
        return True
    return bool(section and section.strip().upper() in NAV_SECTIONS)


def _classify_block(text: str, section: str | None = None) -> tuple[str, str] | None:
    """Return (doc_kind, label) for a text block worth keeping as literature."""
    stripped = text.strip()
    if len(stripped) < 40:
        return None
    if _is_navigation(stripped, section):
        return None
    head = stripped[:80].upper()
    for marker in SAFETY_MARKERS:
        # signal word appears as its own token near the start of the block
        if re.search(rf"\b{marker}\b", head):
            return "safety", marker
    low = stripped.lower()
    if any(h in low for h in PROCEDURE_HINTS):
        return "procedure", "procedure"
    return None


def extract_text_blocks(pdf_path: Path, sku: str | None = None) -> list[dict]:
    """
    Pull safety warnings and procedural prose out of a manual, VERBATIM.

    These must never reach the FAQ generator — an LLM paraphrasing a DANGER block
    is a safety problem, not a quality problem. They are returned as literature
    chunks so /search can quote them exactly, with the page number for citation.

    Returns [{"text", "page", "kind", "label"}]; never raises.
    """
    doc = _convert(pdf_path, sku=sku)
    if doc is None:
        return []

    blocks: list[dict] = []
    try:
        items = []
        for item, _level in doc.iterate_items():
            text = getattr(item, "text", None)
            if not text or not text.strip():
                continue
            page = None
            try:
                prov = getattr(item, "prov", None)
                if prov:
                    page = prov[0].page_no
            except Exception:  # noqa: BLE001
                pass
            items.append({"text": text.strip(), "page": page,
                          "label": str(getattr(item, "label", ""))})

        i = 0
        section = None
        while i < len(items):
            it = items[i]
            flat = " ".join(it["text"].split())
            upper = flat.upper()

            # A boxed panel puts the signal word in its own element, detaching it
            # from the body ("DANGER" / "BEFORE OPENING ANY VIKING PUMP..."). Stitch
            # the following elements back on until the next heading, or the block
            # would be stored as the single useless word "DANGER".
            if upper in SAFETY_MARKERS:
                body = []
                j = i + 1
                while j < len(items) and len(body) < 8:
                    nxt = items[j]
                    nxt_flat = " ".join(nxt["text"].split())
                    if nxt_flat.upper() in SAFETY_MARKERS:
                        break
                    if "section_header" in nxt["label"] and body:
                        break
                    body.append(nxt["text"])
                    j += 1
                    if re.search(r"\b(injury or death|death or serious injury|"
                                 r"catastrophic|equipment damage)\b", nxt_flat, re.I):
                        break
                if body and not _is_navigation(flat, section):
                    blocks.append({
                        "text": _repair_glyphs("\n".join([flat] + body)),  # VERBATIM
                        "page": it["page"],
                        "kind": "safety",
                        "label": upper,
                        "section_context": section,
                    })
                i = j
                continue

            if "section_header" in it["label"]:
                section = flat

            hit = _classify_block(it["text"], section)
            if hit:
                kind, label = hit
                blocks.append({
                    "text": _repair_glyphs(it["text"]),   # VERBATIM — never reworded
                    "page": it["page"],
                    "kind": kind,
                    "label": label,
                    "section_context": section,
                })
            i += 1
    except Exception as e:  # noqa: BLE001
        log_exception(sku=sku, file=str(pdf_path), reason=f"manual_text_walk_failure: {e}")

    # Boxed DANGER/WARNING panels are laid out as bordered boxes, so Docling
    # classifies them as single-column TABLES rather than paragraphs. Missing
    # these would drop exactly the safety text we most need to quote.
    try:
        for table in getattr(doc, "tables", []):
            page_no = None
            try:
                page_no = table.prov[0].page_no if table.prov else None
            except Exception:  # noqa: BLE001
                pass
            df = table.export_to_dataframe().fillna("").astype(str)
            if df.shape[1] > 2:
                continue
            cells = [str(c) for c in df.columns] + [
                str(v) for row in df.itertuples(index=False) for v in row
            ]
            text = " ".join(c.strip() for c in cells if c.strip() and c.strip() != "0")
            hit = _classify_block(text)
            if hit and hit[0] == "safety":
                blocks.append({"text": _repair_glyphs(text.strip()), "page": page_no,
                               "kind": hit[0], "label": hit[1]})
    except Exception as e:  # noqa: BLE001
        log_exception(sku=sku, file=str(pdf_path), reason=f"manual_table_safety_walk_failure: {e}")

    log.info("Extracted %s literature blocks (%s safety) from %s",
             len(blocks), sum(1 for b in blocks if b["kind"] == "safety"), pdf_path.name)
    return blocks


def parse_pdf_for_sku(sku: str, pdf_path: Path) -> tuple[list[SpecRecord], bool]:
    """Wrapper that tags exceptions with the SKU. Returns (records, pdf_enrichment_ok)."""
    if not pdf_path.exists():
        log_exception(sku=sku, file=str(pdf_path), reason="pdf_not_found")
        return [], False
    try:
        records = parse_pdf_tables(pdf_path, sku=sku)
        return records, bool(records)
    except Exception as e:  # noqa: BLE001  (belt-and-braces; parse_pdf_tables shouldn't raise)
        log_exception(sku=sku, file=str(pdf_path), reason=f"unexpected_pdf_error: {e}")
        return [], False


if __name__ == "__main__":
    import sys
    recs = parse_pdf_tables(Path(sys.argv[1]))
    for r in recs[:20]:
        print(r.model_dump())
