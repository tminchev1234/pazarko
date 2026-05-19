"""
Fantastico Bulgaria scraper — Selenium + SSB text parser
SSB страница: https://www.fantastico.bg/special-offers/ssb/fantastiko-broshura

Страницата е достъпна (screen-reader) версия на брошурата с ~12 MB текст.
Съдържа всички продукти с цени в tab-separated формат:
  №: N
  Продукт: ИМЕ НА ПРОДУКТА
  МЕ\tЦвят цена\t...headers...
  бр\tЗелен\tПредишна цена\tOLD_EUR\tевро\tQTY\tнова цена\tNEW_EUR\tевро\tNEW_BGN\tлева\tUNIT\tDISCOUNT

Употреба:
    py -m scrapers.fantastico_selenium              # scrape + save JSON
    py -m scrapers.fantastico_selenium --supabase   # + upload
    py -m scrapers.fantastico_selenium --show       # headful
"""

from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.fantastico.bg"
OFFERS_URL = f"{BASE_URL}/special-offers"
SSB_URL    = f"{BASE_URL}/special-offers/ssb/fantastiko-broshura"

EUR_TO_BGN = 1.9558

# JavaScript: парсира SSB страницата директно в браузъра и връща продуктите
PARSE_SSB_JS = r"""
const EUR_TO_BGN = 1.9558;
let products = [];
let seen = new Set();

let text = document.body.innerText || document.body.textContent || '';

// Split into product blocks — всеки блок започва с "№: N"
let blocks = text.split(/\n(?=№:\s*\d)/);

for (let block of blocks) {
    // Product name
    let nameMatch = block.match(/Продукт:\s*(.+)/);
    if (!nameMatch) continue;
    let name = nameMatch[1].trim();
    if (!name || name.length < 3) continue;

    // Price data line:
    // unit\tcolor\tПредишна цена\tOLD_EUR\tевро\tQTY\tнова цена\tNEW_EUR\tевро\tNEW_BGN\tлева\tUNIT\tDISCOUNT\t...
    // We look for the specific pattern with евро and лева
    let priceMatch = block.match(
        /\n\S+\t\S+\tПредишна цена\t([\d.]+)\tевро\t\d+\tнова цена\t([\d.]+)\tевро\t([\d.]+)\tлева\t(\S*)\t(-?\d+%)/
    );
    if (!priceMatch) continue;

    let oldPriceEur = parseFloat(priceMatch[1]);
    let newPriceEur = parseFloat(priceMatch[2]);
    let newPriceBgn = parseFloat(priceMatch[3]);  // BGN price, directly stated
    let unit        = priceMatch[4] || '';
    let discount    = priceMatch[5];              // e.g. "-31%" or "0%"

    if (isNaN(newPriceBgn) || newPriceBgn <= 0) continue;

    // Old price in BGN — not in text, calculate from EUR
    let oldPriceBgn = Math.round(oldPriceEur * EUR_TO_BGN * 100) / 100;

    // Clean unit (брой → бр, etc.)
    let unitClean = unit.replace('брой', 'бр').trim();

    let key = name + '|' + newPriceBgn;
    if (seen.has(key)) continue;
    seen.add(key);

    products.push({
        name:       name,
        price:      newPriceBgn,
        oldPrice:   (discount !== '0%') ? oldPriceBgn : null,
        discount:   (discount !== '0%') ? discount : '',
        unit:       unitClean,
    });
}

return JSON.stringify(products);
"""


def _normalize_products(raw: list[dict], scraped_at: str) -> list[dict]:
    import re
    products = []
    for p in raw:
        name = p.get("name", "").strip()
        price = p.get("price")
        if not name or not price:
            continue

        # Convert CAPS → Title Case (РИБА ТОН → Риба Тон)
        name_clean = name.title()

        unit_m = re.search(r"\b(\d+[\.,]?\d*\s*(?:г|кг|мл|л|бр|пак|рол))\b", name, re.IGNORECASE)
        unit = unit_m.group(1) if unit_m else p.get("unit", "")

        products.append({
            "store":        "fantastico",
            "raw_name":     name_clean,
            "brand":        name_clean.split()[0] if name_clean else "",
            "description":  "",
            "price":        round(float(price), 2),
            "old_price":    round(float(p["oldPrice"]), 2) if p.get("oldPrice") else None,
            "discount":     p.get("discount", ""),
            "unit":         unit,
            "image_url":    "",
            "url":          OFFERS_URL,
            "category_raw": "Фантастико брошура",
            "scraped_at":   scraped_at,
        })

    logger.info("[fantastico] Normalized %d products", len(products))
    return products


def scrape_fantastico(headless: bool = True) -> list[dict]:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

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
    opts.add_experimental_option("useAutomationExtension", False)

    scraped_at = datetime.now(timezone.utc).isoformat()

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )

    try:
        # Step 1: set cookies on main domain
        logger.info("[fantastico] Setting session cookies on %s", BASE_URL)
        driver.get(BASE_URL)
        time.sleep(3)

        # Accept cookie consent
        for sel in ("button#fantastico_cookie", "button[class*='accept']"):
            try:
                driver.find_element(By.CSS_SELECTOR, sel).click()
                logger.info("[fantastico] Accepted cookies")
                time.sleep(1)
                break
            except Exception:
                pass

        # Step 2: navigate to SSB page
        logger.info("[fantastico] Loading SSB page: %s", SSB_URL)
        driver.get(SSB_URL)

        # SSB page is large (~12 MB) — wait for it to fully load
        logger.info("[fantastico] Waiting for page to load (large page)...")
        time.sleep(10)

        # Verify page loaded
        body_len = driver.execute_script("return document.body.innerHTML.length")
        logger.info("[fantastico] Body size: %d chars", body_len)

        if body_len < 100_000:
            logger.error("[fantastico] Page too small (%d chars) — possible block", body_len)
            return []

        # Step 3: parse products in-browser via JS
        logger.info("[fantastico] Parsing products via JS...")
        result = driver.execute_script(PARSE_SSB_JS)
        raw = json.loads(result) if result else []
        logger.info("[fantastico] JS parser found %d raw products", len(raw))

    finally:
        driver.quit()

    return _normalize_products(raw, scraped_at)


def save_to_supabase(products: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0
    sb = get_supabase_admin()
    sb.table("kaufland_offers").delete().eq("store", "fantastico").execute()
    total = 0
    for i in range(0, len(products), 50):
        sb.table("kaufland_offers").insert(products[i:i+50]).execute()
        total += len(products[i:i+50])
    logger.info("[fantastico] Saved %d products", total)
    return total


def save_to_json(products: list[dict], filename: str = "fantastico_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    headless    = "--show"      not in sys.argv
    to_supabase = "--supabase" in sys.argv

    print("Scraping Fantastico Bulgaria brochure...")
    products = scrape_fantastico(headless=headless)

    if products:
        save_to_json(products)
        print(f"\n[OK] {len(products)} produkta")
        for p in products[:5]:
            old = f"  (beshe {p['old_price']:.2f} lv.)" if p.get("old_price") else ""
            disc = f"  [{p['discount']}]" if p.get("discount") else ""
            print(f"  {p['raw_name'][:50]:<50} {p['price']:.2f} lv.{old}{disc}")
        if to_supabase:
            saved = save_to_supabase(products)
            print(f"[OK] Zapisani {saved} v Supabase")
    else:
        print("[X] Nyama produkti — probvay s --show")
