"""
naoferta.py — използва публичното API на api.naoferta.net
Покрива: Billa, Kaufland, Fantastico, Lidl, T-Market, Metro и още

API документация: https://api.naoferta.net/swagger-ui.html
GitHub: https://github.com/StefanBratanov/sofia-supermarkets-api

Употреба:
    py -m scrapers.naoferta                 # само промоции, save JSON
    py -m scrapers.naoferta --all           # всички продукти
    py -m scrapers.naoferta --supabase      # + upload в Supabase
"""

from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE    = "https://api.naoferta.net"
HEADERS     = {
    "User-Agent": "Mozilla/5.0 (compatible; Pazarko/1.0)",
    "Accept":     "application/json",
}

# Съответствие: API supermarket name → store id в нашата БД
STORE_MAP = {
    "Billa":       "billa",
    "Kaufland":    "kaufland",
    "Fantastico":  "fantastico",
    "Lidl":        "lidl",
    "T-Market":    "tmarket",
    "METRO":       "metro",
    "Kam Market":  "kammarket",
    "CBA":         "cba",
    "ProMarket":   "promarket",
    "Hit Max":     "hitmax",
}


def _clean_name(name: Optional[str]) -> str:
    """Премахва водещото '- ' в имената."""
    if not name:
        return ""
    return re.sub(r"^[\-–—]\s*", "", name).strip()


def _fetch_products(offers_only: bool = True) -> list[dict]:
    """Взима продуктите от API-то и ги нормализира."""
    url    = f"{API_BASE}/products"
    params = {"offers": "true"} if offers_only else {}

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    scraped_at = datetime.now(timezone.utc).isoformat()
    products   = []

    for store_block in data:
        api_name = store_block.get("supermarket", "")
        store_id = STORE_MAP.get(api_name, api_name.lower().replace(" ", "_"))
        raw_list = store_block.get("products", [])

        logger.info("[naoferta] %s → %d products", api_name, len(raw_list))

        for p in raw_list:
            name  = _clean_name(p.get("name"))
            price = p.get("price")
            if not name or not price:
                continue

            old_price = p.get("oldPrice")
            discount  = p.get("discount")

            # Изчисли % ако не е даден
            if old_price and price and not discount and old_price > price:
                discount = round((old_price - price) / old_price * 100)

            products.append({
                "store":        store_id,
                "raw_name":     name,
                "brand":        name.split()[0] if name else "",
                "description":  "",
                "price":        round(float(price), 2),
                "old_price":    round(float(old_price), 2) if old_price else None,
                "discount":     f"-{discount}%" if discount else "",
                "unit":         p.get("quantity") or "",
                "image_url":    p.get("picUrl") or "",
                "url":          "",
                "category_raw": p.get("category") or f"{api_name} оферти",
                "scraped_at":   scraped_at,
            })

    logger.info("[naoferta] Total: %d products from %d stores",
                len(products), len(data))
    return products


def scrape_naoferta(offers_only: bool = True) -> list[dict]:
    return _fetch_products(offers_only=offers_only)


def save_to_supabase(products: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0

    sb = get_supabase_admin()

    # Изтрий старите данни само за магазините, за които имаме нови
    stores = list({p["store"] for p in products})
    for store in stores:
        sb.table("kaufland_offers").delete().eq("store", store).execute()
        logger.info("[naoferta] Deleted old data for %s", store)

    # Вмъкни новите на групи от 50
    total = 0
    for i in range(0, len(products), 50):
        batch = products[i:i+50]
        sb.table("kaufland_offers").insert(batch).execute()
        total += len(batch)

    logger.info("[naoferta] Saved %d products total", total)
    return total


def save_to_json(products: list[dict], filename: str = "naoferta_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    offers_only = "--all"      not in sys.argv
    to_supabase = "--supabase" in sys.argv

    print(f"Fetching from api.naoferta.net ({'промоции' if offers_only else 'всички'})...")
    products = scrape_naoferta(offers_only=offers_only)

    if products:
        save_to_json(products)
        print(f"\n✅ {len(products)} продукта")
        print("\nПо магазин:")

        by_store: dict[str, list] = {}
        for p in products:
            by_store.setdefault(p["store"], []).append(p)
        for store, prods in sorted(by_store.items()):
            print(f"  {store:<15} {len(prods):>4} продукта")

        print("\nПримери:")
        for p in products[:5]:
            old = f"  (беше {p['old_price']:.2f} лв.)" if p.get("old_price") else ""
            print(f"  [{p['store']}] {p['raw_name']:<45} {p['price']:.2f} лв.{old}")

        if to_supabase:
            print("\nКачвам в Supabase...")
            saved = save_to_supabase(products)
            print(f"✅ Записани {saved} продукта")
    else:
        print("❌ Няма продукти")
