"""
Broshura.bg scraper — httpx + BeautifulSoup (no Selenium needed)
Extracts product offers from broshura.bg (powered by Marktjagd/Shopfully)

Supported stores: kaufland, billa, fantastico, lidl, metro
HTML structure (confirmed from live page):
  ul.list-offer > li > a[href^="/p/ID"] = product (skip href^="/b/" = brochure)
    span.title-offer              → product name
    dl.list-product-price dd em   → current price  "0,35 € / 0,68 лв."
    div.has-price-discount dd     → old price       "0,50 € / 0,98 лв."
    figure.component-gallery img  → product image

Usage:
    py -m scrapers.broshura --test          # 1 page, save JSON
    py -m scrapers.broshura                 # all pages
    py -m scrapers.broshura --store billa
"""

from __future__ import annotations
import json
import logging
import re
import time
import random
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://broshura.bg"
HEADERS  = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Referer":         "https://broshura.bg/",
}

STORE_SLUGS = {
    "kaufland":   "kaufland",
    "billa":      "billa",
    "fantastico": "fantastico",
    "lidl":       "lidl",
    "metro":      "metro",
}


# ── Price helpers ──────────────────────────────────────────────────────────────

def _parse_bgn(text: Optional[str]) -> Optional[float]:
    """
    '0,35 € / 0,68 лв.'  →  0.68
    '20,45 € / 40 лв.'   →  40.0   ← integer price (no decimal)
    '0,68 лв.'           →  0.68
    Picks the BGN (лв.) value — the number immediately before 'лв'.
    """
    if not text:
        return None
    # Match integer OR decimal number directly before "лв"
    # e.g. "40 лв." or "58,66 лв." — decimal separator optional
    m = re.search(r"(\d[\d\s]*(?:[,.]\d+)?)\s*лв", text, re.IGNORECASE)
    if m:
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            return round(float(raw), 2)
        except ValueError:
            pass
    return None


# ── Scrape one page ────────────────────────────────────────────────────────────

def _scrape_page(html: str, store: str, scraped_at: str) -> list[dict]:
    soup     = BeautifulSoup(html, "html.parser")
    products = []

    # Every offer row is an <li> inside ul.list-offer
    # Products link to /p/ID, brochures link to /b/ID — skip brochures
    for li in soup.select("li"):
        a = li.select_one("a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("/p/"):
            continue   # skip brochures and nav links

        # ── Name ──
        name_el = li.select_one("span.title-offer")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        # ── Current price (inside <em>) ──
        price_el = li.select_one("dl em")
        price    = _parse_bgn(price_el.get_text(strip=True) if price_el else None)
        if not price:
            # Fallback: any dd text with лв
            for dd in li.select("dd"):
                price = _parse_bgn(dd.get_text(strip=True))
                if price:
                    break
        if not price:
            continue

        # ── Old price ──
        old_el   = li.select_one("div.has-price-discount dd")
        old_price = _parse_bgn(old_el.get_text(strip=True) if old_el else None)
        # Sanity check — old must be > current
        if old_price and old_price <= price:
            old_price = None

        # ── Discount % (calculated) ──
        discount = ""
        if old_price:
            pct = round((old_price - price) / old_price * 100)
            discount = f"-{pct}%"

        # ── Image ──
        img      = li.select_one("figure img") or li.select_one("img")
        image_url = ""
        if img:
            src = img.get("src") or img.get("data-src") or img.get("srcset", "").split()[0]
            if src:
                image_url = src if src.startswith("http") else BASE_URL + src
                # Upgrade thumbnail to larger version on Marktjagd CDN
                image_url = re.sub(r"_(\d+)x(\d+)\.", "_400x400.", image_url)

        # ── Product URL ──
        url = BASE_URL + href if href.startswith("/") else href

        # ── Category (broshura doesn't expose it per-product — use store) ──
        category_raw = store.capitalize() + " оферти"

        products.append({
            "store":        store,
            "raw_name":     name,
            "brand":        "",
            "description":  "",
            "price":        price,
            "old_price":    old_price,
            "discount":     discount,
            "unit":         "",
            "image_url":    image_url,
            "url":          url,
            "category_raw": category_raw,
            "scraped_at":   scraped_at,
        })

    return products


# ── Main function ──────────────────────────────────────────────────────────────

def scrape_broshura(store: str = "kaufland", max_pages: int = 15) -> list[dict]:
    slug       = STORE_SLUGS.get(store, store)
    all_prods: list[dict] = []
    seen:       set[str]  = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}/{slug}" if page == 1 else f"{BASE_URL}/{slug}?page={page}"
            logger.info("[broshura] Fetching: %s", url)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                logger.error("[broshura] Request failed (page %d): %s", page, exc)
                break

            prods = _scrape_page(resp.text, store, scraped_at)

            new = 0
            for p in prods:
                key = f"{p['raw_name']}|{p['price']}"
                if key not in seen:
                    seen.add(key)
                    all_prods.append(p)
                    new += 1

            logger.info("[broshura] Page %d → %d new products (total: %d)", page, new, len(all_prods))

            if new == 0:
                logger.info("[broshura] No new products — done")
                break

            time.sleep(random.uniform(1.5, 2.5))

    logger.info("[broshura] Finished: %d total products", len(all_prods))
    return all_prods


def save_to_supabase(products: list[dict], store: str = "kaufland") -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0
    sb = get_supabase_admin()
    # Delete old entries for this store, insert fresh
    sb.table("kaufland_offers").delete().eq("store", store).execute()
    total = 0
    for i in range(0, len(products), 50):
        sb.table("kaufland_offers").insert(products[i:i+50]).execute()
        total += len(products[i:i+50])
    logger.info("[broshura] Saved %d to Supabase", total)
    return total


def save_to_json(products: list[dict], filename: str = "broshura_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    store       = "kaufland"
    max_pages   = 1 if "--test" in sys.argv else 15
    to_supabase = "--supabase" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--store" and i + 1 < len(sys.argv):
            store = sys.argv[i + 1]

    print(f"Scraping broshura.bg/{store} (max {max_pages} pages)...")
    products = scrape_broshura(store=store, max_pages=max_pages)

    if products:
        fname = f"broshura_{store}.json"
        save_to_json(products, fname)
        print(f"\n✅ Scraped {len(products)} products")
        print("\nSample:")
        for p in products[:8]:
            old = f"  (беше {p['old_price']:.2f} лв.)" if p.get("old_price") else ""
            disc = f"  {p['discount']}" if p.get("discount") else ""
            print(f"  {p['raw_name']:<50} {p['price']:.2f} лв.{old}{disc}")
        if to_supabase:
            print("\nUploading to Supabase...")
            saved = save_to_supabase(products, store=store)
            print(f"✅ Saved {saved} products to Supabase")
    else:
        print("❌ No products found")
