"""
Base scraper class — all store scrapers inherit from this
"""

from __future__ import annotations
import logging
import re
import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class RawProduct:
    """A product as scraped from the store website — before normalization."""
    store: str
    raw_name: str           # e.g. "МЛЯКО УХТ ЛАКТИМА 3.2% 1Л"
    price: float            # e.g. 2.49
    unit: Optional[str]     # e.g. "лв./л" or None
    url: Optional[str]      # product page URL
    image_url: Optional[str]
    category_raw: str       # original category string
    sku: Optional[str] = None
    brand_raw: Optional[str] = None
    volume_raw: Optional[str] = None
    extra: dict = field(default_factory=dict)


class BaseScraper(ABC):
    """
    Abstract base for all store scrapers.

    Subclasses implement:
        - scrape_category(category_url) -> List[RawProduct]
        - scrape_product(url) -> Optional[RawProduct]
        - get_category_urls() -> List[tuple[str, str]]   # (url, category_name)
    """

    store_id: str = ""
    store_name: str = ""
    base_url: str = ""

    # Polite scraping defaults
    MIN_DELAY = 1.5   # seconds between requests
    MAX_DELAY = 3.5

    def __init__(self):
        self.session = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=30,
        )
        self._last_request: float = 0.0

    # ── polite fetching ───────────────────────────────────────────────────────

    def _get(self, url: str, **kwargs) -> httpx.Response:
        """Throttled GET request with random delay."""
        elapsed = time.time() - self._last_request
        delay = random.uniform(self.MIN_DELAY, self.MAX_DELAY)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.time()

        logger.debug("[%s] GET %s", self.store_id, url)
        resp = self.session.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def _soup(self, url: str, **kwargs) -> BeautifulSoup:
        resp = self._get(url, **kwargs)
        return BeautifulSoup(resp.text, "lxml")

    # ── abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def get_category_urls(self) -> List[tuple[str, str]]:
        """Return list of (url, category_name) to scrape."""

    @abstractmethod
    def scrape_category(self, url: str, category_name: str) -> List[RawProduct]:
        """Scrape all products from a category listing page."""

    # ── utility helpers ───────────────────────────────────────────────────────

    @staticmethod
    def parse_price(text: str) -> Optional[float]:
        """Extract float price from text like '2,49 лв.' or '3.50'."""
        if not text:
            return None
        # normalize: '2,49' → '2.49'
        cleaned = re.sub(r"[^\d,.]", "", text.strip()).replace(",", ".")
        # handle '2.49.0' edge case
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0] + "." + "".join(parts[1:])
        try:
            return round(float(cleaned), 2)
        except ValueError:
            return None

    @staticmethod
    def extract_volume(name: str) -> Optional[str]:
        """Extract volume/weight from product name, e.g. '1Л', '500Г', '6x100МЛ'."""
        patterns = [
            r"\b(\d+[\.,]?\d*)\s*(кг|г|л|мл|бр|пак|рол)\b",
            r"\b(\d+)\s*[xX]\s*(\d+)\s*(г|мл|бр)\b",
        ]
        for p in patterns:
            m = re.search(p, name, re.IGNORECASE)
            if m:
                return m.group(0).strip()
        return None

    # ── full scrape ───────────────────────────────────────────────────────────

    def scrape_all(self) -> List[RawProduct]:
        """Scrape all categories. Returns all products."""
        all_products: List[RawProduct] = []
        categories = self.get_category_urls()
        logger.info("[%s] Scraping %d categories", self.store_id, len(categories))

        for url, cat_name in categories:
            try:
                products = self.scrape_category(url, cat_name)
                logger.info("[%s] %s → %d products", self.store_id, cat_name, len(products))
                all_products.extend(products)
            except Exception as exc:
                logger.error("[%s] Failed on %s: %s", self.store_id, url, exc)
                continue

        logger.info("[%s] Total scraped: %d products", self.store_id, len(all_products))
        return all_products

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
