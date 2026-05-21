"""
Technopolis Bulgaria scraper — Selenium (headless Chrome)
Site: https://www.technopolis.bg
Причина за Selenium: Angular SSR + bot protection (403 с requests)

Употреба:
  py -m alex.scrapers.technopolis                 # scrape + JSON
  py -m alex.scrapers.technopolis --supabase      # + Supabase
  py -m alex.scrapers.technopolis --test          # 1 категория, 2 стр.
  py -m alex.scrapers.technopolis --show          # видим браузър
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

BASE_URL = "https://www.technopolis.bg"

TECHNOPOLIS_CATEGORIES = [
    # ── Електроника ──────────────────────────────────────────────────
    ("/bg/Smartfoni-mobilni-telefoni-i-tableti/Smartfoni-i-mobilni-telefoni/c/P11040101", "phones",      "Телефони"),
    ("/bg/Kompyutri-i-periferiya/Laptopi/c/P11010101",                                    "laptops",     "Лаптопи"),
    ("/bg/Smartfoni-mobilni-telefoni-i-tableti/Tableti/c/P11010701",                      "tablets",     "Таблети"),
    ("/bg/TV--Video-i-Gaming/Televizori/c/P11090104",                                     "tvs",         "Телевизори"),
    ("/bg/Sport-i-svobodno-vreme/Audio-slushalki-i-mikrofoni/Audio-slushalki/c/P11070201","headphones",  "Аудио слушалки"),
    ("/bg/Sport-i-svobodno-vreme/Audio-slushalki-i-mikrofoni/True-wireless-slushalki/c/P11070203","headphones","True Wireless"),
    ("/bg/Kompyutarni-aksesoari/Slushalki-i-mikrofoni/Slushalki/c/P11020501",            "headphones",  "PC Слушалки"),
    ("/bg/TV--Video-i-Gaming/Konzoli/c/P11030101",                                        "gaming",      "Конзоли"),
    ("/bg/Foto-i-videokameri/c/P1105",                                                    "cameras",     "Фотоапарати"),
    # ── Бяла техника ─────────────────────────────────────────────────
    ("/bg/Domakinski-elektrouredi/c/P1110",                                               "appliances",  "Домакински уреди"),
    ("/bg/Malki-elektrouredi/c/P1113",                                                    "appliances",  "Малки уреди"),
]

# JS to extract product data from rendered Angular page
EXTRACT_JS = r"""
const products = [];
const seen = new Set();

// Technopolis Angular: te-product-box custom elements
let cards = Array.from(document.querySelectorAll('te-product-box'));

// Fallbacks
if (cards.length === 0)
    cards = Array.from(document.querySelectorAll('.product-box, [class*="product-box"]'));
if (cards.length === 0)
    cards = Array.from(document.querySelectorAll('cx-product-list-item, .cx-product-card'));

cards.forEach(card => {
    // Name
    const nameEl = (
        card.querySelector('a.product-box__title-link') ||
        card.querySelector('[class*="title-link"]') ||
        card.querySelector('[class*="product-title"]') ||
        card.querySelector('h3 a, h2 a')
    );
    const name = (nameEl?.getAttribute('title') || nameEl?.textContent || '').trim();
    if (!name || name.length < 4) return;

    // Prices: two values — EUR first, BGN second
    const priceEls = card.querySelectorAll('.product-box__price-value, [class*="price-value"]');
    if (!priceEls.length) return;
    const priceRaw = priceEls[0].textContent.trim();   // EUR

    // Old price
    const oldEl = card.querySelector('.product-box__old-price, [class*="old-price"], s, del');
    const oldRaw = oldEl?.textContent?.trim() || null;

    // Image
    const imgEl = card.querySelector('img[src]');
    const img = imgEl?.src || '';

    // Link
    const linkEl = card.querySelector('a[href*="/p/"], a[href*="/bg/"]') || nameEl?.closest('a');
    const link = linkEl?.href || '';

    const key = name + '|' + priceRaw;
    if (seen.has(key)) return;
    seen.add(key);

    products.push({ name, priceRaw, oldRaw, img, link });
});

