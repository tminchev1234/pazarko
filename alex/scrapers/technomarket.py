"""
Technomarket Bulgaria scraper — requests + BeautifulSoup
Site: https://www.technomarket.bg

Употреба:
  py -m alex.scrapers.technomarket                 # scrape + JSON
  py -m alex.scrapers.technomarket --supabase      # + Supabase
  py -m alex.scrapers.technomarket --test          # 1 категория, 2 стр.
"""

from __future__ import annotations
import json
import logging
import re
import time
import random
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.technomarket.bg"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.technomarket.bg/",
}

TECHNOMARKET_CATEGORIES = [
    ("/produkti/laptopi",             "laptops",     "Лаптопи"),
    ("/produkti/telefoni",            "phones",      "Телефони"),
    ("/produkti/slushalki",           "headphones",  "Слушалки"),
    ("/produkti/televizori",          "tvs",         "Телевизори"),
    ("/produkti/tableti",             "tablets",     "Таблети"),
    ("/produkti/fotoaparati",         "cameras",     "Фотоапарати"),
    ("/produkti/gaming",              "gaming",      "Гейминг"),
    ("/produkti/domakinski",          "appliances",  "Домакински уреди"),
    ("/produkti/aksesoari",           "accessories", "Аксесоари"),
    # ── Бяла техника ─────────────────────────────────────────────────
    ("/produkti/hladilnici",          "fridges",     "Хладилници"),
    ("/produkti/peralni",             "washing",     "Перални машини"),
    ("/produkti/sushilni",            "washing",     "Сушилни"),
    ("/produkti/klimatici",           "ac",          "Климатици"),
    ("/produkti/prahosmukachki",      "vacuum",      "Прахосмукачки"),
    ("/produkti/gotvarki",            "cooking",     "Готварки"),
    ("/produkti/mikrovalni",          "cooking",     "Микровълнови"),
    ("/produkti/sudomialni",          "dishwasher",  "Съдомиялни"),
]


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = text.strip()
    cleaned = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
    cleaned = re.sub(r'[^\d,.]', '', cleaned)
    if not cleaned:
        return None
    # Thousands comma: ",NNN" followed by ".", "," or end-of-string → remove comma
    cleaned = re.sub(r',(\d{3})(?=[,.]|$)', r'\1', cleaned)
    cleaned = cleaned.replace(',', '.')
    parts = cleaned.split('.')
    if len(parts) >= 2:
        cleaned = ''.join(parts[:-1]) + '.' + parts[-1][:2]
    try:
        return round(float(cleaned), 2) if cleaned else None
    except ValueError:
        return None


def _discount(price, old):
    if price and old and old > price > 0:
        return round((1 - price / old) * 100, 1)
    return None


def _extract_brand(name: str) -> str:
    known = [
        "Samsung", "Apple", "Sony", "Huawei", "Xiaomi", "OnePlus", "Google",
        "LG", "Philips", "Bose", "JBL", "Sennheiser", "Jabra", "AKG",
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI",
        "Panasonic", "Hisense", "TCL", "Grundig",
        "Bosch", "Miele", "Whirlpool", "Electrolux",
        "Nintendo", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm", "GoPro",
    ]
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    return name.split()[0] if name else ""


