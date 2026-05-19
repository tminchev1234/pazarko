"""
Inflation tracker — Pazarko's viral PR feature
Tracks real food basket price change month-over-month, year-over-year
"""

from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["inflation"])

# The "standard basket" — 30 everyday products tracked weekly
# These are canonical product IDs once DB is populated
STANDARD_BASKET_TAGS = [
    "мляко_1л",
    "хляб_700г",
    "яйца_10бр",
    "кисело_мляко_400г",
    "масло_125г",
    "сирене_400г",
    "кашкавал_200г",
    "захар_1кг",
    "брашно_1кг",
    "олио_1л",
    "ориз_1кг",
    "пиле_цяло_1кг",
    "кайма_500г",
    "краставици_1кг",
    "домати_1кг",
    "картофи_1кг",
    "лук_1кг",
    "ябълки_1кг",
    "банани_1кг",
    "портокали_1кг",
    "паста_400г",
    "домати_консерва_400г",
    "кафе_200г",
    "чай_20пак",
    "шампоан_400мл",
    "паста_за_зъби",
    "тоалетна_хартия_4рол",
    "перилен_1кг",
    "детска_формула_400г",
    "детска_пюре_125г",
]


@router.get("/inflation/thermometer")
async def get_inflation_thermometer():
    """
    The 'inflation thermometer' — how much has the standard basket changed?
    Returns: current basket cost, MoM%, YoY%, trend (hot/warm/cool/cold)
    Used for the big hero number on the home page.
    """
    sb = get_supabase()

    today = date.today()
    one_month_ago = (today - timedelta(days=30)).isoformat()
    one_year_ago  = (today - timedelta(days=365)).isoformat()
    today_str      = today.isoformat()

    try:
        resp = sb.rpc("get_basket_inflation", {
            "tags": STANDARD_BASKET_TAGS,
            "date_current": today_str,
            "date_month_ago": one_month_ago,
            "date_year_ago": one_year_ago,
        }).execute()

        data = resp.data or {}
        current  = data.get("current_total", 0)
        prev_m   = data.get("month_ago_total", 0)
        prev_y   = data.get("year_ago_total", 0)

        mom_pct = round(((current - prev_m) / prev_m * 100) if prev_m else 0, 1)
        yoy_pct = round(((current - prev_y) / prev_y * 100) if prev_y else 0, 1)

        # Thermometer color / mood
        if yoy_pct > 8:
            trend = "hot"       # 🔴 serious inflation
        elif yoy_pct > 4:
            trend = "warm"      # 🟠 moderate
        elif yoy_pct > 0:
            trend = "neutral"   # 🟡 slight
        elif yoy_pct > -2:
            trend = "cool"      # 🟢 stable / mild deflation
        else:
            trend = "cold"      # 🔵 deflation

        return {
            "basket_items": len(STANDARD_BASKET_TAGS),
            "current_total": round(current, 2),
            "month_ago_total": round(prev_m, 2),
            "year_ago_total": round(prev_y, 2),
            "mom_pct": mom_pct,
            "yoy_pct": yoy_pct,
            "trend": trend,
            "trend_label": {
                "hot": "Сериозна инфлация",
                "warm": "Умерена инфлация",
                "neutral": "Стабилни цени",
                "cool": "Леко поевтиняване",
                "cold": "Дефлация",
            }[trend],
        }

    except Exception as exc:
        logger.warning("[inflation] rpc failed, returning stub: %s", exc)
        # Return stub until DB has real data
        return {
            "basket_items": 30,
            "current_total": 0,
            "month_ago_total": 0,
            "year_ago_total": 0,
            "mom_pct": 0,
            "yoy_pct": 0,
            "trend": "neutral",
            "trend_label": "Събираме данни...",
            "stub": True,
        }


@router.get("/inflation/history")
async def get_inflation_history(
    months: int = Query(12, ge=3, le=24),
):
    """Monthly basket cost history for the trend chart."""
    sb = get_supabase()
    resp = sb.rpc("get_basket_monthly_history", {
        "tags": STANDARD_BASKET_TAGS,
        "months": months,
    }).execute()

    return {"months": months, "history": resp.data or []}


@router.get("/inflation/by-category")
async def inflation_by_category():
    """
    YoY price change broken down by category.
    'Млечни продукти +12%', 'Месо +8%', 'Зеленчуци -2%' etc.
    """
    sb = get_supabase()
    resp = sb.rpc("get_inflation_by_category", {}).execute()
    return {"categories": resp.data or []}


@router.get("/inflation/biggest-movers")
async def biggest_movers(
    direction: str = Query("up", regex="^(up|down)$"),
    limit: int = Query(10, ge=5, le=20),
):
    """
    Products with the biggest price change MoM.
    direction='up' → most expensive now, direction='down' → biggest discounts.
    """
    sb = get_supabase()
    resp = sb.rpc("get_biggest_movers", {
        "direction": direction,
        "limit_n": limit,
    }).execute()
    return {"direction": direction, "movers": resp.data or []}
