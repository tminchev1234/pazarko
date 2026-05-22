"""
Ozone.bg scraper — Selenium (headless Chrome)
Site: https://www.ozone.bg
Причина за Selenium: сайтът изисква JavaScript за зареждане на продукти

Употреба:
  py -m alex.scrapers.ozone                 # scrape + JSON
  py -m alex.scrapers.ozone --supabase      # + Supabase
  py -m alex.scrapers.ozone --test          # 1 категория, 2 стр.
  py -m alex.scrapers.ozone --show          # видим браузър
"""

from __future__ import annotations
import json
import logging
import re
import time
import random
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ozone.bg"

OZONE_CATEGORIES = [
    ("/mobilni-ustroistva/smartfoni/",         "phones",     "Смартфони"),
    ("/laptopi-monitori-i-kompyutri/laptopi/", "laptops",    "Лаптопи"),
    ("/mobilni-ustroistva/tableti/",           "tablets",    "Таблети"),
    ("/tv-foto-i-video/televizori/",           "tvs",        "Телевизори"),
    ("/audio-i-video/slushalki/",              "headphones", "Слушалки"),
    ("/gaming/",                               "gaming",     "Гейминг"),
    ("/tv-foto-i-video/fotoaparati/",          "cameras",    "Фотоапарати"),
]

EXTRACT_JS = r"""
const products = [];
const seen = new Set();

const cards = Array.from(document.querySelectorAll('div.product-item, li.product-item'));

cards.forEach(card => {
    // Name + link — first text-bearing anchor
    let name = '', link = '';
    for (const a of card.querySelectorAll('a[href]')) {
        const t = (a.textContent || '').trim();
        if (t.length > 5) { name = t; link = a.href; break; }
    }
    if (!name || name.length < 4) return;

    // Current price — .special-price (sale) or .price (regular)
    const specialEl = card.querySelector('.special-price');
    const priceEl   = card.querySelector('.price');
    const priceRaw  = specialEl
        ? (specialEl.textContent || '').trim()
        : (priceEl ? (priceEl.textContent || '').trim() : '');
    if (!priceRaw) return;

    // Old / RRP price — .pcd-price contains "ПЦД: X €"
    const pcdEl   = card.querySelector('.pcd-price');
    const oldRaw  = pcdEl ? (pcdEl.textContent || '').trim() : '';

    // Image
    const imgEl = card.querySelector('img');
    const img   = imgEl ? (imgEl.dataset.src || imgEl.src || '') : '';

    const key = name + '|' + priceRaw;
    if (seen.has(key)) return;
    seen.add(key);

    products.push({ name, priceRaw, oldRaw, img, link });
});

return JSON.stringify(products);
"""


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = text.strip()
    # Strip label prefixes like "Special Price", "ПЦД:", "Цена:"
    t = re.sub(r'^[А-Яа-яA-Za-z:\s]+', '', t).strip()
    t = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
    cleaned = re.sub(r'[^\d,.]', '', t)
    if not cleaned:
        return None
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
        "Dyson", "iRobot", "Honor", "Motorola", "Realme", "Oppo",
    ]
    words = name.split()
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    if len(words) >= 2:
        for b in known:
            if words[1].lower().startswith(b.lower()):
                return b
    return words[0] if words else ""


def scrape_ozone(headless: bool = True, max_categories: int = 99,
                 max_pages: int = 8) -> list[dict]:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)

    all_offers: list[dict] = []
    seen:       set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        driver.get(BASE_URL + "/")
        time.sleep(random.uniform(2.0, 3.0))

        for path, category, cat_label in OZONE_CATEGORIES[:max_categories]:
            logger.info("[ozone] Category: %s", cat_label)
            prev_count = -1

            for page in range(1, max_pages + 1):
                url = BASE_URL + path
                if page > 1:
                    url = f"{url}?p={page}"

                try:
                    driver.get(url)
                    time.sleep(random.uniform(2.5, 4.0))

                    # Scroll to trigger lazy loading
                    for pct in (0.3, 0.6, 1.0):
                        driver.execute_script(
                            f"window.scrollTo(0, document.body.scrollHeight * {pct})"
                        )
                        time.sleep(0.5)

                    result = driver.execute_script(EXTRACT_JS)
                    raw    = json.loads(result) if result else []

                    if not raw:
                        logger.info("[ozone] %s p%d — empty, stopping", cat_label, page)
                        break

                    if len(raw) == prev_count and page > 1:
                        logger.info("[ozone] %s p%d — repeated page, stopping", cat_label, page)
                        break
                    prev_count = len(raw)

                    new = 0
                    for p in raw:
                        price = _parse_price(p.get("priceRaw"))
                        if not price:
                            continue
                        name = (p.get("name") or "").strip()
                        key  = f"{name}|{price}"
                        if key in seen:
                            continue
                        seen.add(key)
                        new += 1

                        old_price = _parse_price(p.get("oldRaw"))
                        all_offers.append({
                            "store":        "ozone",
                            "raw_name":     name,
                            "brand":        _extract_brand(name),
                            "category":     category,
                            "category_raw": cat_label,
                            "price":        price,
                            "old_price":    old_price,
                            "discount_pct": _discount(price, old_price),
                            "image_url":    p.get("img", ""),
                            "url":          p.get("link", url),
                            "in_stock":     True,
                            "scraped_at":   scraped_at,
                        })

                    logger.info("[ozone] %s p%d → %d raw, %d new", cat_label, page, len(raw), new)

                    if new == 0 and page > 1:
                        break

                    time.sleep(random.uniform(1.5, 2.5))

                except Exception as exc:
                    logger.error("[ozone] %s p%d: %s", cat_label, page, exc)
                    break

            time.sleep(random.uniform(2.0, 3.0))

    finally:
        driver.quit()

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
    show_mode   = "--show"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_ozone(
        headless=not show_mode,
        max_categories=1 if test_mode else 99,
        max_pages=2      if test_mode else 8,
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
