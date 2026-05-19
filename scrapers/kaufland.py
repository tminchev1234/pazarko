"""
Kaufland Bulgaria scraper
Site: https://www.kaufland.bg/produkti/

Structure (as of 2025):
  /produkti/<category>/  → listing pages with pagination
  Each page has product cards with: name, price, unit price, image

Uses: BeautifulSoup + httpx (no Selenium needed for listing pages)
"""

from __future__ import annotations
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup, Tag
from scrapers.base import BaseScraper, RawProduct

logger = logging.getLogger(__name__)


# Category map: URL slug → Bulgarian display name
KAUFLAND_CATEGORIES = {
    "mlqko-i-mlqchni-produkti":         "Мляко и млечни продукти",
    "meso-ribi-i-delikatesi":            "Месо, риба и деликатеси",
    "hlyab-i-peciva":                    "Хляб и печива",
    "plo-dove-i-zelenchutsi":            "Плодове и зеленчуци",
    "napitki":                           "Напитки",
    "zamrazeni-hrani":                   "Замразени храни",
    "kafe-chai-i-kakao":                 "Кафе, чай и какао",
    "kshteri-zakharo-i-chokol":          "Захарни и шоколадови",
    "konservi-i-bur-":                   "Консерви и бурканчета",
    "pasta-orizh-i-zhito":              "Паста, ориз и жито",
    "masla-i-sosynyi":                  "Масла и сосове",
    "chipsove-i-zakuski":               "Чипс и закуски",
    "detski-khrani":                    "Детски храни",
    "higiena-i-grizha-za-tyaloto":      "Хигиена и грижа за тялото",
    "domakinski-produkti":              "Домакинство",
}


class KauflandScraper(BaseScraper):
    store_id   = "kaufland"
    store_name = "Kaufland"
    base_url   = "https://www.kaufland.bg"

    MIN_DELAY = 2.0
    MAX_DELAY = 4.0

    def get_category_urls(self) -> List[tuple[str, str]]:
        urls = []
        for slug, name in KAUFLAND_CATEGORIES.items():
            url = f"{self.base_url}/produkti/{slug}/"
            urls.append((url, name))
        return urls

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_total_pages(self, soup: BeautifulSoup) -> int:
        """Find total number of pagination pages."""
        # Kaufland uses: <span class="m-pagination__total">Страница 1 от 12</span>
        total_el = soup.select_one(".m-pagination__total, [data-total-pages]")
        if not total_el:
            return 1
        m = re.search(r"(\d+)\s*$", total_el.get_text())
        return int(m.group(1)) if m else 1

    def _parse_product_card(self, card: Tag, category_name: str) -> Optional[RawProduct]:
        """Parse a single product card element into RawProduct."""
        try:
            # Name
            name_el = card.select_one(
                ".m-product-tile__title, "
                "[data-qa='product-title'], "
                "h2.product-title, "
                ".o-product__name"
            )
            raw_name = name_el.get_text(strip=True) if name_el else None
            if not raw_name:
                return None

            # Price — main price
            price_el = card.select_one(
                ".m-product-tile__price .a-pricetag__price, "
                "[data-qa='product-price'] .a-pricetag__price, "
                ".price__whole, "
                ".a-pricetag__price"
            )
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = self.parse_price(price_text)
            if price is None or price <= 0:
                return None

            # Unit price (e.g. "2.49 лв./л")
            unit_el = card.select_one(
                ".m-product-tile__price .a-pricetag__addon, "
                ".a-pricetag__addon, "
                "[data-qa='product-unit-price']"
            )
            unit = unit_el.get_text(strip=True) if unit_el else None

            # URL
            link_el = card.select_one("a[href*='/produkt/'], a[href*='/product/'], a.product-link")
            if not link_el:
                link_el = card.find("a", href=True)
            url = urljoin(self.base_url, link_el["href"]) if link_el else None

            # Image
            img_el = card.select_one("img[src], img[data-src]")
            image_url = (img_el.get("data-src") or img_el.get("src")) if img_el else None
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

            volume = self.extract_volume(raw_name)

            return RawProduct(
                store=self.store_id,
                raw_name=raw_name,
                price=price,
                unit=unit,
                url=url,
                image_url=image_url,
                category_raw=category_name,
                volume_raw=volume,
            )

        except Exception as exc:
            logger.debug("[kaufland] Failed parsing card: %s", exc)
            return None

    # ── main scrape ───────────────────────────────────────────────────────────

    def scrape_category(self, url: str, category_name: str) -> List[RawProduct]:
        products: List[RawProduct] = []
        page = 1

        while True:
            page_url = f"{url}?page={page}" if page > 1 else url

            try:
                soup = self._soup(page_url)
            except Exception as exc:
                logger.warning("[kaufland] page fetch failed %s: %s", page_url, exc)
                break

            # Find product cards — try multiple selectors
            cards = (
                soup.select(".m-product-tile")
                or soup.select("[data-qa='product-tile']")
                or soup.select(".o-overview-list__list-item")
                or soup.select(".product-card")
            )

            if not cards:
                logger.debug("[kaufland] No cards on page %d of %s", page, url)
                break

            for card in cards:
                p = self._parse_product_card(card, category_name)
                if p:
                    products.append(p)

            # Pagination
            total_pages = self._get_total_pages(soup)
            if page >= total_pages or page >= 10:   # safety cap: max 10 pages
                break
            page += 1

        return products


# ── quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    with KauflandScraper() as scraper:
        url, name = scraper.get_category_urls()[0]  # Млечни продукти
        products = scraper.scrape_category(url, name)
        print(f"\nScraped {len(products)} products from {name}:\n")
        for p in products[:5]:
            print(f"  {p.raw_name!r:50s}  {p.price:.2f} лв.   {p.unit or ''}")

        # Save sample
        with open("kaufland_sample.json", "w", encoding="utf-8") as f:
            json.dump(
                [vars(p) for p in products[:20]],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print("\nSample saved to kaufland_sample.json")
