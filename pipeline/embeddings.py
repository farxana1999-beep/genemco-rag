"""
Async, batched, rate-limited embedding generation (SOW Section 5:
'async batching and rate-limit handling for all embedding API calls').
tenacity retries + asyncio-throttle keep Stream C volume safe.
"""
import asyncio

from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.logging_utils import get_logger

log = get_logger("embeddings")


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, min=2, max=90))
async def _embed_batch(client, texts: list[str]) -> list[list[float]]:
    resp = await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed any number of texts with batching + concurrency + throttling."""
    from asyncio_throttle import Throttler
    from openai import AsyncOpenAI

    throttler = Throttler(rate_limit=max(1, int(settings.EMBED_RATE_LIMIT_PER_SEC)), period=1.0)
    sem = asyncio.Semaphore(settings.EMBED_MAX_CONCURRENCY)
    batches = [texts[i:i + settings.EMBED_BATCH_SIZE] for i in range(0, len(texts), settings.EMBED_BATCH_SIZE)]
    results: list[list[list[float]] | None] = [None] * len(batches)

    # Close the client before asyncio.run() tears the loop down. Leaving it to the
    # garbage collector raises "RuntimeError: Event loop is closed" on Windows,
    # which looks like a crash in batch logs even though the embeddings succeeded.
    async with AsyncOpenAI(api_key=settings.OPENAI_API_KEY) as client:

        async def run(idx: int, batch: list[str]):
            async with sem:
                async with throttler:
                    results[idx] = await _embed_batch(client, batch)
                    log.info("Embedded batch %s/%s (%s texts)", idx + 1, len(batches), len(batch))

        await asyncio.gather(*(run(i, b) for i, b in enumerate(batches)))

    flat: list[list[float]] = []
    for r in results:
        flat.extend(r or [])
    return flat


def embed_texts_sync(texts: list[str]) -> list[list[float]]:
    return asyncio.run(embed_texts(texts))
