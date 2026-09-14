"""
Deployable verified-query service: GET /health, POST /query.

Sits BEHIND the public Worker. The Worker terminates public traffic and JWT auth
and proxies to this process; this process should not be reachable from the
internet. It needs no OpenAI, Pinecone or Docling -- it answers from golden
records already on disk.

CONFIGURATION (environment variables only -- nothing is hardcoded)
    GENEMCO_API_HOST            bind address                default 127.0.0.1 (local only)
    GENEMCO_API_PORT            bind port                   default 9000
    QUERY_UPSTREAM_TOKEN        shared secret with Worker   optional, strongly recommended
    GENEMCO_GOLDEN_DIR          authoritative golden records (dir of *.json)
    GENEMCO_CATALOG_PATH        catalog search layer (.jsonl file, or dir of *.json)
    GENEMCO_MANUAL_URLS         JSON map {manual filename: hosted url}   optional
    GENEMCO_FORWARDED_ALLOW_IPS proxies trusted for X-Forwarded-*        default 127.0.0.1
    GENEMCO_API_DOCS            "1" to serve /docs and /openapi.json     default 0
    GENEMCO_LOG_LEVEL           default info

RUN
    pip install -r requirements-query.txt
    python -m api.query_app
  or
    uvicorn api.query_app:app --host "$GENEMCO_API_HOST" --port "$GENEMCO_API_PORT"
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api.query import router as query_router
from retrieval.verified_query import get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load and index golden records BEFORE accepting traffic, so the first proxied
    # request is not the one that pays the load time.
    get_engine()
    yield


_docs = os.getenv("GENEMCO_API_DOCS", "0") == "1"
app = FastAPI(
    title="Genemco Verified Query API",
    version="2.0.0",
    description="Verified, citation-backed answers for Genemco equipment. "
                "Answers that cannot be grounded are withheld (ungrounded=true).",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)
app.include_router(query_router)


@app.get("/health")
def health():
    """Liveness + readiness. 503 until golden records are loaded, so a proxy can hold traffic."""
    stats = get_engine().store.stats
    ready = bool(stats.get("loaded") and stats.get("records"))
    return JSONResponse(status_code=200 if ready else 503,
                        content={"status": "ok" if ready else "degraded", "store": stats})


def main() -> None:
    import uvicorn

    uvicorn.run(
        "api.query_app:app",
        host=os.getenv("GENEMCO_API_HOST", "127.0.0.1"),
        port=int(os.getenv("GENEMCO_API_PORT", "9000")),
        log_level=os.getenv("GENEMCO_LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("GENEMCO_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
