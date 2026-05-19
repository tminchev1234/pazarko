"""
Price comparison endpoints — compare basket, track history, get deals
"""

from __future__ import annotations
import logging
from typing import List, Optional
from datetime import date, timedelta

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["prices"])


class BasketItem(BaseModel):
    product_id: str
    quantity: float = 1.0


class BasketRequest(BaseModel):
    items: List[BasketItem]
    user_id: Optional[str] = None


class StoreTotals(BaseModel):
    store: str
    total: float
    items_found: int
    items_missing: int


class BasketComparison(BaseModel):
    stores: List[StoreTotals]
    cheapest_store: str
    max_savings: float
    items: int


class PriceHistoryPoint(BaseModel):
    date: str
    price: float
    store: str


@router.post("/basket/compare", response_model=BasketComparison)
async def compare_basket(req: BasketRequest):
    """
    Compare the total cost of a basket across all stores.
    Returns how much the user would pay in each store.
    """
    sb = get_supabase()

    store_totals: dict[str, float] = {}
    store_found: dict[str, int] = {}
    store_missing: dict[str, int] = {}

    stores_seen: set[str] = set()

    for item in req.items:
        prices_resp = (
            sb.table("latest_prices")
            .select("store, price")
            .eq("product_id", item.product_id)
            .execute()
        )
        prices = prices_resp.data or []
        stores_in_item = {p["store"] for p in prices}
        stores_seen.update(stores_in_item)

        for p in prices:
            store = p["store"]
            if store not in store_totals:
                store_totals[store] = 0.0
                store_found[store] = 0
                store_missing[store] = 0
            store_totals[store] += p["price"] * item.quantity
            store_found[store] += 1

        # mark missing for stores that don't have this product
        for store in stores_seen - stores_in_item:
            if store in store_missing:
                store_missing[store] += 1

    result = [
        StoreTotals(
            store=store,
            total=round(total, 2),
            items_found=store_found.get(store, 0),
            items_missing=store_missing.get(store, 0),
        )
        for store, total in store_totals.items()
    ]
    result.sort(key=lambda x: x.total)

    cheapest = result[0].store if result else ""
    most_expensive = result[-1].total if result else 0
    cheapest_total = result[0].total if result else 0
    savings = round(most_expensive - cheapest_total, 2)

    return BasketComparison(
        stores=result,
        cheapest_store=cheapest,
        max_savings=savings,
        items=len(req.items),
    )


@router.get("/prices/history/{product_id}")
async def price_history(
    product_id: str,
    days: int = Query(90, ge=7, le=365),
    store: Optional[str] = None,
):
    """
    Price history for a product over the last N days.
    Used to render the sparkline/trend chart.
    """
    sb = get_supabase()
    since = (date.today() - timedelta(days=days)).isoformat()

    query = (
        sb.table("price_history")
        .select("scraped_at, price, store")
        .eq("product_id", product_id)
        .gte("scraped_at", since)
        .order("scraped_at")
    )
    if store:
        query = query.eq("store", store)

    resp = query.execute()
    history = [
        PriceHistoryPoint(
            date=r["scraped_at"][:10],
            price=r["price"],
            store=r["store"],
        )
        for r in (resp.data or [])
    ]

    return {"product_id": product_id, "days": days, "history": history}


@router.get("/deals")
async def get_deals(
    limit: int = Query(20, ge=5, le=50),
    category: Optional[str] = None,
):
    """
    Products with the biggest price drop vs. 7-day average.
    The 'deals' feed on the home screen.
    """
    sb = get_supabase()

    query = sb.table("price_deals_view").select("*").order("drop_pct", desc=True)
    if category:
        query = query.eq("category", category)

    resp = query.limit(limit).execute()
    return {"deals": resp.data or [], "count": len(resp.data or [])}


@router.get("/stores")
async def list_stores():
    """All stores with metadata."""
    return {
        "stores": [
            {
                "id": "kaufland",
                "name": "Kaufland",
                "logo": "/img/stores/kaufland.png",
                "color": "#E30613",
                "url": "https://www.kaufland.bg",
            },
            {
                "id": "billa",
                "name": "Billa",
                "logo": "/img/stores/billa.png",
                "color": "#FFE200",
                "url": "https://www.billa.bg",
            },
            {
                "id": "fantastico",
                "name": "Фантастико",
                "logo": "/img/stores/fantastico.png",
                "color": "#0066CC",
                "url": "https://fantastico.bg",
            },
            {
                "id": "ebag",
                "name": "eBag",
                "logo": "/img/stores/ebag.png",
                "color": "#FF6B00",
                "url": "https://www.ebag.bg",
            },
        ]
    }
