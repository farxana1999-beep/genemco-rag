"""
Manual -> SKU mapping and model-alias resolution.

Real service manuals are not one-SKU spec sheets. A single Frick RXF manual
(Form 070.410-IOM) covers package models 12-101 *and* the XJF compressor models
95/120/151 fitted inside them, so:

  * one manual must be able to enrich several SKUs, and
  * each SKU must only absorb the rows that describe *its* machine.

Two small data files drive this (both optional; absent files mean "fall back to
the old {SKU}.pdf convention and nameplate-derived model"):

  data/manual_map.json   {"<manual filename>": ["SKU", ...]}
  data/model_map.json    {"sku_models": {"SKU": "RXF-85"},
                          "rxf_to_xjf": {"RXF-85": "XJF-151"}}

A SKU's accepted model aliases are the union of its package model and the
compressor model it contains, so RXF-85 pulls both RXF-level rows (oil charge)
and XJF-151 rows (swept volume) from the same document.
"""
import json
import re
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger

log = get_logger("manual_map")

MANUAL_MAP_PATH = settings.DATA_DIR / "manual_map.json"
MODEL_MAP_PATH = settings.DATA_DIR / "model_map.json"

# "RXF-85", "RXF 85", "RXF85H" -> family RXF, number 85
_FAMILY_NUM_RE = re.compile(r"\b(RXF|XJF)[\s\-]?(\d{2,3})([A-Z]*)\b", re.I)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read %s (%s) — ignoring", path.name, e)
        return {}


def load_manual_map() -> dict[str, list[str]]:
    """{"070.410-IOM.pdf": ["WHB03", "WHB02"]}"""
    raw = _load_json(MANUAL_MAP_PATH)
    out: dict[str, list[str]] = {}
    for manual, skus in raw.items():
        if isinstance(skus, str):
            skus = [skus]
        out[manual] = [str(s).strip() for s in (skus or []) if str(s).strip()]
    return out


def load_model_map() -> dict:
    return _load_json(MODEL_MAP_PATH)


def manuals_for_sku(sku: str) -> list[Path]:
    """
    Every manual that should enrich this SKU, most specific first.

    1. data/pdf_manuals/{SKU}.pdf  — the original per-SKU convention, still default
    2. any manual listing this SKU in data/manual_map.json
    """
    found: list[Path] = []
    default = settings.PDF_DIR / f"{sku}.pdf"
    if default.exists():
        found.append(default)

    for manual, skus in load_manual_map().items():
        if sku not in skus:
            continue
        path = settings.PDF_DIR / manual
        if not path.exists():
            log.warning("manual_map lists %s for %s but the file is missing", manual, sku)
            continue
        if path not in found:
            found.append(path)
    return found


def _models_from_title(title: str | None) -> list[str]:
    """
    Genemco titles carry both tiers: 'Frick RXF-85-H ... (Frick XJF151L, 175 HP)'.
    The variant letter matters — XJF 151A/M/L/N have different swept volumes — so
    it is preserved when present.
    """
    out: list[str] = []
    for family, number, suffix in _FAMILY_NUM_RE.findall(title or ""):
        alias = f"{family.upper()}-{number}{suffix.upper()}"
        if alias not in out:
            out.append(alias)
    return out


def models_for_sku(sku: str, title: str | None = None,
                   nameplate_model: str | None = None) -> list[str]:
    """
    Accepted model aliases for a SKU, used to filter a multi-model manual down to
    the rows describing this machine. Union of, in order:
      declared sku_models -> RXF->XJF expansion -> nameplate model -> title-derived.
    Empty list means "no model constraint known" (caller keeps existing behaviour).
    """
    mm = load_model_map()
    declared = mm.get("sku_models", {}).get(sku)
    rxf_to_xjf = {k.upper(): v for k, v in (mm.get("rxf_to_xjf") or {}).items()}

    aliases: list[str] = []

    def add(m: str | None):
        if not m:
            return
        m = str(m).strip()
        if m and m not in aliases:
            aliases.append(m)

    if isinstance(declared, str):
        add(declared)
    elif isinstance(declared, list):
        for d in declared:
            add(d)

    add(nameplate_model)
    for m in _models_from_title(title):
        add(m)

    # Expand each package model to the compressor model it contains.
    for m in list(aliases):
        paired = rxf_to_xjf.get(m.upper())
        if paired:
            add(paired)

    # Drop family-level aliases when a specific variant of the same family is known.
    # 'XJF-151' would match 151A/M/L/N alike, silently merging four different swept
    # volumes; if the title told us it is the L, keep only XJF-151L.
    specific = [normalize_model(a) for a in aliases]
    pruned = []
    for a in aliases:
        na = normalize_model(a)
        if any(s != na and s.startswith(na) and s[len(na):].isalpha() for s in specific):
            log.debug("dropping family alias %s in favour of a known variant", a)
            continue
        pruned.append(a)
    return pruned


def normalize_model(s: str | None) -> str:
    """'RXF-85' / 'RXF 85' / 'rxf85' -> 'RXF85'. Trailing build suffixes kept."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


_RANGE_RE = re.compile(r"^(\d{1,3})\s*[-–—]\s*(\d{1,3})$")


def _numeric_part(model: str) -> int | None:
    m = re.search(r"(\d{2,3})", normalize_model(model))
    return int(m.group(1)) if m else None


def _candidate_tokens(label: str) -> list[str]:
    """
    Real spec tables label a row for several machines at once:
        '85, 101'      -> ['85', '101']
        'XJS/XJF 95M'  -> ['XJS', 'XJF 95M']
    The full label is kept too, so '12 - 19' survives for the range test.
    """
    parts = [p.strip() for p in re.split(r"[;,/]", label) if p.strip()]
    if label.strip() not in parts:
        parts.append(label.strip())
    return parts


def _token_matches(tok: str, accepted: list[str]) -> bool:
    rng = _RANGE_RE.match(tok)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        return any((n := _numeric_part(a)) is not None and lo <= n <= hi for a in accepted)

    cand = normalize_model(tok)
    if not cand:
        return False
    for a in accepted:
        acc = normalize_model(a)
        if not acc:
            continue
        if cand == acc:
            return True
        # build/variant letter: RXF85H matches RXF85 (RXF851 must not)
        if cand.startswith(acc) and cand[len(acc):].isalpha():
            return True
        if acc.startswith(cand) and acc[len(cand):].isalpha():
            return True
        # bare size number against the accepted alias's numeric part
        if cand.isdigit() and _numeric_part(acc) == int(cand):
            return True
    return False


def model_matches(candidate: str | None, accepted: list[str]) -> bool:
    """
    Does a model label from a manual table refer to one of this SKU's machines?

    The Frick RXF manual labels rows every which way, so all of these must work:
      exact        'RXF-85'      vs accepted 'RXF-85'
      variant      'RXF-85H'     vs accepted 'RXF-85'
      bare size    '85'          vs accepted 'RXF-85'   (models are numbered 12-101)
      comma list   '85, 101'     vs accepted 'RXF-85'   (oil charge table)
      numeric range'12 - 19'     vs accepted 'RXF-19'   (oil charge table)
      slash family 'XJS/XJF 95M' vs accepted 'XJF-95M'  (swept volume table)
    """
    if not accepted:
        return True          # no constraint known — preserve prior behaviour
    if not candidate:
        return False
    return any(_token_matches(tok, accepted) for tok in _candidate_tokens(candidate))
