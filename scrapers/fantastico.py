"""
Fantastico Bulgaria scraper
Site: https://fantastico.bg/
"""

from __future__ import annotations
import logging
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import Tag
from scrapers.base import BaseScraper, RawProduct

logger = logging.getLogger(__name__)


FANTASTICO_CATEGORIES = {
    "mlyako-i-mlychni":        "Мляко и млечни продукти",
    "meso-kolbasi":            "Месо и колбаси",
    "hlyab-peciva":            "Хляб и печива",
    "plodove-zelenchutsi":     "Плодове и зеленчуци",
    "napitki":                 "Напитки",
    "zamrazeni":               "Замразени",
    "kafe-chai":               "Кафе и чай",
    "sladkishi-bonboni":       "Сладкиши и бонбони",
    "konservi":                "Консерви",
    "pasta-oriz-zhitni":       "Паста, ориз, житни",
    "higiena":                 "Хигиена",
    "domakinski":              "Домакинство",
}


class FantasticoScraper(BaseScraper):
    store_id   = "fantastico"
    store_name = "Фантастико"
    base_url   = "https://fantastico.bg"

    MIN_DELAY = 2.0
    MAX_DELAY = 4.5

    def get_category_urls(self) -> List[tuple[str, str]]:
        return [
            (f"{self.base_url}/category/{slug}/", name)
            for slug, name in FANTASTICO_CATEGORIES.items()
        ]

    def _parse_product_card(self, card: Tag, category_name: str) -> Optional[RawProduct]:
        try:
            name_el = card.select_one(
                ".product__name, .product-name, h2.name, "
                "[class*='product-title'], [class*='ProductName']"
            )
            raw_name = name_el.get_text(strip=True) if name_el else None
            if not raw_name:
                return None

            price_el = card.select_one(
                ".product__price, .price, .product-price, "
                "[class*='Price']:not([class*='old']):not([class*='Old'])"
            )
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = self.parse_price(price_text)
            if price is None or price <= 0:
                return None

            link_el = card.find("a", href=True)
            url = urljoin(self.base_url, link_el["href"]) if link_el else None

            img_el = card.select_one("img")
            image_url = (img_el.get("data-src") or img_el.get("src")) if img_el else None

            return RawProduct(
                store=self.store_id,
                raw_name=raw_name,
                price=price,
                unit=None,
                url=url,
                image_url=image_url,
                category_raw=category_name,
                volume_raw=self.extract_volume(raw_name),
            )
        except Exception as exc:
            logger.debug("[fantastico] card error: %s", exc)
            return None

    def scrape_category(self, url: str, category_name: str) -> List[RawProduct]:
        products: List[RawProduct] = []
        page = 1

        while page <= 10:
            page_url = f"{url}page/{page}/" if page > 1 else url
            try:
                soup = self._soup(page_url)
            except Exception as exc:
                logger.warning("[fantastico] %s: %s", page_url, exc)
                break

            cards = (
                soup.select(".product, .product-item, [class*='ProductCard']")
                or soup.select("article.product")
            )
            if not cards:
                break

            for card in cards:
                p = self._parse_product_card(card, category_name)
                if p:
                    products.append(p)

            next_page = soup.select_one("a.next, .next-page, [rel='next']")
            if not next_page:
                break
            page += 1

        return products
