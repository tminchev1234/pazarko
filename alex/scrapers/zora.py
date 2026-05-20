"""
Zora Bulgaria scraper — Selenium
Site: https://zora.bg

Употреба:
  py -m alex.scrapers.zora                 # scrape + JSON
  py -m alex.scrapers.zora --supabase      # + Supabase
  py -m alex.scrapers.zora --test          # 1 категория, 2 стр.
  py -m alex.scrapers.zora --show          # headful (вижда се браузъра)
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

BASE_URL = "https://zora.bg"

ZORA_CATEGORIES = [
    # ── Електроника ──────────────────────────────────────────────────
    ("/category/smartfoni",              "phones",      "Смартфони"),
    ("/category/laptopi",                "laptops",     "Лаптопи"),
    ("/category/tableti2",               "tablets",     "Таблети"),
    ("/category/televizori",             "tvs",         "Телевизори"),
    ("/category/slusalki",               "headphones",  "Слушалки"),
    ("/category/igrovi-konzoli",         "gaming",      "Конзоли"),
    ("/category/fotoaparati",            "cameras",     "Фотоапарати"),
    # ── Бяла техника (специфични първо) ──────────────────────────────
    ("/category/khladilnitsi",           "fridges",     "Хладилници"),
    ("/category/peralni-masini",         "washing",     "Перални машини"),
    ("/category/susilni",                "washing",     "Сушилни"),
    ("/category/gotvarski-pecki",        "cooking",     "Готварски печки"),
    ("/category/mikrovalnovi-furni",     "cooking",     "Микровълнови"),
    ("/category/prakhosmukachki",        "vacuum",      "Прахосмукачки"),
    ("/category/roboti-prakhosmukachki", "vacuum",      "Роботи-прахосмукачки"),
    ("/category/mialni",                 "dishwasher",  "Съдомиялни"),
    ("/category/invertorni-sistemi",     "ac",          "Климатици"),
]

# JS run in browser to extract product cards
EXTRACT_JS = r"""
const products = [];
const seen = new Set();

const cards = Array.from(document.querySelectorAll('div._product[data-box="product"]'));

cards.forEach(card => {
    // Name + URL
    const nameEl = card.querySelector('div._product-name h3 a');
    if (!nameEl) return;
    const name = (nameEl.textContent || '').trim();
    if (!name || name.length < 4) return;
    const link = nameEl.href || '';

    // Image — lazy-loaded, real URL in data-src
    const imgEl = card.querySelector('img.lazyload-image');
    let img = '';
    if (imgEl) {
        img = imgEl.dataset.src || imgEl.src || '';
        if (img.includes('/logo/')) img = '';  // skip placeholder
    }

    // Current price — first bgn2eur-primary-currency NOT inside <del>
    const priceBox = card.querySelector('div._product-price');
    if (!priceBox) return;
    let price = '';
    const allSpans = priceBox.querySelectorAll('span.bgn2eur-primary-currency');
    for (const span of allSpans) {
        if (!span.closest('del')) {
            price = span.textContent.trim();
            break;
        }
    }
    if (!price) return;

    // Old price — inside <del>
    const oldDel = priceBox.querySelector('del span.bgn2eur-primary-currency');
    const oldPrice = oldDel ? oldDel.textContent.trim() : '';

    const key = name + '|' + price;
    if (seen.has(key)) return;
    seen.add(key);

    products.push({ name, price, oldPrice, img, link });
});

