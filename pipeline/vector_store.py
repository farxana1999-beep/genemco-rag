"""
Vector store abstraction — Pinecone (serverless) by default, Weaviate optional.
Every vector carries its chunk's hard metadata so hybrid search can filter
by SKU/model before semantic scoring.
"""
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.logging_utils import get_logger
from core.schemas import Chunk

log = get_logger("vector_store")


class VectorStore:
    def init_index(self) -> None: ...
    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int: ...
    def query(self, vector: list[float], top_k: int = 50, metadata_filter: dict | None = None) -> list[dict]: ...


class PineconeStore(VectorStore):
    def __init__(self):
        from pinecone import Pinecone
        self.pc = Pinecone(api_key=settings.PINECONE_API_KEY)
        self.index_name = settings.PINECONE_INDEX
        self._index = None

    def init_index(self) -> None:
        from pinecone import ServerlessSpec
        existing = [i.name for i in self.pc.list_indexes()]
        if self.index_name not in existing:
            log.info("Creating Pinecone index '%s' (dim=%s)", self.index_name, settings.EMBEDDING_DIM)
            self.pc.create_index(
                name=self.index_name,
                dimension=settings.EMBEDDING_DIM,
                metric="cosine",
                spec=ServerlessSpec(cloud=settings.PINECONE_CLOUD, region=settings.PINECONE_REGION),
            )
        self._index = self.pc.Index(self.index_name)

    @property
    def index(self):
        if self._index is None:
            self.init_index()
        return self._index

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60))
    def _upsert_batch(self, items: list[dict]) -> None:
        self.index.upsert(vectors=items)

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        items = []
        for chunk, vec in zip(chunks, vectors):
            meta: dict[str, Any] = {
                "text": chunk.text[:3500],
                "doc_type": chunk.doc_type,
            }
            for key in ("sku", "model", "source_url", "section"):
                val = getattr(chunk, key)
                if val:
                    meta[key] = str(val)
            if chunk.page is not None:
                meta["page"] = int(chunk.page)
            if chunk.parent_text:
                meta["parent_text"] = chunk.parent_text[:3500]
            items.append({"id": chunk.chunk_id, "values": vec, "metadata": meta})

        for i in range(0, len(items), 100):
            self._upsert_batch(items[i:i + 100])
        log.info("Upserted %s vectors", len(items))
        return len(items)

    def query(self, vector: list[float], top_k: int = 50, metadata_filter: dict | None = None) -> list[dict]:
        res = self.index.query(
            vector=vector, top_k=top_k, include_metadata=True,
            filter=metadata_filter or None,
        )
        return [
            {"id": m["id"], "score": m["score"], **(m.get("metadata") or {})}
            for m in res.get("matches", [])
        ]


class WeaviateStore(VectorStore):
    """Optional Weaviate backend — same interface. Enable with VECTOR_BACKEND=weaviate."""
    COLLECTION = "GenemcoChunk"

    def __init__(self):
        import weaviate
        from weaviate.auth import AuthApiKey
        self.client = weaviate.connect_to_weaviate_cloud(
            cluster_url=settings.WEAVIATE_URL,
            auth_credentials=AuthApiKey(settings.WEAVIATE_API_KEY),
        )

    def init_index(self) -> None:
        import weaviate.classes.config as wc
        if not self.client.collections.exists(self.COLLECTION):
            self.client.collections.create(
                name=self.COLLECTION,
                vectorizer_config=wc.Configure.Vectorizer.none(),
                properties=[
                    wc.Property(name="text", data_type=wc.DataType.TEXT),
                    wc.Property(name="parent_text", data_type=wc.DataType.TEXT),
                    wc.Property(name="sku", data_type=wc.DataType.TEXT),
                    wc.Property(name="model", data_type=wc.DataType.TEXT),
                    wc.Property(name="source_url", data_type=wc.DataType.TEXT),
                    wc.Property(name="section", data_type=wc.DataType.TEXT),
                    wc.Property(name="doc_type", data_type=wc.DataType.TEXT),
                    wc.Property(name="page", data_type=wc.DataType.INT),
                ],
            )

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        col = self.client.collections.get(self.COLLECTION)
        with col.batch.dynamic() as batch:
            for chunk, vec in zip(chunks, vectors):
                batch.add_object(
                    properties={
                        "text": chunk.text, "parent_text": chunk.parent_text or "",
                        "sku": chunk.sku or "", "model": chunk.model or "",
                        "source_url": chunk.source_url or "", "section": chunk.section or "",
                        "doc_type": chunk.doc_type, "page": chunk.page or 0,
                    },
                    vector=vec, uuid=None,
                )
        return len(chunks)

    def query(self, vector: list[float], top_k: int = 50, metadata_filter: dict | None = None) -> list[dict]:
        from weaviate.classes.query import Filter, MetadataQuery
        col = self.client.collections.get(self.COLLECTION)
        wfilter = None
        if metadata_filter:
            conds = [Filter.by_property(k).equal(v) for k, v in metadata_filter.items()]
            wfilter = Filter.all_of(conds) if len(conds) > 1 else conds[0]
        res = col.query.near_vector(
            near_vector=vector, limit=top_k, filters=wfilter,
            return_metadata=MetadataQuery(distance=True),
        )
        out = []
        for o in res.objects:
            out.append({"id": str(o.uuid), "score": 1 - (o.metadata.distance or 0), **o.properties})
        return out


def get_vector_store() -> VectorStore:
    if settings.VECTOR_BACKEND == "weaviate":
        return WeaviateStore()
    return PineconeStore()