return JSON.stringify(products);
"""


# Technopolis placeholder image — served when product has no real photo
_TP_PLACEHOLDER_PATTERNS = ("NI-Listing", "/NI.", "-NI-", "noimage", "no-image", "placeholder")


def _clean_img(url: str) -> str:
    """Return None if url is a Technopolis generic placeholder, else the url."""
    if not url:
        return ""
    low = url.lower()
    if any(p.lower() in low for p in _TP_PLACEHOLDER_PATTERNS):
        return ""
    return url


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = text.strip()
    # European: "1.299,99 €"
    if re.search(r'\d\.\d{3}', t) and ',' in t:
        cleaned = re.sub(r'[^\d,]', '', t).replace(',', '.')
    else:
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
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI",
        "Panasonic", "Hisense", "TCL", "Grundig",
        "Bosch", "Miele", "Whirlpool", "Electrolux", "Indesit",
        "Nintendo", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm", "GoPro",
    ]
    low = name.lower()
    for b in known:
        if low.startswith(b.lower()):
            return b
    return name.split()[0] if name else ""


def scrape_technopolis(headless: bool = True, max_categories: int = 99,
                       max_pages: int = 20) -> list[dict]:
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
        for path, category, cat_label in TECHNOPOLIS_CATEGORIES[:max_categories]:
            logger.info("[technopolis] Category: %s", cat_label)
            prev_raw_count = -1

            for page in range(1, max_pages + 1):
                url = BASE_URL + path
                if page > 1:
                    url = f"{url}?currentPage={page - 1}"

                try:
                    driver.get(url)
                    time.sleep(random.uniform(3.5, 5.5))

                    # Scroll to trigger lazy loading
                    for pct in (0.3, 0.6, 1.0):
                        driver.execute_script(
                            f"window.scrollTo(0, document.body.scrollHeight * {pct})"
                        )
                        time.sleep(0.8)

                    result = driver.execute_script(EXTRACT_JS)
                    raw    = json.loads(result) if result else []

                    if not raw:
                        logger.info("[technopolis] %s p%d — no products", cat_label, page)
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
                            "store":        "technopolis",
                            "raw_name":     name,
                            "brand":        _extract_brand(name),
                            "category":     category,
                            "category_raw": cat_label,
                            "price":        price,
                            "old_price":    old_price,
                            "discount_pct": _discount(price, old_price),
                            "image_url":    _clean_img(p.get("img", "")),
                            "url":          p.get("link", url),
                            "in_stock":     True,
                            "scraped_at":   scraped_at,
                        })

                    logger.info("[technopolis] %s p%d → %d raw, %d new",
                                cat_label, page, len(raw), new)

                    if new == 0 and len(raw) == prev_raw_count:
                        logger.info("[technopolis] %s — repeated page, stopping", cat_label)
                        break
                    prev_raw_count = len(raw)

                    if len(raw) < 10:
                        break

                    time.sleep(random.uniform(2.0, 3.5))

                except Exception as exc:
                    logger.error("[technopolis] %s p%d: %s", cat_label, page, exc)
                    break

            time.sleep(random.uniform(2.0, 3.5))

    finally:
        driver.quit()

    logger.info("[technopolis] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "technopolis").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i+100]).execute()
        total += len(offers[i:i+100])
    logger.info("[technopolis] Saved %d", total)
    return total


def save_to_json(offers: list[dict], filename: str = "technopolis_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(offers)} -> {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    headless    = "--show"     not in sys.argv
    test_mode   = "--test"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_technopolis(
        headless=headless,
        max_categories=1 if test_mode else 99,
        max_pages=2 if test_mode else 20,
    )

    if offers:
        save_to_json(offers)
        print("OK: " + str(len(offers)) + " products")
        for o in offers[:5]:
            disc = f" [-{o['discount_pct']}%]" if o.get("discount_pct") else ""
            print(f"  {o['raw_name'][:55]:<55} {o['price']:.2f} €{disc}")
        if to_supabase:
            save_to_supabase(offers)
    else:
        print("❌ Няма продукти — пробвай с --show")
