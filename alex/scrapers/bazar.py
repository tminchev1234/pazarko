"""
Bazar.bg second-hand search — server-side HTML parsing (no Selenium).
Search URL: https://bazar.bg/obiavi/elektronika?q={query}

Usage:
  py -m alex.scrapers.bazar "iphone 13 pro"
"""
from __future__ import annotations
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE = "https://bazar.bg"
_SEARCH = f"{_BASE}/obiavi/elektronika"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

_PRICE_RE = re.compile(r"([\d\s]+(?:[.,]\d+)?)\s*(?:лв\.?|BGN)", re.IGNORECASE)
_HREF_RE  = re.compile(r"/obiava-(\d+)/")


def _parse_price(text: str) -> Optional[float]:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group(1)).replace(",", ".")
    # strip trailing dot
    raw = raw.rstrip(".")
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def search_bazar(query: str, max_results: int = 5) -> list[dict]:
    """Search Bazar.bg and return up to max_results listings."""
    try:
        resp = httpx.get(
            _SEARCH,
            params={"q": query},
            headers=_HEADERS,
            timeout=12,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("[bazar] search failed for %r: %s", query, exc)
        return []

    soup  = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=_HREF_RE)

    results = []
    seen: set[str] = set()

    for link in links:
        href = link.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        url = href if href.startswith("http") else _BASE + href

        # Title from title attr, fallback to first meaningful div text
        title = (link.get("title") or "").strip()

        divs = link.find_all("div")
        price_text = ""
        location   = ""

        if not title and divs:
            title = divs[0].get_text(strip=True)

        # Find price div (contains "лв")
        for div in divs:
            t = div.get_text(strip=True)
            if "лв" in t:
                price_text = t
                break

        # Location is usually the second div
        if len(divs) >= 2:
            candidate = divs[1].get_text(strip=True)
            # Avoid picking the title again
            if candidate != title and "лв" not in candidate:
                location = candidate

        price = _parse_price(price_text)
        if not price or not title:
            continue

        # Image
        img_el    = link.find("img")
        image_url = (img_el.get("src") or "") if img_el else ""
        if image_url and not image_url.startswith("http"):
            image_url = _BASE + image_url
        if "noPhoto" in image_url or not image_url:
            image_url = ""

        results.append({
            "source":    "bazar",
            "title":     title[:120],
            "price":     price,
            "condition": "използвано",
            "location":  location[:60],
            "url":       url,
            "image_url": image_url,
        })

        if len(results) >= max_results:
            break

    logger.info("[bazar] %d results for %r", len(results), query)
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    q = " ".join(sys.argv[1:]) or "iphone 13"
    for r in search_bazar(q, max_results=8):
        print(f"  [{r['source']}] {r['title'][:55]:<55} {r['price']:.0f} лв.  {r['location']}")
        print(f"          {r['url']}")
