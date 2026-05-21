"""
OLX.bg second-hand search — public REST API (no auth, no Selenium).
API: https://www.olx.bg/api/v1/offers/?query=...

Usage:
  py -m alex.scrapers.olx "iphone 13 pro"
"""
from __future__ import annotations
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://www.olx.bg/api/v1/offers/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "bg,en;q=0.9",
}
_CDN = "https://frankfurt.apollo.olxcdn.com:443/v1/files/{fn}/image;s=360x270"


def _bgn_price(params: list) -> Optional[float]:
    for p in params:
        if p.get("key") == "price":
            v = p.get("value", {})
            val = v.get("converted_value") or v.get("value")
            try:
                return round(float(val), 2) if val else None
            except (TypeError, ValueError):
                return None
    return None


def _condition(params: list) -> str:
    for p in params:
        if p.get("key") == "state":
            return p.get("value", {}).get("label", "")
    return "използвано"


def _photo(photos: list) -> str:
    if not photos:
        return ""
    fn = photos[0].get("filename", "")
    return _CDN.format(fn=fn) if fn else ""


def search_olx(query: str, max_results: int = 5) -> list[dict]:
    """Search OLX.bg and return up to max_results listings."""
    try:
        resp = httpx.get(
            _API,
            params={
                "query":   query,
                "limit":   max_results,
                "offset":  0,
                "sort_by": "created_at:desc",
            },
            headers=_HEADERS,
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("[olx] search failed for %r: %s", query, exc)
        return []

    results = []
    for item in items[:max_results]:
        price = _bgn_price(item.get("params", []))
        if not price:
            continue
        loc = (item.get("location") or {})
        city = loc.get("city", {}).get("name", "")
        results.append({
            "source":    "olx",
            "title":     (item.get("title") or "").strip(),
            "price":     price,
            "condition": _condition(item.get("params", [])),
            "location":  city,
            "url":       item.get("url", ""),
            "image_url": _photo(item.get("photos", [])),
        })

    logger.info("[olx] %d results for %r", len(results), query)
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    q = " ".join(sys.argv[1:]) or "iphone 13"
    for r in search_olx(q, max_results=8):
        print(f"  [{r['source']}] {r['title'][:55]:<55} {r['price']:.0f} лв.  {r['location']}")
        print(f"          {r['url']}")
