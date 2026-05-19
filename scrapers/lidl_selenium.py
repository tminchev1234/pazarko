"""
Lidl Bulgaria scraper — Selenium + JS extraction
Site: https://www.lidl.bg

Lidl Bulgaria не показва HTML оферти на отделна страница — продуктите с намаление
се намират в обичайните категорийни страници. Скреперът:
  1. Отваря Lidl.bg и намира категорийни URL-и
  2. За всяка категория извлича продуктите, у които има задраскана (стара) цена
  3. Спира ако не намери нови намалени продукти в дадена категория

Употреба:
    py -m scrapers.lidl_selenium              # scrape + save JSON
    py -m scrapers.lidl_selenium --supabase   # + upload
    py -m scrapers.lidl_selenium --show       # headful (вижда се прозорец)
"""

from __future__ import annotations
import json
import logging
import re
import time
import random
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.lidl.bg"

# Начални категорийни URL-и (храни и напитки)
# Скреперът динамично ще намери повече от homepage
SEED_CATEGORIES = [
    f"{BASE_URL}/c/khrani-i-napitki/s10068374",
    f"{BASE_URL}/c/broshura/s10020060",
]

# JS: извлича САМО продуктите с намаление (имат задраскана стара цена)
# Структура на Lidl Bulgaria:
#   div.odsc-tile (product card)
#     a.odsc-tile__link (href)
#     div.product-grid-box__title (name)
#     div.ods-price
#       s (old price: "X.XX € (Y.YY лв.)")
#       div.ods-price__value (current EUR: "X.XX€*")
#       div.ods-price__value (current BGN: "X.XX лв.*")
EXTRACT_DISCOUNTED_JS = """
let products = [];
let seen = new Set();

document.querySelectorAll('div.odsc-tile').forEach(card => {
    // Проверяваме дали има задраскана стара цена (= промоция)
    let oldPriceEl = card.querySelector('s, del');
    if (!oldPriceEl) return;

    // Заглавие
    let titleEl = card.querySelector('.product-grid-box__title, .odsc-tile__title, h3, h2');
    let name = titleEl?.textContent?.trim() || '';
    if (!name) return;

    // Текуща цена — вземаме BGN (ods-price__value с "лв")
    let priceRaw = '';
    card.querySelectorAll('.ods-price__value').forEach(el => {
        let t = el.textContent.trim();
        if (t.includes('лв') || t.includes('BGN')) priceRaw = t;
    });
    // Ако нямаме BGN цена, вземаме EUR и конвертираме
    if (!priceRaw) {
        card.querySelectorAll('.ods-price__value').forEach(el => {
            let t = el.textContent.trim();
            if (t.includes('€') || /\\d[,.]\\d{2}/.test(t)) priceRaw = t;
        });
    }
    if (!priceRaw) return;

    // Стара цена — от <s> елемента, извличаме BGN от скоби: "5.87 € (11.48 лв.)"
    let oldPriceRaw = oldPriceEl.textContent.trim();

    // Единица мярка
    let unit = card.querySelector('.ods-footer-item, .ods-price__footer, .product-grid-box__meta')?.textContent?.trim() || '';

    // Снимка
    let imgSrc = card.querySelector('img')?.src || card.querySelector('img')?.getAttribute('data-src') || '';

    // URL
    let link = card.querySelector('a.odsc-tile__link, a[href*="/p/"], a[href]')?.href || '';

    // % отстъпка ако е показана
    let discount = card.querySelector('[class*="discount"], [class*="badge"]')?.textContent?.trim() || '';

    let key = name + '|' + priceRaw;
    if (!seen.has(key)) {
        seen.add(key);
        products.push({ name, priceRaw, oldPriceRaw, unit, imgSrc, link, discount });
    }
});

return JSON.stringify(products);
"""

# JS: диагностика — колко продукта и дали имат намаления
DIAG_JS = """
let cards = [...document.querySelectorAll('div.odsc-tile')];
let withDiscount = cards.filter(c => c.querySelector('s, del')).length;
return JSON.stringify({
    total: cards.length,
    withDiscount,
    url: window.location.href,
    title: document.title.substring(0, 60),
});
"""

