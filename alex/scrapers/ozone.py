"""
Ozone.bg scraper — requests + BeautifulSoup
Site: https://www.ozone.bg

Употреба:
  py -m alex.scrapers.ozone                 # scrape + JSON
  py -m alex.scrapers.ozone --supabase      # + Supabase
  py -m alex.scrapers.ozone --test          # 1 категория, 2 стр.
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

BASE_URL = "https://www.ozone.bg"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.ozone.bg/",
}

OZONE_CATEGORIES = [
    ("/mobilni-ustroistva/smartfoni/",         "phones",     "Смартфони"),
    ("/laptopi-monitori-i-kompyutri/laptopi/", "laptops",    "Лаптопи"),
    ("/mobilni-ustroistva/tableti/",           "tablets",    "Таблети"),
    ("/tv-foto-i-video/televizori/",           "tvs",        "Телевизори"),
    ("/audio-i-video/slushalki/",              "headphones", "Слушалки"),
    ("/gaming/",                               "gaming",     "Гейминг"),
    ("/tv-foto-i-video/fotoaparati/",          "cameras",    "Фотоапарати"),
]


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = text.strip()
    # Strip Bulgarian label prefixes like "ПЦД:", "Цена:"
    t = re.sub(r'^[А-Яа-яA-Za-z:\s]+', '', t).strip()
    # Collapse thousands spaces: "1 299,00" → "1299,00"
    t = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
    # Remove all non-numeric except comma and dot
    cleaned = re.sub(r'[^\d,.]', '', t)
    if not cleaned:
        return None
    # Thousands comma: ",NNN" at end or followed by "." → strip
    cleaned = re.sub(r',(\d{3})(?=[,.]|$)', r'\1', cleaned)
    cleaned = cleaned.replace(',', '.')
    parts = cleaned.split('.')
    if len(parts) >= 2:
        cleaned = ''.join(parts[:-1]) + '.' + parts[-1][:2]
    try:
        return round(float(cleaned), 2) if cleaned else None
    except ValueError:
        return None


def _discount(price: Optional[float], old: Optional[float]) -> Optional[float]:
    if price and old and old > price > 0:
        return round((1 - price / old) * 100, 1)
    return None


def _extract_brand(name: str) -> str:
    known = [
        "Samsung", "Apple", "Sony", "Huawei", "Xiaomi", "OnePlus", "Google",
        "LG", "Philips", "Bose", "JBL", "Sennheiser", "Jabra", "AKG",
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI",
        "Panasonic", "Hisense", "TCL", "Grundig",
        "Bosch", "Miele", "Whirlpool", "Electrolux", "Indesit", "Gorenje",
        "Nintendo", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm", "GoPro",
        "Dyson", "iRobot",
    ]
    words = name.split()
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    # Also check second word (some names start with category type in BG)
    if len(words) >= 2:
        second = words[1].lower()
        for b in known:
            if second.startswith(b.lower()):
                return b
    return words[0] if words else ""


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
            logger.warning("[ozone] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.warning("[ozone] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    return None


def _get_max_page(soup: BeautifulSoup, max_pages: int) -> int:
    # Ozone pager: <div class="pages"> or <div class="toolbar-bottom">
    # Last page number link tells us max
    nums = []
    for a in soup.select('.pages a, .toolbar-bottom .pages a, .pager a'):
        t = a.get_text(strip=True)
        if t.isdigit():
            nums.append(int(t))
    # Also check data-page or href="?p=N" links
    for a in soup.find_all('a', href=re.compile(r'[?&]p=(\d+)')):
        m = re.search(r'[?&]p=(\d+)', a['href'])
        if m:
            nums.append(int(m.group(1)))
    return min(max(nums, default=1), max_pages)


def _parse_page(soup: BeautifulSoup, cat_url: str) -> list[dict]:
    products = []
    cards = soup.select('li.product-item, div.product-item')

    for card in cards:
        # Name
        name_el = card.select_one('.product-name a, .product-item-name a, h2.product-name a')
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 4:
            continue

        # Link
        href = name_el.get('href', '')
        if href and not href.startswith('http'):
            href = BASE_URL + href

        # Price — try sale price first, then regular price
        price: Optional[float] = None
        old_price: Optional[float] = None

        special_el = card.select_one('.special-price .price, .special-price')
        regular_el = card.select_one('.regular-price .price, .regular-price')
        old_el     = card.select_one('.old-price .price, .old-price')

        if special_el:
            price = _parse_price(special_el.get_text(strip=True))
            if old_el:
                old_price = _parse_price(old_el.get_text(strip=True))
            elif regular_el:
                old_price = _parse_price(regular_el.get_text(strip=True))
        elif regular_el:
            price = _parse_price(regular_el.get_text(strip=True))
        else:
            # Last-resort: any .price element
            price_el = card.select_one('.price')
            if price_el:
                price = _parse_price(price_el.get_text(strip=True))

        if not price:
            continue

        # Image — prefer data-src (lazy load) over src
        img = ""
        img_el = card.select_one('img.product-image, img.product-photo-image, .product-image img')
        if img_el:
            img = img_el.get('data-src') or img_el.get('data-original') or img_el.get('src', '')
            if img and not img.startswith('http'):
                img = BASE_URL + img

        products.append({
            "name": name,
            "price": price,
            "old_price": old_price,
            "img": img,
            "link": href,
        })

    return products


def scrape_ozone(max_categories: int = 99, max_pages: int = 10) -> list[dict]:
    session = requests.Session()
    try:
        session.get(BASE_URL + "/", headers=HEADERS, timeout=15)
        time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass

    all_offers: list[dict] = []
    seen:       set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    for path, category, cat_label in OZONE_CATEGORIES[:max_categories]:
        url1 = BASE_URL + path
        resp = _fetch(session, url1)
        if not resp:
            logger.warning("[ozone] Skipping %s (not found)", path)
            continue

        soup   = BeautifulSoup(resp.text, 'html.parser')
        max_pg = _get_max_page(soup, max_pages)
        logger.info("[ozone] %s — %d pages", cat_label, max_pg)

        for page in range(1, max_pg + 1):
            if page == 1:
                page_soup = soup
            else:
                url  = f"{url1}?p={page}"
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
                    "store":        "ozone",
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

            logger.info("[ozone] %s p%d -> %d raw, %d new", cat_label, page, len(raw), new)

            if not raw:
                break
            time.sleep(random.uniform(0.8, 1.8))

        time.sleep(random.uniform(1.5, 2.5))

    logger.info("[ozone] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "ozone").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i + 100]).execute()
        total += len(offers[i:i + 100])
    logger.info("[ozone] Saved %d", total)
    return total


def save_to_json(offers: list[dict], filename: str = "ozone_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(offers)} offers -> {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    test_mode   = "--test"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_ozone(
        max_categories=1 if test_mode else 99,
        max_pages=2      if test_mode else 10,
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