def _fetch(session: requests.Session, url: str) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code in (404, 410):
                return None
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code in (404, 410):
                return None
            logger.warning("[technomarket] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.warning("[technomarket] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    return None


def _get_max_page(soup: BeautifulSoup, max_pages: int) -> int:
    pages_div = soup.select_one('.pages')
    if not pages_div:
        return 1
    nums = []
    for a in pages_div.find_all('a'):
        t = a.get_text(strip=True)
        if t.isdigit():
            nums.append(int(t))
    return min(max(nums, default=1), max_pages)


def _parse_page(soup: BeautifulSoup, page_url: str) -> list[dict]:
    products = []
    # tm-product-item is the custom element wrapping each product
    cards = soup.select('tm-product-item[data-product]')
    if not cards:
        # Fallback: search by product-image link presence
        cards = [a.find_parent() for a in soup.select('a.product-image[href]') if a.find_parent()]

    for card in cards:
        # Name: brand + name spans inside .overview a.title
        title_el = card.select_one('a.title')
        if not title_el:
            continue
        brand_span = title_el.select_one('span.brand')
        name_span  = title_el.select_one('span.name')
        if brand_span and name_span:
            name = f"{brand_span.get_text(strip=True)} {name_span.get_text(strip=True)}"
        else:
            name = title_el.get_text(strip=True)
        name = name.strip()
        if not name or len(name) < 4:
            continue

        # Prices
        euro_el = card.select_one('.euro_price')
        bgn_el  = card.select_one('.bgn_price')
        old_el  = card.select_one('.old-price')
        old_txt = old_el.get_text(strip=True) if old_el else ''

        # Installment products: current price = monthly, real price is in "ПЦ:..." old-price
        if old_txt.startswith('ПЦ:'):
            price = None
            for part in old_txt.split('/'):
                if '€' in part:
                    price = _parse_price(part)
                    if price:
                        break
            if not price:
                for part in old_txt.split('/'):
                    if 'лв' in part:
                        bgn = _parse_price(part)
                        if bgn:
                            price = round(bgn / 1.95583, 2)
                            break
            old_price = None
        else:
            price = None
            if euro_el:
                price = _parse_price(euro_el.get_text(strip=True))
            if not price and bgn_el:
                bgn = _parse_price(bgn_el.get_text(strip=True))
                if bgn:
                    price = round(bgn / 1.95583, 2)
            old_price = _parse_price(old_txt) if old_txt else None

        if not price:
            continue

        # Link
        link_el = card.select_one('a.product-image[href], a.title[href]')
        href = link_el['href'] if link_el else ""
        if href and not href.startswith('http'):
            href = BASE_URL + href

        # Image
        img_el = card.select_one('a.product-image img[src]')
        img = img_el['src'] if img_el else ""

        products.append({
            "name": name, "price": price, "old_price": old_price,
            "img": img, "link": href,
        })

    return products


def scrape_technomarket(max_categories: int = 99, max_pages: int = 7) -> list[dict]:
    session = requests.Session()
    try:
        session.get(BASE_URL + "/", headers=HEADERS, timeout=15)
        time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass

    all_offers: list[dict] = []
    seen:       set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    for path, category, cat_label in TECHNOMARKET_CATEGORIES[:max_categories]:
        url1  = BASE_URL + path
        resp  = _fetch(session, url1)
        if not resp:
            logger.warning("[technomarket] Skipping %s (not found)", path)
            continue

        soup      = BeautifulSoup(resp.text, 'html.parser')
        max_pg    = _get_max_page(soup, max_pages)
        logger.info("[technomarket] %s — %d pages", cat_label, max_pg)

        for page in range(1, max_pg + 1):
            if page == 1:
                page_soup = soup
            else:
                url  = f"{url1}?page={page}"
                resp = _fetch(session, url)
                if not resp:
                    break
                page_soup = BeautifulSoup(resp.text, 'html.parser')

            raw = _parse_page(page_soup, url1)

            new = 0
            for p in raw:
                key = f"{p['name']}|{p['price']}"
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                all_offers.append({
                    "store":        "technomarket",
                    "raw_name":     p["name"],
                    "brand":        _extract_brand(p["name"]),
                    "category":     category,
                    "category_raw": cat_label,
                    "price":        p["price"],
                    "old_price":    p["old_price"],
                    "discount_pct": _discount(p["price"], p["old_price"]),
                    "image_url":    p["img"],
                    "url":          p["link"],
                    "in_stock":     True,
                    "scraped_at":   scraped_at,
                })

            logger.info("[technomarket] %s p%d -> %d raw, %d new", cat_label, page, len(raw), new)

            if not raw:
                break
            time.sleep(random.uniform(0.8, 1.8))

        time.sleep(random.uniform(1.5, 2.5))

    logger.info("[technomarket] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "technomarket").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i+100]).execute()
        total += len(offers[i:i+100])
    logger.info("[technomarket] Saved %d", total)
    return total


def save_to_json(offers: list[dict], filename: str = "technomarket_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(offers)} offers -> {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    test_mode   = "--test"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_technomarket(
        max_categories=1 if test_mode else 99,
        max_pages=2      if test_mode else 7,
    )

    if offers:
        save_to_json(offers)
        print(f"OK: {len(offers)} products")
        for o in offers[:5]:
            disc = f" [-{o['discount_pct']}%]" if o.get("discount_pct") else ""
            print(f"  {o['raw_name'][:55]:<55} {o['price']:.2f} EUR{disc}")
        if to_supabase:
            save_to_supabase(offers)
    else:
        print("No products found")
