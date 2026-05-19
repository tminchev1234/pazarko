"""
Product search endpoint — returns best price per store for a query
"""

from __future__ import annotations
import logging
from typing import List, Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


# ── response models ──────────────────────────────────────────────────────────

class PriceEntry(BaseModel):
    store: str
    price: float
    unit: Optional[str]
    url: Optional[str]
    scraped_at: str


class ProductResult(BaseModel):
    id: str
    canonical_name: str
    brand: Optional[str]
    volume: Optional[str]
    category: str
    image_url: Optional[str]
    prices: List[PriceEntry]
    best_price: float
    best_store: str
    savings: float          # difference between most expensive and cheapest


class SearchResponse(BaseModel):
    query: str
    results: List[ProductResult]
    total: int


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_result(product: dict, prices: list[dict]) -> ProductResult:
    sorted_prices = sorted(prices, key=lambda p: p["price"])
    best = sorted_prices[0] if sorted_prices else None
    worst = sorted_prices[-1] if sorted_prices else None

    return ProductResult(
        id=product["id"],
        canonical_name=product["canonical_name"],
        brand=product.get("brand"),
        volume=product.get("volume"),
        category=product.get("category", "Общи"),
        image_url=product.get("image_url"),
        prices=[
            PriceEntry(
                store=p["store"],
                price=p["price"],
                unit=p.get("unit"),
                url=p.get("url"),
                scraped_at=p["scraped_at"],
            )
            for p in sorted_prices
        ],
        best_price=best["price"] if best else 0,
        best_store=best["store"] if best else "",
        savings=round((worst["price"] - best["price"]), 2) if worst and best else 0,
    )


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/search", response_model=SearchResponse)
async def search_products(
    q: str = Query(..., min_length=2, description="Search term, e.g. 'мляко', 'кофе'"),
    category: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Full-text search across canonical product names.
    Returns matching products with best price from each store.
    """
    try:
        sb = get_supabase()

        # search products table using ilike on canonical_name + brand
        query = (
            sb.table("products")
            .select("*")
            .ilike("canonical_name", f"%{q}%")
        )
        if category:
            query = query.eq("category", category)

        resp = query.limit(limit).execute()
        products = resp.data or []

        if not products:
            # try brand fallback
            resp2 = (
                sb.table("products")
                .select("*")
                .ilike("brand", f"%{q}%")
                .limit(limit)
                .execute()
            )
            products = resp2.data or []

        results: list[ProductResult] = []
        for prod in products:
            prices_resp = (
                sb.table("latest_prices")   # view — latest price per product/store
                .select("*")
                .eq("product_id", prod["id"])
                .execute()
            )
            prices = prices_resp.data or []
            if prices:
                results.append(_build_result(prod, prices))

        return SearchResponse(query=q, results=results, total=len(results))

    except Exception as exc:
        logger.error("[search] error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Грешка при търсене")


@router.get("/product/{product_id}", response_model=ProductResult)
async def get_product(product_id: str):
    """Single product detail with full price history."""
    sb = get_supabase()
    prod_resp = sb.table("products").select("*").eq("id", product_id).single().execute()
    if not prod_resp.data:
        raise HTTPException(status_code=404, detail="Продуктът не е намерен")

    prices_resp = (
        sb.table("latest_prices")
        .select("*")
        .eq("product_id", product_id)
        .execute()
    )
    return _build_result(prod_resp.data, prices_resp.data or [])


@router.get("/categories")
async def list_categories():
    """All available product categories."""
    sb = get_supabase()
    resp = sb.table("products").select("category").execute()
    cats = sorted({r["category"] for r in resp.data if r.get("category")})
    return {"categories": cats}
