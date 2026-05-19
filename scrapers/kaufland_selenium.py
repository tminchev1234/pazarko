"""
Kaufland Bulgaria scraper — Selenium based
Извлича актуалните оферти от kaufland.bg чрез JavaScript execution

Инсталация: py -m pip install selenium
ChromeDriver: автоматично се управлява от selenium >= 4.6
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

# ── категории на Kaufland (от URL параметъра kloffer-category) ────────────────
KAUFLAND_CATEGORIES = [
    ("0001_TopArticle",            "Топ оферти"),
    ("02_Plodove_i_zelenchuczi",   "Плодове и зеленчуци"),
    ("03_Млечни_продукти",         "Млечни продукти"),
    ("04_Meso_ptiche_meso",        "Месо и птиче месо"),
    ("05_Ryba_i_morski",           "Риба и морски дарове"),
    ("06_Zamrazeni",               "Замразени продукти"),
    ("07_Hlqb_i_peciva",           "Хляб и печива"),
    ("08_Napitki",                 "Напитки"),
    ("09_Suhi_hrani",              "Сухи храни и консерви"),
    ("10_Sladkishi",               "Сладкиши и снаксове"),
    ("0002_K-Card",                "K-Card оферти"),
]

# JavaScript за извличане на продукти от рендерираната страница
EXTRACT_JS = """
let seen = new Set();
let products = [];

document.querySelectorAll('[class*="k-product-tile"], [class*="k-offer-tile"]').forEach(el => {
    let title    = el.querySelector('[class*="title"]')?.textContent?.trim();
    let subtitle = el.querySelector('[class*="subtitle"], [class*="description"]')?.textContent?.trim();
    let priceLv  = [...el.querySelectorAll('*')].find(
        e => e.textContent?.includes('ЛВ.') && e.children.length === 0
    )?.textContent?.trim();
    let priceEur = [...el.querySelectorAll('*')].find(
        e => e.textContent?.match(/\\d[,.]\\d{2}\\s*€/) && e.children.length === 0
    )?.textContent?.trim();
    let oldPrice = [...el.querySelectorAll('[class*="old"], [class*="before"], s, del')].find(
        e => e.children.length === 0
    )?.textContent?.trim();
    let discount = el.querySelector('[class*="discount"], [class*="badge"], [class*="saving"]')?.textContent?.trim();
    let weight   = el.querySelector('[class*="quantity"], [class*="unit"], [class*="weight"], [class*="grammage"]')?.textContent?.trim();
    let imgSrc   = el.querySelector('img[src*="kaufland"], img[src*="schwarz"]')?.src;
    let link     = el.querySelector('a[href]')?.href;

    // Deduplicate by title+priceLv
    let key = (title || '') + '|' + (priceLv || '');
    if (title && priceLv && !seen.has(key)) {
        seen.add(key);
        products.push({ title, subtitle, priceLv, priceEur, oldPrice, discount, weight, imgSrc, link });
    }
});
return JSON.stringify(products);
"""


def parse_price(text: Optional[str]) -> Optional[float]:
    """'0,72 ЛВ.' → 0.72  |  '1.29 лв.' → 1.29  |  '3.50.' → 3.50"""
    if not text:
        return None
    # Keep only digits, comma, period
    cleaned = re.sub(r"[^\d,.]", "", text).replace(",", ".")
    # Remove trailing/leading dots and collapse multiple dots
    # e.g. "0.72." → ["0", "72", ""] → "0.72"
    parts = [p for p in cleaned.split(".") if p.isdigit() or (p and p[0].isdigit())]
    if not parts:
        return None
    if len(parts) >= 2:
        cleaned = parts[0] + "." + parts[1]
    else:
        cleaned = parts[0]
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def scrape_kaufland(headless: bool = True, max_categories: int = 99) -> list[dict]:
    """
    Scrape всички категории от Kaufland offers page.
    Returns list of product dicts ready for Supabase insert.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = webdriver.Chrome(options=options)
    all_products: list[dict] = []
    seen_global: set[str] = set()   # dedup across all categories
    scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        for i, (cat_code, cat_name) in enumerate(KAUFLAND_CATEGORIES[:max_categories]):
            url = f"https://www.kaufland.bg/aktualni-predlozheniya/oferti.html?kloffer-category={cat_code}"
            logger.info("[kaufland] Scraping: %s (%s)", cat_name, cat_code)

            try:
                driver.get(url)
                # Изчакваме продуктите да заредят
                time.sleep(random.uniform(3.5, 5.5))

                # Scroll down да заредят lazy images
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2)")
                time.sleep(1)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)

                result = driver.execute_script(EXTRACT_JS)
                raw_products = json.loads(result) if result else []

                new_this_cat = 0
                for p in raw_products:
                    price = parse_price(p.get("priceLv"))
                    if not price:
                        continue

                    # Global dedup — Kaufland loads ALL products in DOM every page
                    dedup_key = f"{p.get('title', '')}|{price}"
                    if dedup_key in seen_global:
                        continue
                    seen_global.add(dedup_key)
                    new_this_cat += 1

                    all_products.append({
                        "store":        "kaufland",
                        "raw_name":     f"{p.get('title', '')} {p.get('subtitle', '')}".strip(),
                        "brand":        p.get("title", ""),
                        "description":  p.get("subtitle", ""),
                        "price":        price,
                        "old_price":    parse_price(p.get("oldPrice")),
                        "discount":     p.get("discount", ""),
                        "unit":         p.get("weight", ""),
                        "image_url":    p.get("imgSrc", ""),
                        "url":          p.get("link", url),
                        "category_raw": cat_name,
                        "scraped_at":   scraped_at,
                    })

                logger.info("[kaufland] %s → %d raw, %d new", cat_name, len(raw_products), new_this_cat)

                # If all products are already seen, no point continuing
                if new_this_cat == 0 and i > 0:
                    logger.info("[kaufland] No new products — skipping remaining categories")
                    break

            except Exception as exc:
                logger.error("[kaufland] Failed %s: %s", cat_name, exc)
                continue

            # Пауза между категории
            time.sleep(random.uniform(2, 4))

    finally:
        driver.quit()

    logger.info("[kaufland] Total: %d products", len(all_products))
    return all_products


