"""
User memory — Shopping DNA
Tracks spending habits, preferences, savings over time
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


# ── models ────────────────────────────────────────────────────────────────────

class ShoppingDNA(BaseModel):
    user_id: str
    # spending personality
    price_sensitivity: float        # 0-1: 0=premium buyer, 1=always cheapest
    brand_loyalty: Dict[str, float] # brand → score 0-1
    # dietary / lifestyle
    dietary_tags: List[str]         # ["vegetarian","lactose_free","bio","halal"]
    preferred_stores: List[str]     # ordered by preference
    # financial tracking
    total_saved: float              # total EUR saved vs buying at most expensive
    searches_count: int
    # favorite categories
    top_categories: List[str]


class SaveSearchRequest(BaseModel):
    user_id: str
    query: str
    results_count: int
    selected_product_id: Optional[str] = None


class UpdatePreferencesRequest(BaseModel):
    user_id: str
    dietary_tags: Optional[List[str]] = None
    preferred_stores: Optional[List[str]] = None


class RecordSavingRequest(BaseModel):
    user_id: str
    product_id: str
    chosen_store: str
    chosen_price: float
    max_price: float        # most expensive option = potential spend


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}/dna", response_model=ShoppingDNA)
async def get_shopping_dna(user_id: str):
    """Return the Shopping DNA profile for a user."""
    sb = get_supabase()
    resp = sb.table("user_dna").select("*").eq("user_id", user_id).single().execute()

    if not resp.data:
        # Return empty DNA for new user
        return ShoppingDNA(
            user_id=user_id,
            price_sensitivity=0.5,
            brand_loyalty={},
            dietary_tags=[],
            preferred_stores=[],
            total_saved=0.0,
            searches_count=0,
            top_categories=[],
        )

    d = resp.data
    return ShoppingDNA(
        user_id=user_id,
        price_sensitivity=d.get("price_sensitivity", 0.5),
        brand_loyalty=d.get("brand_loyalty", {}),
        dietary_tags=d.get("dietary_tags", []),
        preferred_stores=d.get("preferred_stores", []),
        total_saved=d.get("total_saved", 0.0),
        searches_count=d.get("searches_count", 0),
        top_categories=d.get("top_categories", []),
    )


@router.post("/users/record-search")
async def record_search(req: SaveSearchRequest):
    """
    Log a search event — used to build habit profile.
    Updates searches_count in user_dna.
    """
    sb = get_supabase()

    # insert search log
    sb.table("search_logs").insert({
        "user_id": req.user_id,
        "query": req.query,
        "results_count": req.results_count,
        "selected_product_id": req.selected_product_id,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    # bump counter
    sb.rpc("increment_search_count", {"uid": req.user_id}).execute()

    return {"ok": True}


@router.post("/users/record-saving")
async def record_saving(req: RecordSavingRequest):
    """
    Record that user chose cheapest option.
    Adds saved amount to total_saved in user_dna.
    """
    saved = round(req.max_price - req.chosen_price, 2)
    if saved <= 0:
        return {"ok": True, "saved": 0}

    sb = get_supabase()
    sb.rpc("add_saving", {"uid": req.user_id, "amount": saved}).execute()

    # also update price_sensitivity score (user is price-sensitive if they pick cheapest)
    sb.rpc("update_price_sensitivity", {
        "uid": req.user_id,
        "chose_cheapest": True,
    }).execute()

    return {"ok": True, "saved": saved}


@router.post("/users/preferences")
async def update_preferences(req: UpdatePreferencesRequest):
    """Update dietary tags and store preferences."""
    sb = get_supabase()

    update: Dict[str, Any] = {}
    if req.dietary_tags is not None:
        update["dietary_tags"] = req.dietary_tags
    if req.preferred_stores is not None:
        update["preferred_stores"] = req.preferred_stores

    if update:
        sb.table("user_dna").upsert({
            "user_id": req.user_id,
            **update,
        }).execute()

    return {"ok": True}


@router.get("/users/{user_id}/savings-history")
async def savings_history(user_id: str, limit: int = 20):
    """Recent savings history — 'Спести 1.20 лв при Kaufland'."""
    sb = get_supabase()
    resp = (
        sb.table("savings_log")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"history": resp.data or []}
