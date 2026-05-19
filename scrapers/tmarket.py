"""
T-Market Online scraper — httpx + BeautifulSoup
Site: https://tmarketonline.bg/selection/produkti-v-akciya

Статичен HTML — не е нужен Selenium.
Цените са в EUR; конвертираме в BGN (курс: 1 EUR = 1.9558 лв.)

Употреба:
    py -m scrapers.tmarket               # scrape + save JSON
    py -m scrapers.tmarket --supabase    # + upload
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
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL   = "https://tmarketonline.bg"
OFFERS_URL = f"{BASE_URL}/selection/produkti-v-akciya"
EUR_TO_BGN = 1.9558   # фиксиран курс EUR → BGN

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Referer":         BASE_URL,
}


def _parse_price(text: Optional[str]) -> Optional[float]:
    """
    '0,14 €'   → 0.27 лв. (конвертирано)
    '0,27 лв.' → 0.27
    '1,29лв.'  → 1.29
    """
    if not text:
        return None
    text = text.strip()

    # Опит за BGN директно
    m_bgn = re.search(r"([\d]+[,.][\d]+)\s*лв", text, re.IGNORECASE)
    if m_bgn:
        try:
            return round(float(m_bgn.group(1).replace(",", ".")), 2)
        except ValueError:
            pass

    # Конвертиране от EUR
    m_eur = re.search(r"([\d]+[,.][\d]+)\s*€", text, re.IGNORECASE)
    if m_eur:
        try:
            eur = float(m_eur.group(1).replace(",", "."))
            return round(eur * EUR_TO_BGN, 2)
        except ValueError:
            pass

    return None


def _parse_page(html: str, scraped_at: str) -> list[dict]:
    """
    CloudCart структура на T-Market:
      div._product-info
        div._product-name
          h3._product-name-tag
            a[href="/product/..."] → ime
        span._product-price-compare  → "0,18 €0,35 лв." (стара combined)
        span.bgn2eur-secondary-currency → "0,35 лв."  (цена BGN)
        span.bgn2eur-secondary-currency → "0,47 лв."  (стара BGN)
    """
    soup     = BeautifulSoup(html, "html.parser")
    products = []

    for h3 in soup.select("h3._product-name-tag"):
        link_el = h3.select_one("a[href*='/product/']")
        if not link_el:
            continue

        name = h3.get_text(strip=True)
        url  = link_el.get("href", OFFERS_URL)
        if url and not url.startswith("http"):
            url = BASE_URL + url

        # Намираме _product-info — 2 нива нагоре от h3
        info_div = h3.parent  # _product-name
        if info_div:
            info_div = info_div.parent  # _product-info

        if not info_div:
            continue

        # BGN цени от span.bgn2eur-secondary-currency
        bgn_spans = info_div.select("span.bgn2eur-secondary-currency")
        price     = _parse_price(bgn_spans[0].get_text(strip=True)) if len(bgn_spans) >= 1 else None
        old_price = _parse_price(bgn_spans[1].get_text(strip=True)) if len(bgn_spans) >= 2 else None

        # Ако нямаме BGN — fallback към EUR конверсия
        if not price:
            compare_el = info_div.select_one("span._product-price-compare")
            if compare_el:
                price = _parse_price(compare_el.get_text(strip=True))

        if not price:
            continue

        # Снимка — търсим в parent на info_div
        image_url = ""
        card_div  = info_div.parent
        if card_div:
            img_el = card_div.select_one("img[src]")
            if img_el:
                image_url = img_el.get("src", "")

        # Отстъпка
        discount = ""
        if old_price and old_price > price:
            pct      = round((old_price - price) / old_price * 100)
            discount = f"-{pct}%"
        else:
            # Търсим в HTML за % badge
            badge = info_div.select_one("[class*='discount'], [class*='badge'], [class*='percent']")
            if badge:
                m = re.search(r"(\d+[.,]?\d*)\s*%", badge.get_text())
                if m:
                    discount = f"-{m.group(1)}%"

        # Количество (от името)
        unit_m = re.search(r"\b(\d+[\.,]?\d*\s*(?:г|кг|мл|л|бр|пак|рол))\b", name, re.IGNORECASE)
        unit   = unit_m.group(1) if unit_m else ""

        products.append({
            "store":        "tmarket",
            "raw_name":     name,
            "brand":        name.split()[0] if name else "",
            "description":  "",
            "price":        price,
            "old_price":    old_price if old_price and old_price > price else None,
            "discount":     discount,
            "unit":         unit,
            "image_url":    image_url,
            "url":          url,
            "category_raw": "T-Market оферти",
            "scraped_at":   scraped_at,
        })

    return products


def scrape_tmarket(max_pages: int = 10) -> list[dict]:
    all_products: list[dict] = []
    seen:         set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        for page in range(1, max_pages + 1):
            url = OFFERS_URL if page == 1 else f"{OFFERS_URL}?page={page}"
            logger.info("[tmarket] Fetching page %d: %s", page, url)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                logger.error("[tmarket] Fetch error page %d: %s", page, exc)
                break

            prods = _parse_page(resp.text, scraped_at)
            new   = 0
            for p in prods:
                key = f"{p['raw_name']}|{p['price']}"
                if key not in seen:
                    seen.add(key)
                    all_products.append(p)
                    new += 1

            logger.info("[tmarket] Page %d → %d new (total: %d)", page, new, len(all_products))

            if new == 0:
                logger.info("[tmarket] No new products — stopping")
                break

            time.sleep(random.uniform(1.5, 2.5))

    logger.info("[tmarket] Done: %d products", len(all_products))
    return all_products


def save_to_supabase(products: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0
    sb = get_supabase_admin()
    sb.table("kaufland_offers").delete().eq("store", "tmarket").execute()
    total = 0
    for i in range(0, len(products), 50):
        sb.table("kaufland_offers").insert(products[i:i+50]).execute()
        total += len(products[i:i+50])
    logger.info("[tmarket] Saved %d products", total)
    return total


def save_to_json(products: list[dict], filename: str = "tmarket_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    to_supabase = "--supabase" in sys.argv

    print("Scraping T-Market Online promotions...")
    products = scrape_tmarket()

    if products:
        save_to_json(products)
        print(f"\n✅ {len(products)} продукта")
        for p in products[:5]:
            old = f"  (беше {p['old_price']:.2f} лв.)" if p.get("old_price") else ""
            print(f"  {p['raw_name']:<50} {p['price']:.2f} лв.{old}")
        if to_supabase:
            saved = save_to_supabase(products)
            print(f"✅ Записани {saved} в Supabase")
    else:
        print("❌ Няма продукти — провери селекторите")
