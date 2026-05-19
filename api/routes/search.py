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

@router.get("/search")
async def search_products(
    q: str = Query(..., min_length=2, description="Search term, e.g. 'мляко', 'кафе'"),
    category: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Search across all scraped offers in kaufland_offers table.
    Groups by product name and shows cheapest price per store.
    """
    try:
        sb = get_supabase()

        # Search in scraped offers — all 5 stores live here
        resp = (
            sb.table("kaufland_offers")
            .select("*")
            .ilike("raw_name", f"%{q}%")
            .limit(min(limit * 10, 500))
            .execute()
        )
        offers = resp.data or []

        if not offers:
            return {"query": q, "results": [], "total": 0}

        # Group by raw_name, keep cheapest offer per store per product name
        from collections import defaultdict
        by_name: dict[str, dict[str, dict]] = defaultdict(dict)
        for o in offers:
            name  = (o.get("raw_name") or "").strip()
            store = o.get("store") or "unknown"
            price = float(o.get("price") or 999)
            if not name:
                continue
            existing = by_name[name].get(store)
            if existing is None or float(existing.get("price") or 999) > price:
                by_name[name][store] = o

        results = []
        for name, by_store in list(by_name.items())[:limit]:
            all_prices = sorted(by_store.values(), key=lambda x: float(x.get("price") or 999))
            best  = all_prices[0]
            worst = all_prices[-1]

            price_entries = [
                {
                    "store":      o["store"],
                    "price":      float(o.get("price") or 0),
                    "unit":       o.get("unit") or "",
                    "url":        o.get("url") or "",
                    "scraped_at": o.get("scraped_at") or "",
                }
                for o in all_prices
            ]

            savings = round(
                float(worst.get("price") or 0) - float(best.get("price") or 0), 2
            ) if len(price_entries) > 1 else 0

            results.append({
                "id":             best.get("id") or name,
                "canonical_name": name,
                "brand":          best.get("brand") or "",
                "volume":         best.get("unit") or "",
                "category":       best.get("category_raw") or "Оферти",
                "image_url":      best.get("image_url") or "",
                "prices":         price_entries,
                "best_price":     float(best.get("price") or 0),
                "best_store":     best.get("store") or "",
                "savings":        savings,
            })

        # Multi-store matches first (savings > 0), then alphabetical
        results.sort(key=lambda x: (-x["savings"], x["canonical_name"]))
        return {"query": q, "results": results, "total": len(results)}

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
