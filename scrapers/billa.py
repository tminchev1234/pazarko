"""
Billa Bulgaria scraper
Site: https://www.billa.bg/

Billa Bulgaria has a proper online shop / product listing.
Products are loaded via their website's listing pages.
"""

from __future__ import annotations
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from scrapers.base import BaseScraper, RawProduct

logger = logging.getLogger(__name__)


BILLA_CATEGORIES = {
    "mlyako-i-mlychni-produkti":   "Мляко и млечни продукти",
    "meso-i-kolbasi":              "Месо и колбаси",
    "ribi-i-morski-darove":        "Риби и морски дарове",
    "hlyab-i-peciva":              "Хляб и печива",
    "plodove-i-zelenchutsi":       "Плодове и зеленчуци",
    "napitki":                     "Напитки",
    "zamrazeni-produkti":          "Замразени продукти",
    "kafe-chai-kakao":             "Кафе, чай, какао",
    "bonboni-i-shokolad":          "Бонбони и шоколад",
    "konservi-i-saksii":           "Консерви и саксии",
    "pasta-i-oriz":                "Паста и ориз",
    "masla-sosove-podpravki":      "Масла, сосове, подправки",
    "detski-produkti":             "Детски продукти",
    "chistota-i-higiena":          "Чистота и хигиена",
    "grizhа-za-tqlo":              "Грижа за тяло",
}


class BillaScraper(BaseScraper):
    store_id   = "billa"
    store_name = "Billa"
    base_url   = "https://www.billa.bg"

    MIN_DELAY = 2.5
    MAX_DELAY = 5.0

    def get_category_urls(self) -> List[tuple[str, str]]:
        return [
            (f"{self.base_url}/categories/{slug}", name)
            for slug, name in BILLA_CATEGORIES.items()
        ]

    def _parse_product_card(self, card: Tag, category_name: str) -> Optional[RawProduct]:
        try:
            # Name
            name_el = card.select_one(
                ".product-card__name, "
                ".product-title, "
                "[data-testid='product-name'], "
                "h2, h3"
            )
            raw_name = name_el.get_text(strip=True) if name_el else None
            if not raw_name:
                return None

            # Price
            price_el = card.select_one(
                ".product-card__price, "
                ".price-tag, "
                "[data-testid='product-price'], "
                ".price"
            )
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = self.parse_price(price_text)
            if price is None or price <= 0:
                return None

            # Unit price
            unit_el = card.select_one(
                ".product-card__price-per-unit, "
                ".unit-price, "
                "[data-testid='unit-price']"
            )
            unit = unit_el.get_text(strip=True) if unit_el else None

            # URL
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
            logger.debug("[billa] card parse error: %s", exc)
            return None

    def scrape_category(self, url: str, category_name: str) -> List[RawProduct]:
        products: List[RawProduct] = []
        page = 1

        while page <= 8:   # safety cap
            page_url = f"{url}?page={page}" if page > 1 else url
            try:
                soup = self._soup(page_url)
            except Exception as exc:
                logger.warning("[billa] fetch failed %s: %s", page_url, exc)
                break

            cards = (
                soup.select(".product-card")
                or soup.select("[data-testid='product-card']")
                or soup.select(".product-item")
            )

            if not cards:
                break

            for card in cards:
                p = self._parse_product_card(card, category_name)
                if p:
                    products.append(p)

            # Check for next page
            next_btn = soup.select_one(
                "a[rel='next'], "
                ".pagination__next:not(.disabled), "
                "[aria-label='Следваща страница']"
            )
            if not next_btn:
                break
            page += 1

        return products


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    with BillaScraper() as scraper:
        url, name = scraper.get_category_urls()[0]
        products = scraper.scrape_category(url, name)
        print(f"Scraped {len(products)} products from {name}")
        for p in products[:5]:
            print(f"  {p.raw_name!r:50s}  {p.price:.2f} лв.")
