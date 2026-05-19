"""
Product Matcher — resolves raw scraped names to canonical products
Uses Claude API for fuzzy matching when rules fail

Problem: "МЛЯКО УХТ ЛАКТИМА 3.2% 1Л" (Kaufland)
        "Мляко Лактима 3,2% 1л" (Billa)
        → same canonical product: "Лактима UHT мляко 3.2% 1л"
"""

from __future__ import annotations
import logging
import re
import json
from typing import Optional

import anthropic

from scrapers.base import RawProduct

logger = logging.getLogger(__name__)


# ── normalization helpers ─────────────────────────────────────────────────────

def normalize_name(raw: str) -> str:
    """
    Normalize a product name for fuzzy matching:
    - lowercase, strip extra whitespace
    - normalize volume: '1Л' → '1л', '500г' → '500г'
    - remove store-specific prefixes/suffixes
    """
    s = raw.strip().lower()
    # normalize Cyrillic + Latin mixed volumes
    s = re.sub(r"(\d+)\s*(л|мл|г|кг|бр|пак)\b", lambda m: f"{m.group(1)}{m.group(2)}", s, flags=re.I)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


def extract_brand(raw_name: str, brand_hints: list[str]) -> Optional[str]:
    """Check if any known brand appears in the name."""
    lower = raw_name.lower()
    for brand in brand_hints:
        if brand.lower() in lower:
            return brand
    return None


# ── main matcher ──────────────────────────────────────────────────────────────