def save_to_supabase(products: list[dict]) -> int:
    """Записва продуктите директно в Supabase offers таблица."""
    from api.db import get_supabase_admin
    if not products:
        return 0

    sb = get_supabase_admin()
    # Изтриваме само Kaufland оферти (не засягаме другите магазини!)
    sb.table("kaufland_offers").delete().eq("store", "kaufland").execute()

    # Insert на групи от 50
    total = 0
    for i in range(0, len(products), 50):
        batch = products[i:i+50]
        sb.table("kaufland_offers").insert(batch).execute()
        total += len(batch)

    logger.info("[kaufland] Saved %d products to Supabase", total)
    return total


def save_to_json(products: list[dict], filename: str = "kaufland_offers.json"):
    """Записва в JSON файл за тест."""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    headless    = "--show"      not in sys.argv
    max_cats    = 2            if "--test"     in sys.argv else 99
    to_supabase = "--supabase" in sys.argv

    print(f"Starting Kaufland scraper (headless={headless}, max_categories={max_cats})...")
    products = scrape_kaufland(headless=headless, max_categories=max_cats)

    if products:
        save_to_json(products)
        print(f"\n✅ Scraped {len(products)} products")
        print("\nSample:")
        for p in products[:5]:
            print(f"  {p['raw_name']:<45} {p['price']:.2f} лв.  [{p['category_raw']}]")

        if to_supabase:
            print("\nUploading to Supabase...")
            saved = save_to_supabase(products)
            print(f"✅ Saved {saved} products to Supabase kaufland_offers table")
    else:
        print("❌ No products found")
