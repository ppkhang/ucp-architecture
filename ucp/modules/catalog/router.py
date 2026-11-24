"""Catalog (UC-01): live reads through the platform adapter. The platform owns price and stock (ADR-006)."""
from fastapi import APIRouter, Depends, Query, Request

from ucp.adapters import get_adapter
from ucp.modules.identity.router import current_agent

router = APIRouter(prefix="/v1/products", dependencies=[Depends(current_agent)])  # every agent has read_catalog
# shortcut: no short cache yet; add a Redis cache with a few seconds TTL when load needs it

SUMMARY = ("product_id", "platform_id", "name", "selling_price", "currency", "rating", "availability_type")


def _adapter(request: Request, platform_id: str):
    return get_adapter(platform_id, request.app.state.platform_http)


@router.get("")
def search(request: Request, platform_id: str, q: str = "", category: str | None = None,
           max_price: int | None = Query(None, ge=0), page: int = Query(1, ge=1),
           limit: int = Query(20, ge=1, le=100)) -> dict:
    found = _adapter(request, platform_id).search_products(q)
    if category is not None:
        found = [p for p in found if p.get("category") == category]  # the mock shop has no categories
    if max_price is not None:
        found = [p for p in found if p["selling_price"] <= max_price]
    start = (page - 1) * limit
    return {"products": [{k: p[k] for k in SUMMARY} for p in found[start:start + limit]],
            "total_count": len(found), "page": page, "partial": False}  # one platform per call, so never partial


@router.get("/{product_id}")
def product(request: Request, product_id: str, platform_id: str) -> dict:
    p = _adapter(request, platform_id).get_product(product_id)
    return {k: v for k, v in p.items() if k != "available_qty"}


@router.get("/{product_id}/price")
def price(request: Request, product_id: str, platform_id: str) -> dict:
    p = _adapter(request, platform_id).get_product(product_id)
    return {"list_price": p["selling_price"], "selling_price": p["selling_price"], "currency": p["currency"]}


@router.get("/{product_id}/stock")
def stock(request: Request, product_id: str, platform_id: str) -> dict:
    p = _adapter(request, platform_id).get_product(product_id)
    # display only: the platform decides stock at checkout
    return {"available_qty": p["available_qty"], "availability_type": p["availability_type"], "max_per_user": None}
