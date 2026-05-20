"""
Ardes Bulgaria scraper — Selenium (headless Chrome)
Site: https://www.ardes.bg
Причина за Selenium: Cloudflare JS challenge блокира requests/cloudscraper

Употреба:
  py -m alex.scrapers.ardes                 # scrape + JSON
  py -m alex.scrapers.ardes --supabase      # + Supabase
  py -m alex.scrapers.ardes --test          # 1 категория, 2 стр.
  py -m alex.scrapers.ardes --show          # видим браузър
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

BASE_URL = "https://www.ardes.bg"

ARDES_CATEGORIES = [
    ("/laptopi/laptopi",                          "laptops",     "Лаптопи"),
    ("/smartfoni/smartfoni",                      "phones",      "Телефони"),
    ("/slushalki/slushalki",                      "headphones",  "Слушалки"),
    ("/televizori/televizori",                    "tvs",         "Телевизори"),
    ("/tableti/tableti",                          "tablets",     "Таблети"),
    ("/igri-i-igrovata-zona/konzoli",             "gaming",      "Конзоли"),
    ("/fotoaparati-i-videokameri/fotoaparati",    "cameras",     "Фотоапарати"),
    ("/domakinski-uredi/malki-domakinski-uredi",  "appliances",  "Малки уреди"),
]

EXTRACT_JS = r"""
const products = [];
const seen = new Set();

const cards = Array.from(document.querySelectorAll('div.product[data-sku]'));

cards.forEach(card => {
    // Name
    const nameEl = (
        card.querySelector('.isTruncated span') ||
        card.querySelector('.title span') ||
        card.querySelector('.title')
    );
    const name = (nameEl?.textContent || '').trim();
    if (!name || name.length < 4) return;

    // EUR price — first price-num in .eur-price container
    const eurContainer = card.querySelector('.eur-price, .prices-eur');
    const priceEl = eurContainer
        ? eurContainer.querySelector('.price-num')
        : card.querySelector('.price-num');
    if (!priceEl) return;

    // Get numeric text only (ignore nested currency span)
    const priceRaw = (priceEl.firstChild?.textContent || priceEl.textContent || '').trim();
    if (!priceRaw) return;

    // Old price
    const oldEl = card.querySelector('.old-price .price-num, .price-old');
    const oldRaw = oldEl ? (oldEl.firstChild?.textContent || oldEl.textContent || '').trim() : null;

    // Link
    const linkEl = card.querySelector('.product-head a[href]');
    let link = linkEl?.href || '';

    // Image
    const imgEl = card.querySelector('.product-head img[src]');
    const img = imgEl?.src || '';

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
    cleaned = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
    cleaned = re.sub(r'[^\d,.]', '', cleaned).replace(',', '.')
    parts = cleaned.split('.')
    if len(parts) >= 2:
        cleaned = parts[0] + '.' + parts[1][:2]
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
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI", "Toshiba",
        "Panasonic", "Hisense", "TCL", "Grundig",
        "Bosch", "Miele", "Whirlpool", "Electrolux",
        "Nintendo", "PlayStation", "Xbox", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm", "GoPro", "DJI",
    ]
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    return name.split()[0] if name else ""


def scrape_ardes(headless: bool = True, max_categories: int = 99,
                 max_pages: int = 5) -> list[dict]:
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
        for path, category, cat_label in ARDES_CATEGORIES[:max_categories]:
            logger.info("[ardes] Category: %s", cat_label)
            prev_count = -1

            for page in range(1, max_pages + 1):
                url = BASE_URL + path
                if page > 1:
                    url = f"{url}/page/{page}"

                try:
                    driver.get(url)
                    time.sleep(random.uniform(3.0, 5.0))

                    # Scroll to load lazy images
                    for pct in (0.3, 0.6, 1.0):
                        driver.execute_script(
                            f"window.scrollTo(0, document.body.scrollHeight * {pct})"
                        )
                        time.sleep(0.6)

                    result = driver.execute_script(EXTRACT_JS)
                    raw    = json.loads(result) if result else []

                    if not raw:
                        logger.info("[ardes] %s p%d — empty", cat_label, page)
                        break

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
                            "store":        "ardes",
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

                    logger.info("[ardes] %s p%d → %d raw, %d new",
                                cat_label, page, len(raw), new)

                    if new == 0 and len(raw) == prev_count:
                        logger.info("[ardes] %s — repeated page, stopping", cat_label)
                        break
                    prev_count = len(raw)

                    if len(raw) < 10:
                        break

                    time.sleep(random.uniform(1.5, 3.0))

                except Exception as exc:
                    logger.error("[ardes] %s p%d: %s", cat_label, page, exc)
                    break

            time.sleep(random.uniform(2.0, 3.5))

    finally:
        driver.quit()

    logger.info("[ardes] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "ardes").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i+100]).execute()
        total += len(offers[i:i+100])
    logger.info("[ardes] Saved %d", total)
    return total


def save_to_json(offers: list[dict], filename: str = "ardes_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(offers)} -> {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    headless    = "--show"     not in sys.argv
    test_mode   = "--test"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_ardes(
        headless=headless,
        max_categories=1 if test_mode else 99,
        max_pages=2 if test_mode else 5,
    )

    if offers:
        save_to_json(offers)
        print(f"OK: {len(offers)} products")
        for o in offers[:5]:
            disc = f" [-{o['discount_pct']}%]" if o.get("discount_pct") else ""
            print(f"  {o['raw_name'][:55]:<55} {o['price']:.2f} E{disc}")
        if to_supabase:
            save_to_supabase(offers)
    else:
        print("No products found - try --show to debug")
