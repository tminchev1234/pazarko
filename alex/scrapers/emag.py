"""
eMAG Bulgaria scraper — Selenium
Скрейпва електроника от emag.bg по категории.

Употреба:
  py -m alex.scrapers.emag                  # scrape + save JSON
  py -m alex.scrapers.emag --supabase       # + upload to Supabase
  py -m alex.scrapers.emag --test           # само 1 категория
  py -m alex.scrapers.emag --show           # headful (вижда се браузъра)
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

# ── Categories (eMAG BG URL paths) ────────────────────────────────
EMAG_CATEGORIES = [
    # ── Електроника ──────────────────────────────────────────────────
    ("audio-slushalki-za-mobilni-telefoni",  "headphones",   "Аудио слушалки"),
    ("bezjichni-slushalki",                  "headphones",   "Bluetooth слушалки"),
    ("mobilni-telefoni",                     "phones",       "Телефони"),
    ("tableti",                              "tablets",      "Таблети"),
    ("laptopi",                              "laptops",      "Лаптопи"),
    ("televizori",                           "tvs",          "Телевизори"),
    ("gaming-konzoli",                       "gaming",       "Конзоли"),
    ("fotoaparati",                          "cameras",      "Фотоапарати"),
    ("aksesoari-za-telefoni",               "accessories",  "Телефонни аксесоари"),
    ("aksesoari-za-laptopi",                "accessories",  "Лаптоп аксесоари"),
    # ── Бяла техника (специфични категории — преди общите!) ──────────
    ("hladilnici",                          "fridges",      "Хладилници"),
    ("peralni-masini",                      "washing",      "Перални машини"),
    ("sushilni-masini",                     "washing",      "Сушилни машини"),
    ("klimatici",                           "ac",           "Климатици"),
    ("prahosmukachki",                      "vacuum",       "Прахосмукачки"),
    ("roboti-prahosmukachki",               "vacuum",       "Роботи-прахосмукачки"),
    ("gotvarki-i-kuhnenski-plotove",        "cooking",      "Готварки и котлони"),
    ("mikrovalni-peshti",                   "cooking",      "Микровълнови"),
    ("masini-za-mijalnya",                  "dishwasher",   "Съдомиялни"),
    # ── Общи домакински (за продукти извън горните категории) ────────
    ("golemi-domakinski-uredi",             "appliances",   "Голяма техника"),
    ("malki-domakinski-uredi",              "appliances",   "Малка техника"),
]

BASE_URL = "https://www.emag.bg"

# ── JS: extract product cards from listing page ────────────────────
EXTRACT_JS = r"""
const products = [];
const seen = new Set();

// eMAG 2025 card structure: .card-v2
let cards = Array.from(document.querySelectorAll('.card-v2'));

// Fallbacks
if (cards.length === 0)
    cards = Array.from(document.querySelectorAll('.card-item'));
if (cards.length === 0)
    cards = Array.from(document.querySelectorAll('[class*="card-"]'))
              .filter(el => el.querySelector('a[href*="/pd/"]'));

cards.forEach(card => {
    // Name — .card-v2-title
    const nameEl = card.querySelector(
        '.card-v2-title, [class*="card-v2-title"], [class*="js-product-url"][class*="title"]'
    );
    const name = (nameEl?.textContent || '').trim();
    if (!name || name.length < 5) return;

    // Price — first .product-new-price = EUR, second = BGN (skip)
    const priceEls = card.querySelectorAll('.product-new-price');
    if (!priceEls.length) return;
    const priceRaw = priceEls[0].textContent.trim();

    // Old price
    const oldEl = card.querySelector(
        '.product-old-price, [class*="old-price"], s.price, del'
    );
    const oldRaw = oldEl?.textContent?.trim() || null;

    // Image
    const imgEl = card.querySelector('img[src*="emagst"], img[src*="akamaized"], img[src]');
    const img = imgEl?.src || '';

    // Link — /pd/ pattern
    const linkEl = card.querySelector('a[href*="/pd/"], a[href*="/p/"]') || nameEl?.closest('a');
    const link = linkEl?.href || '';

    const key = name + '|' + priceRaw;
    if (seen.has(key)) return;
    seen.add(key);

    products.push({ name, priceRaw, oldRaw, img, link });
});

