"""
Kaufland offers API routes
Serves scraped Kaufland.bg offers directly from the kaufland_offers table
"""

from __future__ import annotations
import logging
import traceback
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["kaufland"])


@router.get("/kaufland/offers")
async def get_kaufland_offers(
    q:        str     = Query("",   description="Search term"),
    category: str     = Query("",   description="Filter by category"),
    store:    str     = Query("",   description="Filter by store (kaufland/billa/fantastico/lidl)"),
    limit:    int     = Query(50,   ge=1, le=200),
    offset:   int     = Query(0,    ge=0),
    sort:     str     = Query("price", pattern="^(price|name|discount)$"),
):
    """
    Browse / search current Kaufland offers.
    Used by the Оферти tab on the frontend.
    """
    sb = get_supabase()

    try:
        query = sb.table("kaufland_offers").select("*")

        if q:
            query = query.ilike("raw_name", f"%{q}%")

        if store:
            query = query.eq("store", store)

        if category:
            query = query.ilike("category_raw", f"%{category}%")

        if sort == "name":
            query = query.order("raw_name", desc=False)
        else:
            query = query.order("price", desc=False)

        query = query.limit(limit).offset(offset)

        resp   = query.execute()
        offers = resp.data or []

        # Compute discount_pct safely
        for o in offers:
            try:
                op = float(o.get("old_price") or 0)
                p  = float(o.get("price") or 0)
                o["discount_pct"] = round((op - p) / op * 100, 1) if op > p > 0 else None
            except Exception:
                o["discount_pct"] = None

        return {
            "count":  len(offers),
            "offset": offset,
            "offers": offers,
        }

    except Exception as exc:
        traceback.print_exc()   # ← показва се в uvicorn терминала
        logger.error("[kaufland] offers fetch failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/kaufland/offers/categories")
async def get_kaufland_categories():
    """List of all distinct categories in kaufland_offers."""
    sb = get_supabase()
    try:
        resp = (
            sb.table("kaufland_offers")
            .select("category_raw")
            .execute()
        )
        categories = sorted({
            row["category_raw"]
            for row in (resp.data or [])
            if row.get("category_raw")
        })
        return {"categories": categories}
    except Exception as exc:
        logger.error("[kaufland] categories fetch failed: %s", exc)
        return {"categories": []}


@router.get("/kaufland/offers/deals")
async def get_kaufland_deals(limit: int = Query(20, ge=5, le=50)):
    """
    Kaufland products currently on promotion (have an old_price).
    Sorted by biggest absolute saving.
    """
    sb = get_supabase()
    try:
        resp = (
            sb.table("kaufland_offers")
            .select("*")
            .not_.is_("old_price", "null")
            .order("old_price", desc=True)
            .limit(limit)
            .execute()
        )
        deals = resp.data or []

        for d in deals:
            if d.get("old_price") and d.get("price"):
                d["saving"]       = round(d["old_price"] - d["price"], 2)
                d["discount_pct"] = round((d["old_price"] - d["price"]) / d["old_price"] * 100, 1)

        # Re-sort by saving descending
        deals.sort(key=lambda x: x.get("saving", 0), reverse=True)
        return {"deals": deals}

    except Exception as exc:
        logger.error("[kaufland] deals fetch failed: %s", exc)
        return {"deals": []}


@router.get("/kaufland/offers/stats")
async def get_kaufland_stats():
    """Quick stats: total offers, categories count, last scraped."""
    sb = get_supabase()
    try:
        count_resp  = sb.table("kaufland_offers").select("id", count="exact").execute()
        latest_resp = sb.table("kaufland_offers").select("scraped_at").order("scraped_at", desc=True).limit(1).execute()

        latest_scraped = None
        if latest_resp.data:
            latest_scraped = latest_resp.data[0].get("scraped_at")

        return {
            "total_offers":  count_resp.count or 0,
            "last_scraped":  latest_scraped,
        }
    except Exception as exc:
        logger.error("[kaufland] stats failed: %s", exc)
        return {"total_offers": 0, "last_scraped": None}
