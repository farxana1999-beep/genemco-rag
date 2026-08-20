"""
Single-SKU end-to-end ingestion — the reusable unit both milestone runners call.
Shopify block -> PDF parse -> nameplate vision -> Golden Record -> chunks ->
embeddings -> vector upsert -> BM25 index.
File conventions (override dirs in .env):
  PDF manual:      data/pdf_manuals/{sku}.pdf
  Nameplate photo: data/nameplates/{sku}.jpg|.jpeg|.png
"""
from pathlib import Path

from core.config import settings
from core.logging_utils import get_logger, log_exception
from core.schemas import ShopifyBlock
from ingestion.golden_record import build_golden_record, save_golden_record
from ingestion.manual_map import manuals_for_sku, models_for_sku
from ingestion.nameplate_vision import extract_nameplate
from ingestion.pdf_parser import extract_text_blocks, parse_pdf_for_sku
from pipeline.chunking import chunks_from_golden_record, chunks_from_manual_blocks
from pipeline.embeddings import embed_texts_sync
from pipeline.vector_store import get_vector_store
from retrieval.hybrid_search import get_bm25

log = get_logger("ingest_sku")


def find_nameplate(sku: str) -> Path | None:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = settings.NAMEPLATE_DIR / f"{sku}{ext}"
        if p.exists():
            return p
    return None


def ingest_sku(sku: str, shopify_block: ShopifyBlock, store=None, bm25=None,
               skip_vision: bool = False) -> dict:
    """Returns a status dict used for completion-rate reporting."""
    status = {"sku": sku, "pdf_enrichment": False, "nameplate": False,
              "chunks": 0, "safety_chunks": 0, "manuals": [], "ok": False, "error": None}
    try:
        # A SKU may be covered by {SKU}.pdf and/or shared manuals via manual_map.json
        manuals = manuals_for_sku(sku)
        status["manuals"] = [p.name for p in manuals]

        nameplate = None
        if not skip_vision:
            np_path = find_nameplate(sku)
            if np_path:
                nameplate = extract_nameplate(sku, np_path)

        accepted_models = models_for_sku(
            sku, title=shopify_block.title,
            nameplate_model=nameplate.model if nameplate else None,
        )

        pdf_records: list = []
        manual_blocks: list[tuple[str, list[dict]]] = []
        pdf_ok = False
        for pdf_path in manuals:
            recs, ok = parse_pdf_for_sku(sku, pdf_path)
            pdf_records.extend(recs)
            pdf_ok = pdf_ok or ok
            # Safety/procedure prose is kept separate from specs and never
            # reaches the FAQ generator (see chunks_from_manual_blocks).
            blocks = extract_text_blocks(pdf_path, sku=sku)
            if blocks:
                manual_blocks.append((pdf_path.name, blocks))

        record = build_golden_record(
            sku=sku, shopify_block=shopify_block,
            pdf_records=pdf_records, nameplate=nameplate,
            pdf_enrichment_ok=pdf_ok,
            accepted_models=accepted_models,
        )
        save_golden_record(record)
        status["pdf_enrichment"] = record.merge_meta.pdf_enrichment
        status["nameplate"] = record.merge_meta.nameplate_enrichment

        chunks = chunks_from_golden_record(record)
        for manual_name, blocks in manual_blocks:
            lit = chunks_from_manual_blocks(blocks, record, manual_name)
            chunks.extend(lit)
            status["safety_chunks"] += sum(1 for c in lit if c.section.startswith("safety:"))
        if chunks:
            store = store or get_vector_store()
            vectors = embed_texts_sync([c.text for c in chunks])
            store.upsert(chunks, vectors)
            bm25 = bm25 or get_bm25()
            bm25.add_documents([
                # `page` matters: a lexical-only hit (no dense counterpart) would
                # otherwise be returned with no page, and a safety warning without
                # its page citation is not usable.
                {"id": c.chunk_id, "text": c.text, "sku": c.sku, "model": c.model,
                 "source_url": c.source_url, "section": c.section,
                 "page": c.page, "parent_text": c.parent_text}
                for c in chunks
            ])
        status["chunks"] = len(chunks)
        status["ok"] = True
    except Exception as e:  # noqa: BLE001
        status["error"] = str(e)
        log_exception(sku=sku, file=None, reason=f"ingest_sku_failure: {e}")
    return status
