"""
POST /query -- verified answers with page-level citations (locked contract).

    request   {query, model_code?, top_k?}
    response  {answer, verified, confidence, citations[], ungrounded}

This router is mounted by the deployable service (api/query_app.py) and by the
full application (api/main.py). All answering logic lives in
retrieval/verified_query.py; this module only handles transport and access.
"""
import hmac
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from retrieval.verified_query import QueryRequest, QueryResponse, get_engine

log = logging.getLogger("api.query")
router = APIRouter()


def require_upstream_token(x_upstream_token: str | None = Header(default=None)) -> None:
    """
    Shared secret between the public Worker and this service.

    The service is meant to accept traffic only from the Worker, which owns JWT
    auth and public exposure. Network isolation (binding to 127.0.0.1 or a private
    interface) is the primary control; when QUERY_UPSTREAM_TOKEN is set, every
    request must also present it in X-Upstream-Token. Compared in constant time.
    """
    expected = os.getenv("QUERY_UPSTREAM_TOKEN", "")
    if expected and not hmac.compare_digest(x_upstream_token or "", expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/query", response_model=QueryResponse,
             dependencies=[Depends(require_upstream_token)])
def query(req: QueryRequest, response: Response) -> QueryResponse:
    result, reason = get_engine().run(req)
    # Why an answer was withheld is operational detail, not part of the locked
    # body contract -- so it travels in a header the Worker can log.
    response.headers["X-Query-Outcome"] = reason[:120]
    log.info("query outcome=%s verified=%s ungrounded=%s model_code=%s",
             reason, result.verified, result.ungrounded, req.model_code)
    return result