# JS: намира категорийни линкове на текущата страница
FIND_CATEGORY_LINKS_JS = """
let links = new Set();
document.querySelectorAll('a[href*="/c/"], a[href*="/h/"]').forEach(a => {
    let href = a.href;
    if (href && !href.includes('#') && href.includes('lidl.bg')) {
        links.add(href);
    }
});
return JSON.stringify([...links].slice(0, 30));
"""


EUR_TO_BGN = 1.9558

def _parse_price(text: Optional[str], prefer_bgn: bool = True) -> Optional[float]:
    """
    Разбира различни формати:
    '9.09лв.*'             → 9.09
    '4.65€*'               → 9.09 лв. (конвертирано)
    '5.87 € (11.48 лв.)'  → 11.48 (BGN от скоби)
    '11.48 лв.'            → 11.48
    """
    if not text:
        return None
    text = text.strip()

    # BGN в скоби: "5.87 € (11.48 лв.)" → 11.48
    if prefer_bgn:
        m_bgn_paren = re.search(r"\((\d+[,.]\d+)\s*(?:лв|BGN)", text, re.IGNORECASE)
        if m_bgn_paren:
            try:
                return round(float(m_bgn_paren.group(1).replace(",", ".")), 2)
            except ValueError:
                pass

    # Директно BGN: "9.09 лв.*"
    m_bgn = re.search(r"(\d+[,.]\d+)\s*(?:лв|BGN)", text, re.IGNORECASE)
    if m_bgn:
        try:
            return round(float(m_bgn.group(1).replace(",", ".")), 2)
        except ValueError:
            pass

    # EUR → BGN: "4.65€*"
    m_eur = re.search(r"(\d+[,.]\d+)\s*[€Ee]", text)
    if m_eur:
        try:
            return round(float(m_eur.group(1).replace(",", ".")) * EUR_TO_BGN, 2)
        except ValueError:
            pass

    # Само число: "4.65"
    m_num = re.search(r"(\d+)[,.](\d{2})", text)
    if m_num:
        try:
            return round(float(m_num.group(1) + "." + m_num.group(2)), 2)
        except ValueError:
            pass

    return None


def _extract_products(driver, scraped_at: str) -> list[dict]:
    """Пуска EXTRACT_DISCOUNTED_JS и нормализира резултата."""
    try:
        raw = json.loads(driver.execute_script(EXTRACT_DISCOUNTED_JS) or "[]")
    except Exception as exc:
        logger.warning("[lidl] JS error: %s", exc)
        return []

    products = []
    seen: set[str] = set()

    for p in raw:
        name  = (p.get("name") or "").strip()
        if not name or len(name) < 3:
            continue

        price     = _parse_price(p.get("priceRaw"),    prefer_bgn=True)
        old_price = _parse_price(p.get("oldPriceRaw"), prefer_bgn=True)

        if not price:
            continue

        # Lidl badge может да съдържа дати ("В наличност от 11.05. - 07.06.")
        # Винаги изчисляваме % от цените
        discount = ""
        if old_price and old_price > price:
            pct      = round((old_price - price) / old_price * 100)
            discount = f"-{pct}%"

        unit_src = name + " " + p.get("unit", "")
        unit_m   = re.search(r"\b(\d+[\.,]?\d*\s*(?:г|кг|мл|л|бр|пак|рол))\b",
                             unit_src, re.IGNORECASE)
        unit = unit_m.group(1) if unit_m else p.get("unit", "")

        key = f"{name}|{price}"
        if key not in seen:
            seen.add(key)
            products.append({
                "store":        "lidl",
                "raw_name":     name,
                "brand":        name.split()[0] if name else "",
                "description":  "",
                "price":        price,
                "old_price":    old_price if old_price and old_price > price else None,
                "discount":     discount,
                "unit":         unit,
                "image_url":    p.get("imgSrc", ""),
                "url":          p.get("link", ""),
                "category_raw": "Lidl оферти",
                "scraped_at":   scraped_at,
            })

    return products


