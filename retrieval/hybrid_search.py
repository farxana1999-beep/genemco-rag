"""
Phase 6 — Hybrid search.
Query flow (SOW Section 2, Hybrid Search Strategy):
  1. Extract model-number-like tokens from the query -> metadata filter first
  2. Dense vector search (top-k ~50)
  3. BM25 lexical search over the same corpus (top-k ~50)
  4. Merge (reciprocal-rank fusion) -> reranker pass -> top 5-10
This keeps exact model strings like 'RDB-222B' from being lost to embedding fuzziness.
"""
import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

from core.config import settings
from core.logging_utils import get_logger
from pipeline.embeddings import embed_texts_sync
from pipeline.vector_store import get_vector_store
from retrieval.reranker import rerank

log = get_logger("hybrid_search")

MODEL_TOKEN_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9]{2,}\b|\b[A-Z]{3,}\d{2,}[A-Z]?\b")
BM25_CORPUS_PATH = settings.DATA_DIR / "bm25_corpus.jsonl"


# ------------------------------------------------------------------ BM25 index
class BM25Index:
    """Simple persisted BM25 index over the same chunk corpus as the vector DB."""

    def __init__(self, corpus_path: Path = BM25_CORPUS_PATH):
        self.corpus_path = corpus_path
        self.docs: list[dict] = []
        self.bm25: BM25Okapi | None = None
        if corpus_path.exists():
            self.load()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9\-]+", text.lower())

    def add_documents(self, docs: list[dict]) -> None:
        """
        Upsert by chunk_id. docs: [{'id':..., 'text':..., 'sku':..., 'model':..., ...}]

        Chunk IDs are deterministic (sha1 of sku+field), so re-ingesting a SKU
        yields the same ids with fresh content — e.g. re-syncing a
        public_storefront record once the Admin API token is available. Skipping
        known ids would leave the lexical index serving stale text while the
        vector store had been overwritten, silently splitting hybrid search
        across two generations of the same document.
        """
        by_id = {d["id"]: d for d in self.docs}
        new: list[dict] = []
        updated = False
        for d in docs:
            prev = by_id.get(d["id"])
            if prev is None:
                by_id[d["id"]] = d
                new.append(d)
            elif prev != d:
                by_id[d["id"]] = d
                updated = True

        if not new and not updated:
            return

        self.docs = list(by_id.values())
        if updated:
            # An in-place edit can't be expressed in an append-only log, so rewrite.
            # Only pays the full-file cost on genuine re-ingests; first-time bulk
            # ingestion stays on the append fast path below.
            self._rewrite()
        else:
            with open(self.corpus_path, "a", encoding="utf-8") as f:
                for d in new:
                    f.write(json.dumps(d) + "\n")
        self._rebuild()

    def _rewrite(self) -> None:
        """Atomically rewrite the whole corpus file from self.docs."""
        tmp = self.corpus_path.with_suffix(self.corpus_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for d in self.docs:
                f.write(json.dumps(d) + "\n")
        tmp.replace(self.corpus_path)

    def load(self) -> None:
        self.docs = []
        with open(self.corpus_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    self.docs.append(json.loads(line))
        self._rebuild()

    def _rebuild(self) -> None:
        if self.docs:
            self.bm25 = BM25Okapi([self._tokenize(d["text"]) for d in self.docs])

    def search(self, query: str, top_k: int = 50) -> list[dict]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(self._tokenize(query))
        ranked = sorted(zip(self.docs, scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [{**doc, "score": float(score)} for doc, score in ranked if score > 0]


_bm25_singleton: BM25Index | None = None


def get_bm25() -> BM25Index:
    global _bm25_singleton
    if _bm25_singleton is None:
        _bm25_singleton = BM25Index()
    return _bm25_singleton


# ------------------------------------------------------------------ fusion
def extract_model_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(MODEL_TOKEN_RE.findall(query.upper())))


def _rrf_merge(dense: list[dict], lexical: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion of the two result lists."""
    fused: dict[str, dict] = {}
    for rank, item in enumerate(dense):
        fused.setdefault(item["id"], {**item, "rrf": 0.0})
        fused[item["id"]]["rrf"] += 1.0 / (k + rank + 1)
    for rank, item in enumerate(lexical):
        fused.setdefault(item["id"], {**item, "rrf": 0.0})
        fused[item["id"]]["rrf"] += 1.0 / (k + rank + 1)
    return sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)


def hybrid_search(query: str, top_k: int = 8, dense_k: int = 50, lexical_k: int = 50,
                  sku: str | None = None) -> list[dict]:
    store = get_vector_store()
    bm25 = get_bm25()

    # 1. Metadata pre-filter from model tokens / explicit SKU
    metadata_filter: dict | None = None
    tokens = extract_model_tokens(query)
    if sku:
        metadata_filter = {"sku": sku}
    elif tokens:
        metadata_filter = {"model": {"$in": tokens}} if settings.VECTOR_BACKEND == "pinecone" else {"model": tokens[0]}

    # 2. Dense search
    qvec = embed_texts_sync([query])[0]
    dense = store.query(qvec, top_k=dense_k, metadata_filter=metadata_filter)
    if metadata_filter and len(dense) < 3:
        # Filter too strict (token not an indexed model) — retry unfiltered
        dense = store.query(qvec, top_k=dense_k)

    # 3. BM25 lexical search (exact token matching wins here).
    # The dense leg honours the SKU filter through vector-store metadata; the
    # lexical leg has to apply it itself, or an explicit ?sku= request leaks
    # chunks from other SKUs that happen to share wording (shared manuals make
    # this the common case, not an edge case).
    lexical = bm25.search(query, top_k=lexical_k)
    if sku:
        lexical = [d for d in lexical if d.get("sku") == sku]

    # 4. Fuse + rerank
    merged = _rrf_merge(dense, lexical)
    reranked = rerank(query, merged, top_k=top_k)
    log.info("hybrid_search '%s' -> %s results (tokens=%s)", query, len(reranked), tokens)
    return reranked