class ProductMatcher:
    """
    Resolves a RawProduct to a canonical product_id in Supabase.
    Strategy:
      1. Exact match on (store_sku, store) → fastest, when SKU available
      2. Rule-based normalization match → brand + volume + category
      3. Claude fuzzy match → for ambiguous cases
      4. Create new canonical if no match found
    """

    def __init__(self, anthropic_api_key: str):
        self._client = anthropic.Anthropic(api_key=anthropic_api_key)
        self._cache: dict[str, str] = {}   # normalized_name → product_id

    # ── public API ────────────────────────────────────────────────────────────

    def get_or_create_product(self, raw: RawProduct, sb) -> Optional[str]:
        """
        Returns product_id for this raw product.
        Creates canonical entry if not found.
        """
        # 1. SKU match
        if raw.sku:
            pid = self._match_by_sku(raw.sku, raw.store, sb)
            if pid:
                return pid

        # 2. Rule-based normalization
        norm = normalize_name(raw.raw_name)
        if norm in self._cache:
            return self._cache[norm]

        pid = self._match_by_norm(norm, raw.category_raw, sb)
        if pid:
            self._cache[norm] = pid
            return pid

        # 3. Claude fuzzy match (only for longer, specific names)
        if len(norm) > 15:
            pid = self._match_with_claude(raw, sb)
            if pid:
                self._cache[norm] = pid
                return pid

        # 4. Create new canonical
        pid = self._create_canonical(raw, norm, sb)
        if pid:
            self._cache[norm] = pid
        return pid

    # ── private helpers ───────────────────────────────────────────────────────

    def _match_by_sku(self, sku: str, store: str, sb) -> Optional[str]:
        try:
            resp = (
                sb.table("product_store_skus")
                .select("product_id")
                .eq("store", store)
                .eq("sku", sku)
                .single()
                .execute()
            )
            return resp.data["product_id"] if resp.data else None
        except Exception:
            return None

    def _match_by_norm(self, norm: str, category: str, sb) -> Optional[str]:
        """
        Look for canonical products with similar normalized name.
        Uses ilike on canonical_name_norm column.
        """
        try:
            # Try exact match first
            resp = (
                sb.table("products")
                .select("id")
                .eq("canonical_name_norm", norm)
                .single()
                .execute()
            )
            if resp.data:
                return resp.data["id"]

            # Try substring match (both ways)
            # Extract key tokens (brand + volume)
            tokens = norm.split()
            if len(tokens) >= 2:
                key = " ".join(tokens[:2])   # first two words usually brand + type
                resp2 = (
                    sb.table("products")
                    .select("id, canonical_name_norm")
                    .ilike("canonical_name_norm", f"%{key}%")
                    .eq("category", _map_category(category))
                    .limit(5)
                    .execute()
                )
                candidates = resp2.data or []
                if len(candidates) == 1:
                    return candidates[0]["id"]

        except Exception as exc:
            logger.debug("norm match error: %s", exc)

        return None

    def _match_with_claude(self, raw: RawProduct, sb) -> Optional[str]:
        """
        Use Claude to find the best canonical match among candidates.
        """
        try:
            # Get top candidates from same category
            resp = (
                sb.table("products")
                .select("id, canonical_name, brand, volume, category")
                .eq("category", _map_category(raw.category_raw))
                .limit(20)
                .execute()
            )
            candidates = resp.data or []
            if not candidates:
                return None

            prompt = f"""You are matching a scraped Bulgarian supermarket product to a canonical product.

Scraped product: "{raw.raw_name}" (from {raw.store}, category: {raw.category_raw})

Canonical candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Rules:
- Match only if you are 95%+ confident it is the SAME product (same brand, same variant, same volume)
- Different sizes (1л vs 2л) are DIFFERENT products
- Different fat % (3.2% vs 2%) are DIFFERENT products
- If no match, say null

Respond with JSON only:
{{"match_id": "<uuid or null>", "confidence": 0.0-1.0, "reason": "brief reason"}}"""

            response = self._client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )

            result = json.loads(response.content[0].text)
            if result.get("match_id") and result.get("confidence", 0) >= 0.9:
                logger.debug(
                    "Claude matched %r → %s (%.0f%%)",
                    raw.raw_name, result["match_id"], result["confidence"] * 100
                )
                return result["match_id"]

        except Exception as exc:
            logger.debug("Claude match error: %s", exc)

        return None

    def _create_canonical(self, raw: RawProduct, norm: str, sb) -> Optional[str]:
        """Create a new canonical product record."""
        try:
            data = {
                "canonical_name": _make_canonical_name(raw),
                "canonical_name_norm": norm,
                "brand": raw.brand_raw,
                "volume": raw.volume_raw,
                "category": _map_category(raw.category_raw),
                "image_url": raw.image_url,
            }
            resp = sb.table("products").insert(data).execute()
            new_id = resp.data[0]["id"] if resp.data else None
            if new_id:
                logger.debug("Created canonical: %r → %s", raw.raw_name, new_id)
            return new_id
        except Exception as exc:
            logger.debug("Create canonical error: %s", exc)
            return None


# ── utility ───────────────────────────────────────────────────────────────────

_CATEGORY_MAP = {
    "мляко":        "dairy",
    "млечни":       "dairy",
    "месо":         "meat",
    "колбас":       "meat",
    "риба":         "fish",
    "хляб":         "bread",
    "плодове":      "produce",
    "зеленчуц":     "produce",
    "напитки":      "beverages",
    "кафе":         "coffee_tea",
    "чай":          "coffee_tea",
    "сладкиш":      "sweets",
    "бонбон":       "sweets",
    "шоколад":      "sweets",
    "консерв":      "canned",
    "паста":        "pasta_rice",
    "ориз":         "pasta_rice",
    "масло":        "oils_sauces",
    "сос":          "oils_sauces",
    "замраз":       "frozen",
    "детск":        "baby",
    "хигиена":      "hygiene",
    "домакинск":    "household",
}


def _map_category(raw_cat: str) -> str:
    lc = raw_cat.lower()
    for bg, eng in _CATEGORY_MAP.items():
        if bg in lc:
            return eng
    return "other"


def _make_canonical_name(raw: RawProduct) -> str:
    """Best effort canonical name from raw data."""
    name = raw.raw_name.strip()
    # Title-case
    name = " ".join(w.capitalize() for w in name.split())
    return name