return JSON.stringify(products);
"""


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = text.strip()
    cleaned = re.sub(r"[^\d\s,.]", "", t)
    cleaned = cleaned.replace("\xa0", "").replace(" ", "")
    cleaned = re.sub(r"(\d)\s+(\d)", r"\1\2", cleaned)
    cleaned = cleaned.replace(",", ".")
    parts = cleaned.split(".")
    if len(parts) >= 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1][:2]
    try:
        return round(float(cleaned.strip()), 2) if cleaned.strip() else None
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
        "AEG", "Candy", "Hotpoint", "Beko", "Ariston", "Liebherr",
        "Nintendo", "Logitech", "Razer",
        "Canon", "Nikon", "Fujifilm", "GoPro",
        "Dyson", "Rowenta", "Karcher", "Bissell", "iRobot",
        "Daikin", "Mitsubishi", "Gree", "Aux", "Haier",
    ]
    # Zora names often start with product type in BG ("Смартфон Samsung ..."),
    # so search across first 4 words, not just first word.
    words = name.split()
    low_words = [w.lower() for w in words[:4]]
    for b in known:
        b_low = b.lower()
        for i, w in enumerate(low_words):
            if w.startswith(b_low) or b_low.startswith(w) and len(w) >= 3:
                return b
    return words[0] if words else ""


def scrape_zora(headless: bool = True, max_categories: int = 99,
                max_pages: int = 12) -> list[dict]:
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
    driver = webdriver.Chrome(service=service, options=opts)
    all_offers: list[dict] = []
    seen_global: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        # Accept cookies / establish session on homepage
        driver.get(BASE_URL)
        time.sleep(random.uniform(2.0, 3.0))

        for i, (path, category, cat_label) in enumerate(ZORA_CATEGORIES[:max_categories]):
            logger.info("[zora] Category: %s (%s)", cat_label, path)

            prev_raw_count = -1
            for page in range(1, max_pages + 1):
                url = BASE_URL + path
                if page > 1:
                    url = f"{BASE_URL}{path}?page={page}"

                try:
                    driver.get(url)
                    time.sleep(random.uniform(2.5, 4.0))

                    # Scroll to trigger lazy loading
                    for scroll_pct in (0.3, 0.6, 1.0):
                        driver.execute_script(
                            f"window.scrollTo(0, document.body.scrollHeight * {scroll_pct})"
                        )
                        time.sleep(0.6)

                    result = driver.execute_script(EXTRACT_JS)
                    raw = json.loads(result) if result else []

                    if not raw:
                        logger.info("[zora] %s p%d — no products, stopping", cat_label, page)
                        break

                    # Repeated-page detection
                    raw_count = len(raw)
                    if raw_count == prev_raw_count and page > 1:
                        logger.info("[zora] %s p%d — repeated page, stopping", cat_label, page)
                        break
                    prev_raw_count = raw_count

                    new_count = 0
                    for p in raw:
                        price = _parse_price(p.get("price"))
                        if not price:
                            continue
                        name = (p.get("name") or "").strip()
                        key = f"{name}|{price}"
                        if key in seen_global:
                            continue
                        seen_global.add(key)
                        new_count += 1

                        old_price = _parse_price(p.get("oldPrice"))
                        disc_pct = _discount(price, old_price)

                        all_offers.append({
                            "store":        "zora",
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

                    logger.info("[zora] %s p%d -> %d raw, %d new",
                                cat_label, page, len(raw), new_count)

                    if new_count == 0 and page > 1:
                        break

                except Exception as exc:
                    logger.warning("[zora] %s p%d error: %s", cat_label, page, exc)
                    break

    finally:
        driver.quit()

    logger.info("[zora] Total: %d offers", len(all_offers))
    return all_offers


def save_to_supabase(offers: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", "zora").execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i + 100]).execute()
        total += len(offers[i:i + 100])
    logger.info("[zora] Saved %d", total)
    return total


def save_to_json(offers: list[dict], filename: str = "zora_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print("Saved " + str(len(offers)) + " offers -> " + filename)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    test_mode   = "--test"     in sys.argv
    show_mode   = "--show"     in sys.argv
    to_supabase = "--supabase" in sys.argv

    offers = scrape_zora(
        headless=not show_mode,
        max_categories=1 if test_mode else 99,
        max_pages=2 if test_mode else 12,
    )

    if offers:
        save_to_json(offers)
        if to_supabase:
            save_to_supabase(offers)
    else:
        print("No offers scraped.")