return JSON.stringify(products);
"""

# ── Price parser ────────────────────────────────────────────────────
def _parse_price(text: Optional[str]) -> Optional[float]:
    """
    '1.299,99 €'  → 1299.99  (European: dot=thousands, comma=decimal)
    '299,99 лв.'  → 299.99
    '39.90'       → 39.90
    '1 299.99'    → 1299.99
    """
    if not text:
        return None
    t = text.strip().lstrip('/')   # eMAG BGN price starts with "/"

    # European format: "1.299,99" — dot is thousands separator
    if re.search(r'\d\.\d{3}', t) and ',' in t:
        cleaned = re.sub(r'[^\d,]', '', t).replace(',', '.')
    else:
        cleaned = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
        cleaned = re.sub(r'[^\d,.]', '', cleaned).replace(',', '.')
        parts = cleaned.split('.')
        if len(parts) >= 2:
            cleaned = parts[0] + '.' + parts[1][:2]

    if not cleaned:
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _discount(price: Optional[float], old: Optional[float]) -> Optional[float]:
    if price and old and old > price > 0:
        return round((1 - price / old) * 100, 1)
    return None


# ── Scraper ──────────────────────────────────────────────────────────
def scrape_emag(headless: bool = True, max_categories: int = 99,
                max_pages: int = 5) -> list[dict]:
    """
    Scrape eMAG Bulgaria electronics category pages.
    Returns list of offer dicts ready for Supabase insert.
    """
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
    seen_global: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        for i, (path, category, cat_label) in enumerate(EMAG_CATEGORIES[:max_categories]):
            logger.info("[emag] Category: %s (%s)", cat_label, path)

            prev_raw_count = -1
            for page in range(1, max_pages + 1):
                url = f"{BASE_URL}/{path}/c"
                if page > 1:
                    url = f"{BASE_URL}/{path}/c?p={page}"

                try:
                    driver.get(url)
                    time.sleep(random.uniform(3.0, 5.0))

                    # Scroll to trigger lazy loading
                    for scroll_pct in (0.3, 0.6, 1.0):
                        driver.execute_script(
                            f"window.scrollTo(0, document.body.scrollHeight * {scroll_pct})"
                        )
                        time.sleep(0.8)

                    result = driver.execute_script(EXTRACT_JS)
                    raw = json.loads(result) if result else []

                    if not raw:
                        logger.info("[emag] %s p%d — no products, stopping pagination", cat_label, page)
                        break

                    new_count = 0
                    for p in raw:
                        price = _parse_price(p.get("priceRaw"))
                        if not price:
                            continue

                        name  = (p.get("name") or "").strip()
                        key   = f"{name}|{price}"
                        if key in seen_global:
                            continue
                        seen_global.add(key)
                        new_count += 1

                        old_price   = _parse_price(p.get("oldRaw"))
                        disc_pct    = _discount(price, old_price)

                        all_offers.append({
                            "store":        "emag",
                            "raw_name":     name,
                            "brand":        _extract_brand(name),
                            "category":     category,
                            "category_raw": cat_label,
                            "price":        price,
                            "old_price":    old_price,
                            "discount_pct": disc_pct,
                            "image_url":    p.get("img", ""),
                            "url":          p.get("link", url),
                            "in_stock":     True,
                            "scraped_at":   scraped_at,
                        })

                    logger.info("[emag] %s p%d → %d raw, %d new",
                                cat_label, page, len(raw), new_count)

                    # Stop if pagination returns same page (no new products + same count)
                    if new_count == 0 and len(raw) == prev_raw_count:
                        logger.info("[emag] %s p%d — repeated page, stopping", cat_label, page)
                        break
                    prev_raw_count = len(raw)

                    # If less than 10 products on this page → last page
                    if len(raw) < 10:
                        break

                    time.sleep(random.uniform(1.5, 3.0))

                except Exception as exc:
                    logger.error("[emag] Failed %s p%d: %s", cat_label, page, exc)
                    break

            time.sleep(random.uniform(2.0, 3.5))

    finally:
        driver.quit()

    logger.info("[emag] Total: %d offers across %d categories", len(all_offers), min(max_categories, len(EMAG_CATEGORIES)))
    return all_offers


def _extract_brand(name: str) -> str:
    """
    Try to extract brand from product name.
    eMAG names usually start with the brand: "Samsung Galaxy S24 128GB ..."
    """
    known_brands = [
        "Samsung", "Apple", "Sony", "Huawei", "Xiaomi", "OnePlus", "Google",
        "LG", "Philips", "Bose", "JBL", "Sennheiser", "AKG", "Jabra",
        "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI", "Toshiba",
        "Panasonic", "Hisense", "TCL", "Grundig", "Bosch", "Miele",
        "Nintendo", "Xbox", "PlayStation", "Logitech", "Razer", "SteelSeries",
        "Canon", "Nikon", "Fujifilm", "GoPro", "DJI",
    ]
    name_lower = name.lower()
    for brand in known_brands:
        if name_lower.startswith(brand.lower()) or f" {brand.lower()} " in name_lower:
            return brand
    # Fall back to first word
    return name.split()[0] if name else ""


# ── Supabase save ────────────────────────────────────────────────────
def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    # Delete old eMAG rows
    sb.table("electronics_offers").delete().eq("store", "emag").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i+100]).execute()
        total += len(offers[i:i+100])
    logger.info("[emag] Saved %d offers to Supabase", total)
    return total


def save_to_json(offers: list[dict], filename: str = "emag_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(offers)} offers to {filename}")


# ── CLI ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    headless    = "--show"      not in sys.argv
    test_mode   = "--test"      in sys.argv
    to_supabase = "--supabase"  in sys.argv
    max_cats    = 1 if test_mode else 99
    max_pgs     = 2 if test_mode else 5

    print(f"eMAG scraper — headless={headless}, categories={'1 (test)' if test_mode else 'all'}")
    offers = scrape_emag(headless=headless, max_categories=max_cats, max_pages=max_pgs)

    if offers:
        save_to_json(offers)
        print(f"\n✅ {len(offers)} продукта")
        for o in offers[:5]:
            disc = f" [{o['discount_pct']}%]" if o.get("discount_pct") else ""
            print(f"  {o['raw_name'][:50]:<50} {o['price']:.2f} €{disc} [{o['category']}]")
        if to_supabase:
            saved = save_to_supabase(offers)
            print(f"\n✅ Записани {saved} в Supabase (electronics_offers)")
    else:
        print("❌ Няма продукти — пробвай с --show за да видиш какво се случва")
