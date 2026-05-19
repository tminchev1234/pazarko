"""
Billa Bulgaria scraper — Selenium based
Извлича промоциите от billa.bg/promocii/sedmichna-broshura

Употреба:
    py -m scrapers.billa_selenium          # scrape + save JSON
    py -m scrapers.billa_selenium --supabase  # + upload
    py -m scrapers.billa_selenium --show      # видим браузър
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

BILLA_OFFERS_URL = "https://www.billa.bg/promocii/sedmichna-broshura"

# JavaScript за извличане на Billa продукти
EXTRACT_JS = """
let seen = new Set();
let products = [];

// Опитваме различни селектори за Billa продуктови карти
let cards = [
  ...document.querySelectorAll('.promotion-item'),
  ...document.querySelectorAll('.product-promo'),
  ...document.querySelectorAll('[class*="promotion"][class*="item"]'),
  ...document.querySelectorAll('[class*="product"][class*="card"]'),
  ...document.querySelectorAll('[class*="promo-product"]'),
  ...document.querySelectorAll('article[class*="product"]'),
  ...document.querySelectorAll('.card-promo'),
  ...document.querySelectorAll('[data-product-id]'),
];

// Премахваме дублиращи се елементи
let uniqueCards = [...new Set(cards)];

uniqueCards.forEach(el => {
  // Намираме текста на елементите
  let allText = el.innerText || '';
  if (!allText.trim()) return;

  // Опит за извличане на цени
  let priceMatch = allText.match(/(\\d+[,.]\\d{2})\\s*(лв|лева|BGN|bll)/i);
  if (!priceMatch) {
    priceMatch = allText.match(/(\\d+[,.]\\d{2})/);
  }

  let title = el.querySelector('[class*="name"], [class*="title"], h2, h3, h4, p')?.textContent?.trim();
  let link = el.querySelector('a')?.href;
  let img  = el.querySelector('img')?.src;

  let priceLv = priceMatch ? priceMatch[1] : null;

  if (!title || !priceLv) return;

  let key = title + '|' + priceLv;
  if (seen.has(key)) return;
  seen.add(key);

  products.push({ title, priceLv, imgSrc: img || '', link: link || '' });
});

return JSON.stringify(products);
"""


def parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.]", "", text).replace(",", ".")
    parts = [p for p in cleaned.split(".") if p]
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


def scrape_billa(headless: bool = True) -> list[dict]:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

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
    products = []
    scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        logger.info("[billa] Loading %s", BILLA_OFFERS_URL)
        driver.get(BILLA_OFFERS_URL)

        # Изчакваме зареждане
        time.sleep(random.uniform(4, 6))

        # Scroll за lazy-load
        for scroll_pct in [0.3, 0.6, 1.0]:
            driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {scroll_pct})")
            time.sleep(1)

        # Логваме какво има в DOM-а за диагностика
        page_text = driver.execute_script("return document.body.innerText.substring(0, 2000)")
        logger.info("[billa] Page text preview: %s", page_text[:500])

        # Броим карти с различни селектори
        for sel in ['.promotion-item', '.product-promo', '[class*="promo"]', '[data-product-id]', 'article']:
            count = driver.execute_script(f"return document.querySelectorAll('{sel}').length")
            if count > 0:
                logger.info("[billa] Found %d elements with selector: %s", count, sel)

        result = driver.execute_script(EXTRACT_JS)
        raw = json.loads(result) if result else []
        logger.info("[billa] JS extracted %d raw items", len(raw))

        for p in raw:
            price = parse_price(p.get("priceLv"))
            if not price:
                continue
            products.append({
                "store":        "billa",
                "raw_name":     p.get("title", ""),
                "brand":        "",
                "description":  "",
                "price":        price,
                "old_price":    None,
                "discount":     "",
                "unit":         "",
                "image_url":    p.get("imgSrc", ""),
                "url":          p.get("link", BILLA_OFFERS_URL),
                "category_raw": "Billa оферти",
                "scraped_at":   scraped_at,
            })

    finally:
        driver.quit()

    logger.info("[billa] Total: %d products", len(products))
    return products


def save_to_supabase(products: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0
    sb = get_supabase_admin()
    sb.table("kaufland_offers").delete().eq("store", "billa").execute()
    total = 0
    for i in range(0, len(products), 50):
        sb.table("kaufland_offers").insert(products[i:i+50]).execute()
        total += len(products[i:i+50])
    logger.info("[billa] Saved %d products", total)
    return total


def save_to_json(products: list[dict], filename: str = "billa_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    headless    = "--show"     not in sys.argv
    to_supabase = "--supabase" in sys.argv

    print(f"Starting Billa scraper (headless={headless})...")
    products = scrape_billa(headless=headless)

    if products:
        save_to_json(products)
        print(f"\n✅ Scraped {len(products)} products")
        for p in products[:5]:
            print(f"  {p['raw_name']:<50} {p['price']:.2f} лв.")
        if to_supabase:
            saved = save_to_supabase(products)
            print(f"✅ Saved {saved} to Supabase")
    else:
        print("❌ No products found")
        print("\nНужно е да инспектираш billa.bg ръчно:")
        print("  1. Отвори https://www.billa.bg/promocii/sedmichna-broshura в Chrome")
        print("  2. F12 → Console → document.querySelectorAll('...') ")
        print("  3. Кажи ми какви класове имат продуктовите карти")
