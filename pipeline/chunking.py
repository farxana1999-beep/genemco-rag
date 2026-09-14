"""
Phase 5 — Chunking with contextual header injection.
Rules (SOW Section 2):
  * Segment by model — a chunk never spans two machines.
  * Every chunk gets a one-line contextual header prepended before embedding.
  * Hard metadata on every chunk: sku, model, source_url, page, section.
  * Small-to-big: child chunk = single fact/spec, parent = full section text.
"""
import hashlib

from core.schemas import Chunk, GoldenRecord

def _chunk_id(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:20]


def _context_header(record: GoldenRecord) -> str:
    model = None
    if "model" in record.specs:
        model = str(record.specs["model"].value)
    title = record.shopify.title or model or record.sku
    mfr = record.shopify.vendor or ""
    # Shopify titles usually already lead with the manufacturer ("Frick RDB-222B ...");
    # prepending it again would embed "Frick Frick RDB-222B ..." into every chunk.
    prefix = f"{mfr} " if mfr and mfr.lower() not in title.lower() else ""
    return f"This is a specification for {prefix}{title} (SKU: {record.sku})."


def chunks_from_golden_record(record: GoldenRecord) -> list[Chunk]:
    header = _context_header(record)
    model = str(record.specs["model"].value) if "model" in record.specs else None
    url = record.shopify.url

    # Parent section: full spec sheet text for context injection at query time
    parent_lines = [header]
    for field, sv in record.specs.items():
        unit = f" {sv.unit}" if sv.unit else ""
        parent_lines.append(f"- {field.replace('_', ' ')}: {sv.value}{unit} (source: {sv.source})")
    parent_text = "\n".join(parent_lines)

    chunks: list[Chunk] = []

    # Identity chunk from Shopify block
    identity = (
        f"{header} Product: {record.shopify.title}. Manufacturer: {record.shopify.vendor}. "
        f"Type: {record.shopify.product_type}. Tags: {', '.join(record.shopify.tags)}. "
        f"Availability: {record.shopify.inventory_status}."
    )
    chunks.append(Chunk(
        chunk_id=_chunk_id(record.sku, "identity"),
        text=identity, parent_text=parent_text,
        sku=record.sku, model=model, source_url=url, section="identity", doc_type="shopify",
    ))

    # One child chunk per spec fact — retrieval precision
    for field, sv in record.specs.items():
        unit = f" {sv.unit}" if sv.unit else ""
        page = None
        if sv.source_ref and "#page=" in str(sv.source_ref):
            try:
                page = int(str(sv.source_ref).split("#page=")[1])
            except ValueError:
                page = None
        text = f"{header} {field.replace('_', ' ').title()}: {sv.value}{unit}."
        chunks.append(Chunk(
            chunk_id=_chunk_id(record.sku, field),
            text=text, parent_text=parent_text,
            sku=record.sku, model=model, source_url=url,
            page=page, section=f"spec:{field}", doc_type="spec",
        ))
    return chunks


def chunks_from_manual_blocks(blocks: list[dict], record: GoldenRecord,
                              manual_name: str) -> list[Chunk]:
    """
    Safety warnings and procedures from a service manual -> literature chunks.

    The chunk text is the manual's wording UNCHANGED. Context that other chunk
    types carry in a prepended header lives in parent_text here instead, so a
    DANGER block is retrieved and displayed exactly as printed — prepending our
    own sentence would make /search return text that is not what the manual says.

    These are doc_type="literature" and hold no specs{} entries, so they are
    structurally unreachable from the FAQ generator, which reads only specs{}.
    """
    model = str(record.specs["model"].value) if "model" in record.specs else None
    url = record.shopify.url
    base_context = (f"From {manual_name}, applies to {record.shopify.title or record.sku} "
                    f"(SKU: {record.sku}).")

    chunks: list[Chunk] = []
    for i, b in enumerate(blocks):
        text = b.get("text", "").strip()
        if not text:
            continue
        page = b.get("page")
        label = b.get("label") or b.get("kind") or "text"
        # Which procedure the warning belongs to lives in context, never in `text`
        sec = b.get("section_context")
        context = f"{base_context} Section: {sec}." if sec else base_context
        chunks.append(Chunk(
            chunk_id=_chunk_id(record.sku, manual_name, str(page), str(i)),
            text=text,                      # VERBATIM
            parent_text=context,
            sku=record.sku,
            model=model,
            source_url=url,
            page=page,
            section=f"{b.get('kind', 'literature')}:{label}",
            doc_type="literature",
        ))
    return chunks


def chunks_from_literature(pdf_text_sections: list[tuple[str, int]], sidecar: dict) -> list[Chunk]:
    """
    Stream C: chunk literature PDFs using their JSON sidecar metadata
    (doi, title, abstract, keywords, license, pdf_url, ...).
    """
    title = sidecar.get("title", "Untitled document")
    doi = sidecar.get("doi")
    url = sidecar.get("pdf_url")
    header = f"This is from the technical document '{title}'" + (f" (DOI: {doi})." if doi else ".")

    chunks: list[Chunk] = []
    if sidecar.get("abstract"):
        chunks.append(Chunk(
            chunk_id=_chunk_id(doi or title, "abstract"),
            text=f"{header} Abstract: {sidecar['abstract']}",
            source_url=url, section="abstract", doc_type="literature",
        ))
    for i, (section_text, page) in enumerate(pdf_text_sections):
        if not section_text.strip():
            continue
        chunks.append(Chunk(
            chunk_id=_chunk_id(doi or title, f"sec{i}"),
            text=f"{header}\n{section_text.strip()}",
            source_url=url, page=page, section=f"body:{i}", doc_type="literature",
        ))
    return chunks


def chunks_for_catalog(record: GoldenRecord) -> list[Chunk]:
    """
    Search-layer chunks for a catalog_harvest Golden Record.

    Same content as chunks_from_golden_record, but doc_type="catalog" and chunk ids
    in their own "cat_" namespace. A SKU that also has a manual produces spec chunks
    with the same sha1(sku|field) ids; without the namespace a catalog upsert would
    silently overwrite the grounded chunk for that field.
    """
    return [c.model_copy(update={"chunk_id": f"cat_{c.chunk_id}", "doc_type": "catalog"})
            for c in chunks_from_golden_record(record)]
