"""
Phase 1 — Shopify Ingestion.
Pulls the full product catalog via the Admin REST API (/products.json) with
Link-header pagination, stores raw responses, and normalizes into the
Golden Record's shopify{} identity block.
"""
import json
import re
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.logging_utils import get_logger, log_exception
from core.schemas import ShopifyBlock

log = get_logger("shopify_sync")

PAGE_LIMIT = 250


def _base_url() -> str:
    store = settings.SHOPIFY_STORE_URL.replace("https://", "").strip("/")
    return f"https://{store}/admin/api/{settings.SHOPIFY_API_VERSION}"


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": settings.SHOPIFY_ADMIN_TOKEN,
        "Content-Type": "application/json",
    }


def _next_page_info(link_header: str | None) -> str | None:
    """Parse Shopify's Link header for rel="next" page_info cursor."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"page_info=([^&>]+)", part)
            if m:
                return m.group(1)
    return None


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60))
def _fetch_page(client: httpx.Client, page_info: str | None) -> httpx.Response:
    params = {"limit": PAGE_LIMIT}
    if page_info:
        params["page_info"] = page_info
    resp = client.get(f"{_base_url()}/products.json", params=params, headers=_headers(), timeout=60)
    if resp.status_code == 429:
        # Shopify rate limit — tenacity will back off and retry
        raise httpx.HTTPStatusError("429 rate limited", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp


def sync_all_products(save_raw: bool = True, max_pages: int | None = None) -> list[dict]:
    """
    Fetch every product in the store. Returns list of raw Shopify product dicts.
    max_pages caps the number of pages fetched (smoke tests / cheap dry runs);
    None means fetch the whole catalog.
    """
    products: list[dict] = []
    page_info = None
    page_num = 0
    with httpx.Client() as client:
        while True:
            resp = _fetch_page(client, page_info)
            batch = resp.json().get("products", [])
            products.extend(batch)
            page_num += 1
            log.info("Fetched page %s (%s products, %s total)", page_num, len(batch), len(products))
            if save_raw:
                out = settings.SHOPIFY_RAW_DIR / f"products_page_{page_num:04d}.json"
                out.write_text(json.dumps(batch, indent=2), encoding="utf-8")
            if max_pages is not None and page_num >= max_pages:
                log.info("Stopping at max_pages=%s", max_pages)
                break
            page_info = _next_page_info(resp.headers.get("Link"))
            if not page_info:
                break
    log.info("Shopify sync complete: %s products", len(products))
    return products


# ------------------------------------------------------------------ public fallback
SYNC_MODE_KEY = "_genemco_sync_mode"
PUBLIC_PAGE_LIMIT = 250


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60))
def _fetch_public_page(client: httpx.Client, page: int) -> list[dict]:
    """One page of the unauthenticated storefront feed (page-numbered, not cursored)."""
    resp = client.get(
        f"{settings.PUBLIC_STORE_URL.rstrip('/')}/products.json",
        params={"limit": PUBLIC_PAGE_LIMIT, "page": page},
        timeout=60, follow_redirects=True,
    )
    if resp.status_code == 429:
        raise httpx.HTTPStatusError("429 rate limited", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json().get("products", [])


def sync_public_products(save_raw: bool = True, max_pages: int | None = None) -> list[dict]:
    """
    STOPGAP: fetch the catalog from the public storefront feed — no auth required.

    This is NOT a replacement for the Admin API. The public feed omits Admin-only
    data (real inventory counts, cost, unpublished/draft products) and exposes
    `tags` as a list and variant availability as a bool rather than a count.
    Every product is stamped so downstream records carry
    sync_mode="public_storefront".
    """
    log.warning("PUBLIC STOREFRONT MODE — demo data only, not authoritative Admin API data")
    products: list[dict] = []
    page = 1
    with httpx.Client() as client:
        while True:
            batch = _fetch_public_page(client, page)
            if not batch:
                break
            for p in batch:
                p[SYNC_MODE_KEY] = "public_storefront"
            products.extend(batch)
            log.info("Fetched public page %s (%s products, %s total)", page, len(batch), len(products))
            if save_raw:
                out = settings.SHOPIFY_RAW_DIR / f"products_public_page_{page:04d}.json"
                out.write_text(json.dumps(batch, indent=2), encoding="utf-8")
            if max_pages is not None and page >= max_pages:
                log.info("Stopping at max_pages=%s", max_pages)
                break
            if len(batch) < PUBLIC_PAGE_LIMIT:
                break
            page += 1
    log.info("Public storefront sync complete: %s products", len(products))
    return products


def sync_catalog(save_raw: bool = True, max_pages: int | None = None,
                 public: bool | None = None) -> list[dict]:
    """
    Single entry point that picks the source. Admin API is the default; the public
    storefront is used only when explicitly requested (--public / PUBLIC_CATALOG_MODE).
    """
    use_public = settings.PUBLIC_CATALOG_MODE if public is None else public
    if use_public:
        return sync_public_products(save_raw=save_raw, max_pages=max_pages)
    return sync_all_products(save_raw=save_raw, max_pages=max_pages)


def load_raw_products() -> list[dict]:
    """Reload previously saved raw pages (avoids re-hitting the API during dev).
    Picks up both Admin (`products_page_*`) and public (`products_public_page_*`)
    caches; each record carries its own sync_mode stamp, so a mixed cache is safe."""
    products = []
    for f in sorted(settings.SHOPIFY_RAW_DIR.glob("products_*page_*.json")):
        products.extend(json.loads(f.read_text(encoding="utf-8")))
    return products


def normalize_product(raw: dict) -> tuple[str | None, ShopifyBlock]:
    """
    Map raw Shopify JSON → Golden Record shopify{} block.
    SKU is taken from the first variant SKU; falls back to product handle.
    """
    try:
        variants = raw.get("variants", []) or []
        sku = None
        for v in variants:
            if v.get("sku"):
                sku = str(v["sku"]).strip()
                break
        if not sku:
            sku = raw.get("handle")

        # Admin API reports a real count; the public feed only exposes an `available`
        # bool. Prefer the count when present so Admin behaviour is unchanged.
        if any("inventory_quantity" in v for v in variants):
            inventory_status = "in_stock" if sum(
                int(v.get("inventory_quantity") or 0) for v in variants) > 0 else "out_of_stock"
        elif any("available" in v for v in variants):
            inventory_status = "in_stock" if any(v.get("available") for v in variants) else "out_of_stock"
        else:
            inventory_status = None

        # Admin API returns tags as a comma-separated string; the public feed a list.
        raw_tags = raw.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]

        block = ShopifyBlock(
            product_id=raw.get("id"),
            title=raw.get("title"),
            handle=raw.get("handle"),
            url=f"https://genemco.com/products/{raw.get('handle')}" if raw.get("handle") else None,
            tags=tags,
            vendor=raw.get("vendor"),
            product_type=raw.get("product_type"),
            variants=[
                {
                    "id": v.get("id"),
                    "sku": v.get("sku"),
                    "price": v.get("price"),
                    "inventory_quantity": v.get("inventory_quantity"),
                }
                for v in variants
            ],
            images=[img.get("src") for img in (raw.get("images") or []) if img.get("src")],
            inventory_status=inventory_status,
            sync_mode=raw.get(SYNC_MODE_KEY, "admin_api"),
        )
        return sku, block
    except Exception as e:  # noqa: BLE001
        log_exception(sku=raw.get("handle"), file=None, reason=f"shopify_normalize_error: {e}")
        return None, ShopifyBlock()


def normalize_all(raw_products: list[dict]) -> dict[str, ShopifyBlock]:
    out: dict[str, ShopifyBlock] = {}
    for raw in raw_products:
        sku, block = normalize_product(raw)
        if sku:
            out[sku] = block
        else:
            log_exception(sku=None, file=None, reason="missing_sku", product_id=raw.get("id"))
    log.info("Normalized %s products with SKUs", len(out))
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Shopify catalog sync")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="stop after N pages (smoke test); default = whole catalog")
    ap.add_argument("--no-save-raw", action="store_true", help="do not write data/shopify_raw/")
    ap.add_argument("--public", action="store_true",
                    help="STOPGAP: pull the unauthenticated storefront feed instead of the "
                         "Admin API (demo data only; records stamped public_storefront)")
    args = ap.parse_args()

    prods = sync_catalog(save_raw=not args.no_save_raw, max_pages=args.max_pages,
                         public=True if args.public else None)
    normalized = normalize_all(prods)
    modes = {}
    for b in normalized.values():
        modes[b.sync_mode] = modes.get(b.sync_mode, 0) + 1
    print(f"Synced {len(prods)} products, normalized {len(normalized)} SKUs {modes}")
