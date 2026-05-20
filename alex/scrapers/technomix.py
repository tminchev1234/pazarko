"""
Technomix Bulgaria scraper — requests + BeautifulSoup
Site: https://technomix.bg

Употреба:
  py -m alex.scrapers.technomix                 # scrape + JSON
  py -m alex.scrapers.technomix --supabase      # + Supabase
  py -m alex.scrapers.technomix --test          # 1 категория, 2 стр.
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

BASE_URL = "https://technomix.bg"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://technomix.bg/",
}

TECHNOMIX_CATEGORIES = [
    # ── Стандартна електроника ──────────────────────────────────────
    ("/smartfoni",               "phones",      "Смартфони"),
    ("/telefoni",                "phones",      "Телефони"),
    ("/laptopi",                 "laptops",     "Лаптопи"),
    ("/televizori",              "tvs",         "Телевизори"),
    ("/slushalki",               "headphones",  "Слушалки"),
    ("/tableti",                 "tablets",     "Таблети"),
    ("/fotoaparati",             "cameras",     "Фотоапарати"),
    ("/gaming",                  "gaming",      "Гейминг"),
    # ── Бяла техника ────────────────────────────────────────────────
    ("/hladilnici",              "fridges",     "Хладилници"),
    ("/peralni-masini",          "washing",     "Перални машини"),
    ("/sushilni",                "washing",     "Сушилни"),
    ("/klimatici",               "ac",          "Климатици"),
    ("/prahosmukachki",          "vacuum",      "Прахосмукачки"),
    ("/gotvarki",                "cooking",     "Готварки"),
    ("/mikrovalni",              "cooking",     "Микровълнови"),
    ("/sudomialni",              "dishwasher",  "Съдомиялни"),
    ("/mali-domakinski-uredi",   "appliances",  "Малки домакински уреди"),
]


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = re.sub(r'[^\d,.]', '', text.strip())
    if not t:
        return None
    t = re.sub(r',(\d{3})(?=[,.]|$)', r'\1', t)
    t = t.replace(',', '.')
    parts = t.split('.')
    if len(parts) >= 2:
        t = ''.join(parts[:-1]) + '.' + parts[-1][:2]
    try:
        return round(float(t), 2) if t else None
    except ValueError:
        return None


def _discount(price, old):
    if price and old and old > price > 0:
        return round((1 - price / old) * 100, 1)
    return None


def _extract_brand(name: str) -> str:
    known = [
        "Samsung", "Apple", "Sony", "Huawei", "Xiaomi", "OnePlus", "Google",
        "LG", "Philips", "Bose", "JBL", "Sennheiser", "Jabra",
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI",
        "Panasonic", "Hisense", "TCL", "Grundig",
        "Bosch", "Miele", "Whirlpool", "Electrolux", "Gorenje", "Indesit",
        "Ariston", "Beko", "Candy", "Haier", "Midea", "Daikin", "Mitsubishi",
        "Toshiba", "Sharp", "Dyson", "Rowenta", "Tefal", "Moulinex",
        "Nintendo", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm",
    ]
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    return name.split()[0] if name else ""


def _fetch(session: requests.Session, url: str) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=25)
            if r.status_code in (404, 410):
                return None
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code in (404, 410):
                return None
            logger.warning("[technomix] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.warning("[technomix] attempt %d %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    return None


def _get_max_page(soup: BeautifulSoup, max_pages: int) -> int:
    # WooCommerce / common pagination patterns
    for sel in [
        '.woocommerce-pagination a.page-numbers',
        '.pagination a',
        'a.page-numbers',
        '.pages a',
        'nav.pagination a',
    ]:
        nums = []
        for a in soup.select(sel):
            t = a.get_text(strip=True)
            if t.isdigit():
                nums.append(int(t))
        if nums:
            return min(max(nums), max_pages)
    return 1


def _parse_page(soup: BeautifulSoup) -> list[dict]:
    products = []

    # Try multiple card selectors (WooCommerce and custom themes)
    card_selectors = [
        'li.product',
        '.product-item',
        '.product_wrapper',
        'div[class*="product-card"]',
        '.tm-product-card',
        'article.product',
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    if not cards:
        logger.debug("[technomix] no product cards found")
        return []

    for card in cards:
        # Name — try multiple selectors
        name = ""
        for nsel in ['.woocommerce-loop-product__title', 'h2', 'h3', '.product-title', '.product-name', 'a.title']:
            el = card.select_one(nsel)
            if el:
                name = el.get_text(strip=True)
                if name:
                    break
        if not name or len(name) < 4:
            continue

        # Price — current
        price = None
        for psel in ['.price ins .amount', '.price .amount', '.product-price', '.price']:
            el = card.select_one(psel)
            if el:
                price = _parse_price(el.get_text(strip=True))
                if price:
                    break

        # If price contains BGN (лв), convert to EUR
        if not price:
            for psel in ['.price', '.product-price']:
                el = card.select_one(psel)
                if el:
                    txt = el.get_text(strip=True)
                    if 'лв' in txt or 'BGN' in txt:
                        bgn = _parse_price(txt)
                        if bgn:
                            price = round(bgn / 1.95583, 2)
                    break

        if not price:
            continue

        # Old price
        old_price = None
        for osel in ['.price del .amount', 'del .amount', '.old-price', '.price del']:
            el = card.select_one(osel)
            if el:
                old_price = _parse_price(el.get_text(strip=True))
                if old_price and old_price > price:
                    break
                else:
                    old_price = None

        # Link
        href = ""
        for lsel in ['a.woocommerce-loop-product__link', 'a[href]', 'h2 a', 'h3 a']:
            el = card.select_one(lsel)
            if el and el.get('href'):
                href = el['href']
                if not href.startswith('http'):
                    href = BASE_URL + href
                break

        # Image
        img = ""
        for isel in ['img.wp-post-image', 'img[src*="technomix"]', 'img[src]']:
            el = card.select_one(isel)
            if el:
                img = el.get('data-src') or el.get('src') or ""
                if img and not img.startswith('http'):
                    img = BASE_URL + img
                if img:
                    break

        products.append({
            "name": name,
            "price": price,
            "old_price": old_price,
            "img": img,
            "link": href,
        })

    return products


def scrape_technomix(max_categories: int = 99, max_pages: int = 6) -> list[dict]:
    session = requests.Session()
    try:
        session.get(BASE_URL + "/", headers=HEADERS, timeout=15)
        time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass

    all_offers: list[dict] = []
    seen:       set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    for path, category, cat_label in TECHNOMIX_CATEGORIES[:max_categories]:
        url1 = BASE_URL + path
        resp = _fetch(session, url1)
        if not resp:
            logger.warning("[technomix] Skipping %s (not found)", path)
            continue

        soup   = BeautifulSoup(resp.text, 'html.parser')
        max_pg = _get_max_page(soup, max_pages)
        logger.info("[technomix] %s — %d pages", cat_label, max_pg)

        for page in range(1, max_pg + 1):
            if page == 1:
                page_soup = soup
            else:
                # WooCommerce pagination: ?page=N or /page/N/
                purl = f"{url1}/page/{page}/"
                presp = _fetch(session, purl)
                if not presp:
                    # fallback to ?page=N
                    purl = f"{url1}?page={page}"
                    presp = _fetch(session, purl)
                if not presp:
                    break
                page_soup = BeautifulSoup(presp.text, 'html.parser')

            raw = _parse_page(page_soup)

            new = 0
            for p in raw:
                key = f"{p['name']}|{p['price']}"
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                all_offers.append({
                    "store":        "technomix",
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

            logger.info("[technomix] %s p%d -> %d raw, %d new", cat_label, page, len(raw), new)

            if not raw:
                break
            time.sleep(random.uniform(0.8, 1.8))

        time.sleep(random.uniform(1.5, 2.5))

    logger.info("[technomix] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "technomix").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i + 100]).execute()
        total += len(offers[i:i + 100])
    return total


def main():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    test    = "--test"     in sys.argv
    push_db = "--supabase" in sys.argv

    offers = scrape_technomix(
        max_categories=1 if test else 99,
        max_pages=2 if test else 6,
    )
    print(f"\nTechnomix: {len(offers)} offers")
    for o in offers[:5]:
        disc = f" [-{o['discount_pct']}%]" if o.get("discount_pct") else ""
        print(f"  {o['raw_name'][:55]:<55} {o['price']:.2f} EUR{disc} [{o['category']}]")

    if offers:
        with open("technomix_offers.json", "w", encoding="utf-8") as f:
            json.dump(offers, f, ensure_ascii=False, indent=2)
        print(f"Saved → technomix_offers.json")

        if push_db:
            saved = save_to_supabase(offers)
            print(f"Saved {saved} to Supabase")


if __name__ == "__main__":
    main()