def scrape_lidl(headless: bool = True) -> list[dict]:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
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

    all_products: list[dict] = []
    seen_global:  set[str]   = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )

    try:
        # Стъпка 1: Homepage — приемаме бисквитки
        logger.info("[lidl] Opening homepage...")
        driver.get(BASE_URL)
        time.sleep(3)

        for sel in (
            "button#onetrust-accept-btn-handler",
            "button[data-selector='onetrust-accept-btn-handler']",
            ".cookie-alert-extended-button",
        ):
            try:
                driver.find_element(By.CSS_SELECTOR, sel).click()
                logger.info("[lidl] Accepted cookies via '%s'", sel)
                time.sleep(1)
                break
            except Exception:
                pass

        # Намираме категорийни URL-и на homepage
        cat_links_raw = json.loads(driver.execute_script(FIND_CATEGORY_LINKS_JS) or "[]")
        logger.info("[lidl] Found %d category links on homepage", len(cat_links_raw))

        # Избираме хранителни и домакински категории; добавяме seed-овете
        food_keywords = [
            "khrani", "plodove", "meso", "mleko", "napitki", "zamr", "khlyab",
            "ribni", "mlech", "yaitsa", "kolbasi", "sladko", "konditor",
            "gotovi", "snaksove", "sosove", "podpravki",
        ]
        cat_urls = list(SEED_CATEGORIES)
        for url in cat_links_raw:
            if any(kw in url.lower() for kw in food_keywords) and url not in cat_urls:
                cat_urls.append(url)

        logger.info("[lidl] Will check %d category URLs", len(cat_urls))

        # Стъпка 2: Обхождаме категориите
        for url in cat_urls:
            logger.info("[lidl] Checking: %s", url)
            try:
                driver.get(url)
                time.sleep(4)

                # Скролираме за lazy-load
                for _ in range(6):
                    driver.execute_script("window.scrollBy(0, 900)")
                    time.sleep(0.4)
                driver.execute_script("window.scrollTo(0, 0)")
                time.sleep(1)

                # Диагностика
                diag = json.loads(driver.execute_script(DIAG_JS) or "{}")
                logger.info("[lidl] %s — cards: %d, with discount: %d",
                            diag.get("title", url), diag.get("total", 0), diag.get("withDiscount", 0))

                if diag.get("withDiscount", 0) == 0:
                    logger.info("[lidl] No discounted products on this page — skipping")
                    continue

                prods = _extract_products(driver, scraped_at)
                new_count = 0
                for p in prods:
                    key = f"{p['raw_name']}|{p['price']}"
                    if key not in seen_global:
                        seen_global.add(key)
                        all_products.append(p)
                        new_count += 1

                logger.info("[lidl] %d new discounted products (total: %d)", new_count, len(all_products))

            except Exception as exc:
                logger.error("[lidl] Error on %s: %s", url, exc)
                continue

            time.sleep(random.uniform(2, 3))

    finally:
        driver.quit()

    logger.info("[lidl] Done: %d discounted products", len(all_products))
    return all_products


def save_to_supabase(products: list[dict]) -> int:
    from api.db import get_supabase_admin
    if not products:
        return 0
    sb = get_supabase_admin()
    sb.table("kaufland_offers").delete().eq("store", "lidl").execute()
    total = 0
    for i in range(0, len(products), 50):
        sb.table("kaufland_offers").insert(products[i:i+50]).execute()
        total += len(products[i:i+50])
    logger.info("[lidl] Saved %d products", total)
    return total


def save_to_json(products: list[dict], filename: str = "lidl_offers.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(products)} products to {filename}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    headless    = "--show"      not in sys.argv
    to_supabase = "--supabase" in sys.argv

    print("Scraping Lidl Bulgaria promotions...")
    products = scrape_lidl(headless=headless)

    if products:
        save_to_json(products)
        print(f"\n[OK] {len(products)} produkTA")
        for p in products[:5]:
            old = f"  (beshe {p['old_price']:.2f} lv.)" if p.get("old_price") else ""
            print(f"  {p['raw_name']:<50} {p['price']:.2f} lv.{old}")
        if to_supabase:
            saved = save_to_supabase(products)
            print(f"[OK] Zapisani {saved} v Supabase")
    else:
        print("[X] Nyama produkti")
        print("   Probvay: py -m scrapers.lidl_selenium --show")
