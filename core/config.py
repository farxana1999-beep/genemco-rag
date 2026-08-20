"""
Central configuration — everything is read from .env.
Copy .env.example to .env and fill in your credentials.
"""
import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from project root
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class Settings:
    # ---- Shopify ----
    SHOPIFY_STORE_URL: str = os.getenv("SHOPIFY_STORE_URL", "")            # e.g. genemco.myshopify.com
    SHOPIFY_ADMIN_TOKEN: str = os.getenv("SHOPIFY_ADMIN_TOKEN", "")
    SHOPIFY_API_VERSION: str = os.getenv("SHOPIFY_API_VERSION", "2024-01")

    # ---- Public storefront fallback (demo/stopgap only) ----
    # Pulls the unauthenticated /products.json feed when the Admin API token is
    # unavailable. Records are stamped sync_mode="public_storefront" so they stay
    # distinguishable from authoritative Admin API data. Admin API is the default.
    PUBLIC_CATALOG_MODE: bool = os.getenv("PUBLIC_CATALOG_MODE", "false").strip().lower() in {"1", "true", "yes"}
    PUBLIC_STORE_URL: str = os.getenv("PUBLIC_STORE_URL", "https://genemco.com")

    # ---- LLM / Embeddings ----
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    VISION_PROVIDER: str = os.getenv("VISION_PROVIDER", "openai")          # "openai" | "anthropic"
    VISION_MODEL: str = os.getenv("VISION_MODEL", "gpt-4o")               # or claude model id
    FAQ_MODEL: str = os.getenv("FAQ_MODEL", "gpt-4o-mini")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    EMBEDDING_DIM: int = int(os.getenv("EMBEDDING_DIM", "3072"))

    # ---- Vector DB ----
    VECTOR_BACKEND: str = os.getenv("VECTOR_BACKEND", "pinecone")          # "pinecone" | "weaviate"
    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY", "")
    PINECONE_INDEX: str = os.getenv("PINECONE_INDEX", "genemco-rag")
    PINECONE_CLOUD: str = os.getenv("PINECONE_CLOUD", "aws")
    PINECONE_REGION: str = os.getenv("PINECONE_REGION", "us-east-1")
    WEAVIATE_URL: str = os.getenv("WEAVIATE_URL", "")
    WEAVIATE_API_KEY: str = os.getenv("WEAVIATE_API_KEY", "")

    # ---- Reranker ----
    RERANKER_BACKEND: str = os.getenv("RERANKER_BACKEND", "cross-encoder") # "cross-encoder" | "cohere" | "none"
    CROSS_ENCODER_MODEL: str = os.getenv("CROSS_ENCODER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    COHERE_API_KEY: str = os.getenv("COHERE_API_KEY", "")

    # ---- Pipeline behaviour ----
    NAMEPLATE_CONFIDENCE_THRESHOLD: float = float(os.getenv("NAMEPLATE_CONFIDENCE_THRESHOLD", "0.7"))
    NUMERIC_CONFLICT_TOLERANCE_PCT: float = float(os.getenv("NUMERIC_CONFLICT_TOLERANCE_PCT", "5.0"))
    # Reference grids (e.g. a 43-row pressure-transducer conversion table) are not
    # per-model specs; anything larger than this is skipped and logged.
    PDF_MAX_TABLE_ROWS: int = int(os.getenv("PDF_MAX_TABLE_ROWS", "40"))
    EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE", "96"))
    EMBED_MAX_CONCURRENCY: int = int(os.getenv("EMBED_MAX_CONCURRENCY", "4"))
    EMBED_RATE_LIMIT_PER_SEC: float = float(os.getenv("EMBED_RATE_LIMIT_PER_SEC", "2.0"))
    STREAM_C_MAX_PAIRS: int = int(os.getenv("STREAM_C_MAX_PAIRS", "15000"))

    # ---- Paths ----
    DATA_DIR: Path = ROOT_DIR / "data"
    SHOPIFY_RAW_DIR: Path = DATA_DIR / "shopify_raw"
    PDF_DIR: Path = Path(os.getenv("PDF_MANUALS_DIR", str(DATA_DIR / "pdf_manuals")))
    NAMEPLATE_DIR: Path = Path(os.getenv("NAMEPLATE_DIR", str(DATA_DIR / "nameplates")))
    STREAM_C_DIR: Path = Path(os.getenv("STREAM_C_DIR", str(DATA_DIR / "stream_c")))
    GOLDEN_DIR: Path = DATA_DIR / "golden_records"
    LOG_DIR: Path = ROOT_DIR / "logs"
    EXCEPTIONS_LOG: Path = LOG_DIR / "exceptions.jsonl"
    CONFLICTS_LOG: Path = LOG_DIR / "conflicts.jsonl"
    REVIEW_QUEUE: Path = LOG_DIR / "review_queue.jsonl"
    DELAY_LOG: Path = LOG_DIR / "delay_log.jsonl"
    ADAPTABILITY_LOG: Path = LOG_DIR / "adaptability_hours.jsonl"


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    s.LOG_DIR.mkdir(parents=True, exist_ok=True)
    s.GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    s.SHOPIFY_RAW_DIR.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
