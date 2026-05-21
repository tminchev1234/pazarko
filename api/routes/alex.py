"""
Alex — AI Electronics Advisor
Claude Tool Use endpoint for electronics price comparison in Bulgaria.

Endpoints:
  POST /api/alex/chat          — streaming SSE with tool calls
  POST /api/alex/chat/simple   — non-streaming (for testing)
  GET  /api/alex/search        — direct product search
"""

from __future__ import annotations
import asyncio
import json
import logging
import re
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, List, Optional, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import anthropic

from api.config import get_settings
from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["alex"])

# ── Local JSON fallback ────────────────────────────────────────────────────────
# When PostgREST schema cache is broken, we read from local JSON files.

_LOCAL_CACHE: list[dict] | None = None
_PROJECT_ROOT = Path(__file__).resolve().parents[2]   # pazarko/

_JSON_FILES = [
    "emag_offers.json",
    "technopolis_offers.json",
    "ardes_offers.json",
    "technomarket_offers.json",
    "zora_offers.json",
]


def _reload_local() -> None:
    """Force reload of local JSON cache (call after new scrape)."""
    global _LOCAL_CACHE
    _LOCAL_CACHE = None


def _load_local() -> list[dict]:
    """Load + cache all offers — JSON files locally, Supabase in production."""
    global _LOCAL_CACHE
    if _LOCAL_CACHE is not None:
        return _LOCAL_CACHE

    offers: list[dict] = []
    for fname in _JSON_FILES:
        path = _PROJECT_ROOT / fname
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                offers.extend(data)
                logger.info("[alex/local] Loaded %d offers from %s", len(data), fname)
            except Exception as exc:
                logger.warning("[alex/local] Could not read %s: %s", fname, exc)

    if not offers:
        # No JSON files (production / Render) — load everything from Supabase
        try:
            sb = get_supabase()
            page_size = 1000
            offset = 0
            while True:
                resp = (
                    sb.table("electronics_offers")
                    .select("*")
                    .range(offset, offset + page_size - 1)
                    .execute()
                )
                batch = resp.data or []
                offers.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
            logger.info("[alex/supabase] Loaded %d offers from Supabase", len(offers))
        except Exception as exc:
            logger.warning("[alex/supabase] Could not load from Supabase: %s", exc)

    _LOCAL_CACHE = offers
    logger.info("[alex/cache] Total offers in cache: %d", len(offers))
    return _LOCAL_CACHE


def _local_search(
    query: str = "",
    category: str | None = None,
    store: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    limit: int = 10,
) -> list[dict]:
    offers = _load_local()
    q = query.lower().strip()

    def _matches(o: dict) -> bool:
        name = (o.get("raw_name") or "").lower()
        if q and q not in name:
            return False
        if category and o.get("category") != category:
            return False
        if store and o.get("store") != store:
            return False
        price = o.get("price") or 0
        if min_price and price < min_price:
            return False
        if max_price and price > max_price:
            return False
        return True

    def _row(o: dict) -> dict:
        return {
            "raw_name":    o.get("raw_name", ""),
            "brand":       o.get("brand", ""),
            "category":    o.get("category", ""),
            "category_raw": o.get("category_raw", ""),
            "price":       o.get("price") or 0,
            "old_price":   o.get("old_price"),
            "discount_pct": o.get("discount_pct"),
            "store":       o.get("store", ""),
            "image_url":   o.get("image_url", ""),
            "url":         o.get("url", ""),
        }

    results = [_row(o) for o in offers if _matches(o)]
    results.sort(key=lambda x: x["price"] or 0)

    # If query returned nothing but category is set, fall back to category-only
    if not results and q and category:
        fallback = [_row(o) for o in offers if o.get("category") == category]
        fallback.sort(key=lambda x: x["price"] or 0)
        return fallback[:limit]

    return results[:limit]


def _local_prices(product_name: str, category: str | None = None) -> list[dict]:
    return _local_search(query=product_name, category=category, limit=20)


def _local_deals(category: str | None = None, limit: int = 8) -> list[dict]:
    offers = _load_local()
    results = []
    for o in offers:
        disc = o.get("discount_pct")
        if not disc or disc <= 0:
            continue
        if not o.get("image_url"):
            continue
        if category and o.get("category") != category:
            continue
        results.append({
            "raw_name":    o.get("raw_name", ""),
            "brand":       o.get("brand", ""),
            "category":    o.get("category", ""),
            "price":       o.get("price", 0),
            "old_price":   o.get("old_price"),
            "discount_pct": disc,
            "store":       o.get("store", ""),
            "image_url":   o.get("image_url", ""),
            "url":         o.get("url", ""),
        })
    results.sort(key=lambda x: x["discount_pct"] or 0, reverse=True)
    return results[:limit]

# ── Models ────────────────────────────────────────────────────────────────────

class AlexMessage(BaseModel):
    role: str       # "user" | "assistant"
    content: str


class AlexChatRequest(BaseModel):
    messages: List[AlexMessage]
    user_id:  Optional[str] = None
    category: Optional[str] = None


# ── System prompt ─────────────────────────────────────────────────────────────

ALEX_SYSTEM = """Ти си Алекс — AI съветник по електроника за България.

Помагаш на хората да вземат умни решения при покупка на техника — слушалки, телефони, лаптопи, телевизори, конзоли, фотоапарати, домакински уреди и аксесоари.

════════════════════════════════════════
АБСОЛЮТНИ ПРАВИЛА (нарушението е грешка)
════════════════════════════════════════

❌ ЗАБРАНЕНО: Да показваш цена, която не е от базата данни
❌ ЗАБРАНЕНО: Приблизителни цени (~€45, "около €50", "между €40-60")
❌ ЗАБРАНЕНО: Да препоръчваш продукт, който не е намерен в търсенето
❌ ЗАБРАНЕНО: Да казваш "лв." — само EUR (€)
❌ ЗАБРАНЕНО: Таблица с продукти БЕЗ колона "Линк" — всеки ред ТРЯБВА да има линк

✅ ЗАДЪЛЖИТЕЛНО: В таблиците показвай САМО продукти от search_products резултатите
✅ ЗАДЪЛЖИТЕЛНО: Цените са ТОЧНО това, което е в базата — нито стотинка повече или по-малко
✅ ЗАДЪЛЖИТЕЛНО: Ако даден модел не е в базата → не го включвай в таблицата с цени
✅ ЗАДЪЛЖИТЕЛНО: Всеки продукт в таблицата ТРЯБВА да има линк [Виж →](url) — вземи url от search_products резултата

════════════════════════════════════════
СТРАТЕГИЯ ЗА ТЪРСЕНЕ
════════════════════════════════════════

При "безжични слушалки до 100€" направи ПОСЛЕДОВАТЕЛНО:
1. search_products(query="bluetooth", category="headphones", max_price=100, limit=20)
2. Ако < 5 резултата → search_products(query="wireless", category="headphones", max_price=100, limit=20)
3. Ако пак < 5 → search_products(query="", category="headphones", max_price=100, limit=20)
4. Работи с намереното — не измисляй

Правило за търсене по марка: ако питат за "Sony слушалки" → query="Sony", category="headphones"
За TV по размер: query="55", category="tvs"
НЕ търси с български думи — само английски: "bluetooth", "wireless", "Samsung", "laptop"

════════════════════════════════════════
СТРУКТУРА НА ОТГОВОРА
════════════════════════════════════════

**СТЪПКА 1 — ТЪРСЕНЕ:**
Извикай search_products. При нужда — 2-3 различни заявки за по-добро покритие.

**СТЪПКА 2 — ТОП 5 ОТ НАМЕРЕНОТО:**
Избери 5 от РЕАЛНИТЕ резултати по различни критерии:

| # | Критерий | Модел | Цена | Защо? | Линк |
|---|----------|-------|------|-------|------|
| 🥇 | Най-добра стойност | [реален модел от базата] | €XX,XX | ... | [Виж →](url) |
| 🥈 | Най-добър бюджет | [реален модел от базата] | €XX,XX | ... | [Виж →](url) |
| 🥉 | Премиум избор | [реален модел от базата] | €XX,XX | ... | [Виж →](url) |
| 4️⃣ | За [конкретна употреба] | [реален модел от базата] | €XX,XX | ... | [Виж →](url) |
| 5️⃣ | Скрита перла | [реален модел от базата] | €XX,XX | ... | [Виж →](url) |

Ако имаш само 3 реални резултата — показвай само 3, не измисляй останалите.

**СТЪПКА 3 — СРАВНЕНИЕ НА ТОП 2:**
Таблица с характеристики на №1 и №2. Цените — точно от базата. Спецификациите (батерия, Bluetooth версия и т.н.) — от твоите знания за модела.

| Характеристика | [Модел 1] | [Модел 2] |
|---|---|---|
| Цена | €XX,XX | €XX,XX |
| Магазин | [store] | [store] |
| Линк | [Виж →](url) | [Виж →](url) |
| [Спец] | ... | ... |

**СТЪПКА 4 — ЗАДЪЛЖИТЕЛЕН ПРОАКТИВЕН ВЪПРОС:**
Всеки отговор завършва с конкретен въпрос — никога с "Мога ли да помогна с нещо друго?"

════════════════════════════════════════
ПРАВИЛА ЗА FOLLOW-UP
════════════════════════════════════════

- Follow-up (сравни, кой е по-добър, ти кой би избрал) → НЕ търси отново, работи с вече намереното
- Ново търсене само при НОВА категория или продукт
- Всеки отговор завършва с уточняващ въпрос

**Магазини:** eMAG · Технополис · Ардес · Техномаркет
**Категории:** headphones, phones, laptops, tvs, tablets, gaming, cameras, appliances, accessories
**Бюджет в лв.:** раздели на 1.96 → "200 лв." = max_price=102
"""

# ── Tool definitions ──────────────────────────────────────────────────────────

ALEX_TOOLS = [
    {
        "name": "search_products",
        "description": (
            "Търси продукти в базата данни от български електронни магазини. "
            "Използвай при: препоръки, сравнения, питания 'колко струва X', 'намери ми Y'. "
            "Връща списък с продукти (name, price, store, category, discount_pct, url)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Търсен текст — марка, модел или описание. Пример: 'Sony WH-1000XM5', 'безжични слушалки', 'iPhone 15'"
                },
                "category": {
                    "type": "string",
                    "description": "Категория (незадължително): headphones, phones, laptops, tvs, tablets, gaming, cameras, appliances, accessories",
                    "enum": ["headphones", "phones", "laptops", "tvs", "tablets", "gaming", "cameras", "appliances", "accessories"]
                },
                "max_price": {
                    "type": "number",
                    "description": "Максимална цена в EUR/€ (незадължително). Ако потребителят дава бюджет в лв., раздели на 1.96."
                },
                "min_price": {
                    "type": "number",
                    "description": "Минимална цена в EUR/€ (незадължително). Ако потребителят дава бюджет в лв., раздели на 1.96."
                },
                "store": {
                    "type": "string",
                    "description": "Магазин (незадължително): technopolis, emag",
                    "enum": ["technopolis", "emag"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Брой резултати (по подразбиране 10, макс 30)",
                    "default": 10
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_prices",
        "description": (
            "Взима цените на конкретен продукт от всички магазини. "
            "Използвай когато потребителят е избрал продукт и иска да знае откъде е най-евтино. "
            "Търси по точно или частично съвпадение на марка+модел."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "Пълно или частично название на продукта. Пример: 'Samsung Galaxy S24', 'Sony WH-1000XM5'"
                },
                "category": {
                    "type": "string",
                    "description": "Категория за по-точно търсене (незадължително)"
                }
            },
            "required": ["product_name"]
        }
    },
    {
        "name": "get_top_deals",
        "description": (
            "Показва топ оферти с най-голяма отстъпка в дадена категория. "
            "Използвай когато потребителят пита за 'намаления', 'промоции', 'най-добри оферти'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Категория: headphones, phones, laptops, tvs, tablets, gaming, cameras, appliances, accessories"
                },
                "limit": {
                    "type": "integer",
                    "description": "Брой оферти (по подразбиране 8)",
                    "default": 8
                }
            },
            "required": []
        }
    }
]

# ── Tool execution ────────────────────────────────────────────────────────────

def _exec_search_products(args: dict) -> list[dict]:
    query = args.get("query", "").strip()
    limit = min(int(args.get("limit", 10)), 30)

    # Try Supabase first
    try:
        sb = get_supabase()
        q = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, category_raw, price, old_price, discount_pct, store, image_url, url")
            .ilike("raw_name", f"%{query}%")
        )
        if args.get("category"):
            q = q.eq("category", args["category"])
        if args.get("store"):
            q = q.eq("store", args["store"])
        if args.get("max_price"):
            q = q.lte("price", args["max_price"])
        if args.get("min_price"):
            q = q.gte("price", args["min_price"])
        resp = q.order("price", desc=False).limit(limit).execute()
        if resp.data:
            return _apply_blocklist(resp.data, args.get("category", ""))
    except Exception as exc:
        logger.warning("[alex] Supabase search_products failed, using local JSON: %s", exc)

    # Fallback: local JSON
    return _apply_blocklist(_local_search(
        query=query,
        category=args.get("category"),
        store=args.get("store"),
        min_price=args.get("min_price"),
        max_price=args.get("max_price"),
        limit=limit,
    ), args.get("category", ""))


def _apply_blocklist(products: list[dict], category: str) -> list[dict]:
    """Filter out category-specific blocked product types (e.g. feature phones)."""
    words = [w.lower() for w in _CAT_BLOCKLIST.get(category, [])]
    if not words:
        return products
    return [p for p in products if not any(w in p.get("raw_name", "").lower() for w in words)]


def _exec_get_prices(args: dict) -> list[dict]:
    product_name = args.get("product_name", "").strip()

    try:
        sb = get_supabase()
        q = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url")
            .ilike("raw_name", f"%{product_name}%")
        )
        if args.get("category"):
            q = q.eq("category", args["category"])
        resp = q.order("price", desc=False).limit(20).execute()
        if resp.data:
            return resp.data
    except Exception as exc:
        logger.warning("[alex] Supabase get_prices failed, using local JSON: %s", exc)

    return _local_prices(product_name=product_name, category=args.get("category"))


_DEAL_COLOR_WORDS = re.compile(
    r"\b(black|white|blue|red|grey|gray|silver|gold|green|pink|purple|"
    r"midnight|starlight|coral|lavender|graphite|rose|тъмен|черен|бял|сив)\b",
    re.IGNORECASE,
)


def _dedup_deals(deals: list[dict], limit: int) -> list[dict]:
    """Deduplicate color/storage variants and enforce max 2 per category."""
    seen_model: dict[str, float] = {}   # model_key → best discount
    cat_count:  dict[str, int]  = {}
    result: list[dict] = []

    for d in sorted(deals, key=lambda x: -(x.get("discount_pct") or 0)):
        if not d.get("image_url"):
            continue
        # Strip color words + trailing model numbers to get canonical model key
        name = d.get("raw_name", "")
        key  = _DEAL_COLOR_WORDS.sub("", name).strip().lower()
        key  = re.sub(r"\s{2,}", " ", key)

        cat = d.get("category", "other")
        if key in seen_model:
            continue  # same model already included
        if cat_count.get(cat, 0) >= 2:
            continue  # max 2 per category

        seen_model[key] = d.get("discount_pct", 0)
        cat_count[cat]  = cat_count.get(cat, 0) + 1
        result.append(d)
        if len(result) >= limit:
            break

    return result


def _exec_get_top_deals(args: dict) -> list[dict]:
    limit = min(int(args.get("limit", 8)), 20)

    try:
        sb = get_supabase()
        q = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url")
            .not_.is_("discount_pct", "null")
            .gt("discount_pct", 0)
        )
        if args.get("category"):
            q = q.eq("category", args["category"])
        resp = q.order("discount_pct", desc=True).limit(60).execute()
        if resp.data:
            return _dedup_deals(resp.data, limit)
    except Exception as exc:
        logger.warning("[alex] Supabase get_top_deals failed, using local JSON: %s", exc)

    return _dedup_deals(_local_deals(category=args.get("category"), limit=60), limit)


def _run_tool(tool_name: str, tool_input: dict) -> Any:
    if tool_name == "search_products":
        return _exec_search_products(tool_input)
    if tool_name == "get_prices":
        return _exec_get_prices(tool_input)
    if tool_name == "get_top_deals":
        return _exec_get_top_deals(tool_input)
    return {"error": f"Unknown tool: {tool_name}"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blocks_to_dicts(content: list) -> list[dict]:
    """Convert Anthropic SDK content block objects to plain API-compatible dicts."""
    result = []
    for b in content:
        btype = getattr(b, "type", None)
        if btype == "text":
            result.append({"type": "text", "text": b.text})
        elif btype == "tool_use":
            result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return result


# ── Streaming chat ─────────────────────────────────────────────────────────────

def _alex_dna_addendum(dna: dict) -> str:
    """Inject personalization context into Alex's system prompt."""
    sensitivity = dna.get("price_sensitivity", 0.5)
    categories  = dna.get("top_categories", [])
    searches    = dna.get("searches_count", 0)

    tier = (
        "бюджетни продукти (под средната цена на категорията)" if sensitivity > 0.65
        else "продукти от средна ценова категория"              if sensitivity > 0.35
        else "по-скъпи, premium продукти"
    )
    cat_str = ", ".join(categories[:4]) if categories else "все още неизвестно"

    return f"""

═══ ПРОФИЛ НА ПОТРЕБИТЕЛЯ (Shopping DNA) ═══
• Предпочита: {tier}
• Интересувал се е от: {cat_str}
• Общо питания към Alex: {searches}

Адаптирай препоръките спрямо профила — не предлагай продукти извън предпочитания ценови клас без изрично питане.
При cross-category предложения, фокусирай се върху категориите, от които потребителят вече се е интересувал.
════════════════════════════════════════════"""


def _update_alex_dna(
    user_id: str,
    category: Optional[str],
    max_prices: list[float],
    current_dna: Optional[dict],
) -> None:
    """Update user DNA after a chat session — best-effort, sync."""
    try:
        sb = get_supabase()
        if not _CATEGORY_MEDIANS:
            _compute_medians()

        update: dict = {}

        # Update top_categories (most recent first, max 6)
        if category:
            cats = list(current_dna.get("top_categories", []) if current_dna else [])
            if category in cats:
                cats.remove(category)
            cats.insert(0, category)
            update["top_categories"] = cats[:6]

        # Infer price sensitivity from max_price relative to category median
        if max_prices and category:
            median = _CATEGORY_MEDIANS.get(category, 0)
            if median > 0:
                avg_max = sum(max_prices) / len(max_prices)
                if avg_max < median * 0.8:     # clearly budget
                    target = 0.8
                elif avg_max > median * 1.6:   # clearly premium
                    target = 0.2
                else:
                    target = None
                if target is not None:
                    cur = current_dna.get("price_sensitivity", 0.5) if current_dna else 0.5
                    update["price_sensitivity"] = round(0.75 * cur + 0.25 * target, 3)

        if update:
            update["updated_at"] = datetime.utcnow().isoformat()
            sb.table("user_dna").upsert({"user_id": user_id, **update}).execute()

        # Always bump search count
        sb.rpc("increment_search_count", {"uid": user_id}).execute()

    except Exception as exc:
        logger.warning("[alex/dna] update failed for %s: %s", user_id, exc)


async def _stream_alex(
    messages: List[AlexMessage],
    system: str,
    user_id: Optional[str] = None,
    category: Optional[str] = None,
    user_dna: Optional[dict] = None,
) -> AsyncIterator[str]:
    settings = get_settings()

    if not settings.anthropic_api_key:
        yield f"data: {json.dumps({'error': 'ANTHROPIC_API_KEY не е зададен'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    # Use the async client so we never block the event loop
    async_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    msg_dicts   = [{"role": m.role, "content": m.content} for m in messages]
    collected_max_prices: list[float] = []

    try:
        for _round in range(5):
            async with async_client.messages.stream(
                model      = "claude-sonnet-4-6",
                max_tokens = 2048,
                system     = system,
                tools      = ALEX_TOOLS,
                messages   = msg_dicts,
            ) as stream:
                async for text_chunk in stream.text_stream:
                    yield f"data: {json.dumps({'text': text_chunk})}\n\n"
                final_msg = await stream.get_final_message()

            full_content = final_msg.content
            stop_reason  = final_msg.stop_reason

            tool_calls = [
                {"id": b.id, "name": b.name, "input": b.input}
                for b in full_content
                if getattr(b, "type", None) == "tool_use"
            ]

            if stop_reason != "tool_use":
                break

            tool_results: list[dict] = []
            for tc in tool_calls:
                inp = tc.get("input", {})
                yield f"data: {json.dumps({'tool': tc['name'], 'input': inp})}\n\n"
                if inp.get("max_price"):
                    collected_max_prices.append(float(inp["max_price"]))
                result = _run_tool(tc["name"], inp)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tc["id"],
                    "content":     json.dumps(result, ensure_ascii=False),
                })

            for tc, tr in zip(tool_calls, tool_results):
                raw = json.loads(tr["content"])
                if isinstance(raw, list) and raw:
                    yield f"data: {json.dumps({'products': raw, 'tool': tc['name'], 'input': tc.get('input', {})})}\n\n"

            msg_dicts.append({"role": "assistant", "content": _blocks_to_dicts(full_content)})
            msg_dicts.append({"role": "user",      "content": tool_results})

    except anthropic.APIStatusError as exc:
        logger.error("[alex] stream error: %s", exc)
        status = exc.status_code
        body   = str(exc).lower()
        if "credit balance" in body or "billing" in body:
            msg = "Няма достатъчно API кредити. Моля, свържете се с администратора."
        elif status == 429:
            msg = "Твърде много заявки. Изчакайте малко и опитайте отново."
        elif status == 401:
            msg = "Невалиден API ключ. Моля, свържете се с администратора."
        else:
            msg = "Грешка при свързване с AI. Опитайте отново след малко."
        yield f"data: {json.dumps({'error': msg})}\n\n"
    except Exception as exc:
        logger.error("[alex] stream error: %s", exc)
        yield f"data: {json.dumps({'error': 'Грешка при обработка на заявката. Опитайте отново.'})}\n\n"

    if user_id:
        _update_alex_dna(user_id, category, collected_max_prices, user_dna)

    yield "data: [DONE]\n\n"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/alex/chat")
async def alex_chat(req: AlexChatRequest):
    """Streaming SSE chat — Claude Tool Use with electronics search."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="Няма съобщения")

    # Load Shopping DNA for personalization
    user_dna: Optional[dict] = None
    if req.user_id:
        try:
            sb = get_supabase()
            resp = sb.table("user_dna").select("*").eq("user_id", req.user_id).single().execute()
            user_dna = resp.data
        except Exception:
            pass  # DNA is optional — chat works without it

    system = ALEX_SYSTEM
    if user_dna:
        system += _alex_dna_addendum(user_dna)

    return StreamingResponse(
        _stream_alex(req.messages, system, req.user_id, req.category, user_dna),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/alex/chat/simple")
async def alex_chat_simple(req: AlexChatRequest):
    """Non-streaming version — runs the agentic tool loop and returns final text."""
    settings = get_settings()
    client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if not req.messages:
        raise HTTPException(status_code=400, detail="Няма съобщения")

    msg_dicts = [{"role": m.role, "content": m.content} for m in req.messages]
    products_found: list[dict] = []

    for _round in range(5):  # max tool rounds
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=ALEX_SYSTEM,
            tools=ALEX_TOOLS,
            messages=msg_dicts,
        )

        if response.stop_reason != "tool_use":
            text = "".join(
                b.text for b in response.content if hasattr(b, "text")
            )
            return {
                "response": text,
                "products": products_found,
                "usage": {
                    "input_tokens":  response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            }

        # Run tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _run_tool(block.name, block.input)
            if isinstance(result, list):
                products_found.extend(result)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, ensure_ascii=False),
            })

        msg_dicts.append({"role": "assistant", "content": response.content})
        msg_dicts.append({"role": "user",      "content": tool_results})

    raise HTTPException(status_code=500, detail="Tool loop exceeded max rounds")


@router.get("/alex/search")
async def alex_search(
    q:        str            = Query(..., description="Search query"),
    category: Optional[str] = Query(None),
    store:    Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    limit:    int            = Query(10, le=50),
):
    """Direct product search — no AI, just database lookup."""
    results = _exec_search_products({
        "query":     q,
        "category":  category,
        "store":     store,
        "min_price": min_price,
        "max_price": max_price,
        "limit":     limit,
    })
    return {"results": results, "count": len(results)}


@router.get("/alex/deals")
async def alex_deals(
    category: Optional[str] = Query(None),
    limit:    int            = Query(8, le=30),
):
    """Top discounted products."""
    results = _exec_get_top_deals({"category": category, "limit": limit})
    return {"results": results, "count": len(results)}


# ── Alex Score ────────────────────────────────────────────────────────────────

# Pre-populated approximate medians — keeps alex_score fast on cold start
# (no full Supabase load needed).  _compute_medians() can refine them later.
_CATEGORY_MEDIANS: dict[str, float] = {
    "phones":     280.0,
    "laptops":    800.0,
    "tvs":        650.0,
    "headphones":  90.0,
    "tablets":    380.0,
    "gaming":     200.0,
    "fridges":    580.0,
    "washing":    560.0,
    "ac":         850.0,
    "vacuum":     180.0,
    "cooking":    450.0,
    "dishwasher": 600.0,
    "cameras":    620.0,
    "appliances": 360.0,
    "accessories": 45.0,
}


def _compute_medians() -> None:
    """Refine medians from actual data (called lazily, not on every request)."""
    offers = _load_local()
    from collections import defaultdict
    by_cat: dict[str, list[float]] = defaultdict(list)
    for o in offers:
        p = o.get("price")
        if p and o.get("category"):
            by_cat[o["category"]].append(p)
    for cat, prices in by_cat.items():
        prices.sort()
        _CATEGORY_MEDIANS[cat] = prices[len(prices) // 2]


def alex_score(product: dict) -> float:
    """Score a product 4.0–9.8 based on discount, price vs median, brand."""

    score = 6.0
    discount = product.get("discount_pct") or 0
    price    = product.get("price") or 0
    brand    = (product.get("brand") or "").lower()
    category = product.get("category") or ""

    # Discount bonus (up to +2.0)
    if   discount >= 40: score += 2.0
    elif discount >= 30: score += 1.5
    elif discount >= 20: score += 1.0
    elif discount >= 10: score += 0.5

    # Price position vs category median (cheaper = better)
    median = _CATEGORY_MEDIANS.get(category, 0)
    if median and price:
        ratio = price / median
        if   ratio < 0.5:  score += 1.0
        elif ratio < 0.75: score += 0.5
        elif ratio > 2.5:  score -= 0.5

    # Reputable brand bonus
    top_brands = {
        "samsung", "apple", "sony", "lg", "lenovo", "hp", "dell", "asus",
        "bose", "jbl", "sennheiser", "jabra", "xiaomi", "panasonic", "philips",
        "tcl", "hisense", "canon", "nikon", "logitech", "razer", "microsoft",
    }
    if brand in top_brands:
        score += 0.3

    # Has image and URL
    if product.get("image_url"): score += 0.1
    if product.get("url"):       score += 0.1

    return round(min(9.8, max(4.0, score)), 1)


# ── Expert Picks cache ────────────────────────────────────────────────────────

_PICKS_CACHE: dict[str, dict] = {}
_PICKS_TTL   = 6 * 3600  # 6 hours

_VERDICT_CACHE: dict[str, dict] = {}
_VERDICT_TTL   = 12 * 3600  # 12 hours

# ── Homepage Picks cache ───────────────────────────────────────────────────────

_HOME_PICKS_CACHE: dict | None = None
_HOME_PICKS_TS: float = 0.0

_HOME_CATEGORIES = [
    "phones", "laptops", "tvs", "headphones", "tablets", "gaming",
    "fridges", "washing", "ac", "vacuum",
]
_CAT_LABELS = {
    "phones": "Телефони", "headphones": "Слушалки", "laptops": "Лаптопи",
    "tvs": "Телевизори", "tablets": "Таблети", "gaming": "Гейминг",
    "fridges": "Хладилници", "washing": "Перални", "ac": "Климатици",
    "vacuum": "Прахосмукачки", "cooking": "Печки", "dishwasher": "Съдомиялни",
}

# Minimum price to be considered a meaningful product (filters out accessories/junk)
_CAT_MIN_PRICE = {
    "phones":     120.0,
    "headphones":  20.0,
    "laptops":    400.0,
    "tvs":        250.0,
    "tablets":    150.0,
    "gaming":      40.0,
    "fridges":    200.0,
    "washing":    250.0,
    "ac":         400.0,
    "vacuum":      60.0,
    "cooking":    150.0,
    "dishwasher": 250.0,
}

# ── Segment config: 3 price tiers per category ────────────────────────────────
SEGMENT_CONFIG: dict[str, list[dict]] = {
    "phones":     [
        {"key": "budget",  "label": "До 350€ — Добра стойност",         "emoji": "💰", "min_price": 120,  "max_price": 350},
        {"key": "mid",     "label": "350–700€ — Среден клас",           "emoji": "⚡", "min_price": 350,  "max_price": 700},
        {"key": "premium", "label": "700€+ — Без компромис",            "emoji": "👑", "min_price": 700,  "max_price": None},
    ],
    "laptops":    [
        {"key": "budget",  "label": "До 700€ — За работа",              "emoji": "💰", "min_price": 400,  "max_price": 700},
        {"key": "mid",     "label": "700–1200€ — Бизнес клас",          "emoji": "⚡", "min_price": 700,  "max_price": 1200},
        {"key": "premium", "label": "1200€+ — Топ производителност",    "emoji": "👑", "min_price": 1200, "max_price": None},
    ],
    "tvs":        [
        {"key": "budget",  "label": "До 500€ — Smart TV",               "emoji": "💰", "min_price": 250,  "max_price": 500},
        {"key": "mid",     "label": "500–1000€ — 4K QLED",              "emoji": "⚡", "min_price": 500,  "max_price": 1000},
        {"key": "premium", "label": "1000€+ — OLED & Голям екран",      "emoji": "👑", "min_price": 1000, "max_price": None},
    ],
    "headphones": [
        {"key": "budget",  "label": "До 80€ — Стойностни",              "emoji": "💰", "min_price": 20,   "max_price": 80},
        {"key": "mid",     "label": "80–200€ — С ANC",                  "emoji": "⚡", "min_price": 80,   "max_price": 200},
        {"key": "premium", "label": "200€+ — Премиум звук",             "emoji": "👑", "min_price": 200,  "max_price": None},
    ],
    "tablets":    [
        {"key": "budget",  "label": "До 300€ — За всеки",               "emoji": "💰", "min_price": 150,  "max_price": 300},
        {"key": "mid",     "label": "300–600€ — Производителност",       "emoji": "⚡", "min_price": 300,  "max_price": 600},
        {"key": "premium", "label": "600€+ — iPad Pro клас",             "emoji": "👑", "min_price": 600,  "max_price": None},
    ],
    "gaming":     [
        {"key": "budget",  "label": "До 150€ — Аксесоари & Игри",       "emoji": "💰", "min_price": 40,   "max_price": 150},
        {"key": "mid",     "label": "150–400€ — Конзоли",               "emoji": "⚡", "min_price": 150,  "max_price": 400},
        {"key": "premium", "label": "400€+ — Топ гейминг",              "emoji": "👑", "min_price": 400,  "max_price": None},
    ],
    "cameras":    [
        {"key": "budget",  "label": "До 500€ — Компактни",              "emoji": "💰", "min_price": 150,  "max_price": 500},
        {"key": "mid",     "label": "500–1000€ — Беззеркални",          "emoji": "⚡", "min_price": 500,  "max_price": 1000},
        {"key": "premium", "label": "1000€+ — Професионални",           "emoji": "👑", "min_price": 1000, "max_price": None},
    ],
    "appliances": [
        {"key": "budget",  "label": "До 300€ — Основни уреди",          "emoji": "💰", "min_price": 80,   "max_price": 300},
        {"key": "mid",     "label": "300–600€ — Smart уреди",           "emoji": "⚡", "min_price": 300,  "max_price": 600},
        {"key": "premium", "label": "600€+ — Топ клас",                 "emoji": "👑", "min_price": 600,  "max_price": None},
    ],
    "fridges":    [
        {"key": "budget",  "label": "До 500€ — Надеждни",               "emoji": "💰", "min_price": 200,  "max_price": 500},
        {"key": "mid",     "label": "500–900€ — No Frost",              "emoji": "⚡", "min_price": 500,  "max_price": 900},
        {"key": "premium", "label": "900€+ — Side-by-Side",             "emoji": "👑", "min_price": 900,  "max_price": None},
    ],
    "washing":    [
        {"key": "budget",  "label": "До 500€ — Достъпни",               "emoji": "💰", "min_price": 250,  "max_price": 500},
        {"key": "mid",     "label": "500–800€ — А клас",                "emoji": "⚡", "min_price": 500,  "max_price": 800},
        {"key": "premium", "label": "800€+ — Тихи & Smart",             "emoji": "👑", "min_price": 800,  "max_price": None},
    ],
    "ac":         [
        {"key": "budget",  "label": "До 700€ — Стандартни",             "emoji": "💰", "min_price": 400,  "max_price": 700},
        {"key": "mid",     "label": "700–1200€ — Инверторни",           "emoji": "⚡", "min_price": 700,  "max_price": 1200},
        {"key": "premium", "label": "1200€+ — Multi Split",             "emoji": "👑", "min_price": 1200, "max_price": None},
    ],
    "vacuum":     [
        {"key": "budget",  "label": "До 150€ — Традиционни",            "emoji": "💰", "min_price": 60,   "max_price": 150},
        {"key": "mid",     "label": "150–350€ — Безкабелни",            "emoji": "⚡", "min_price": 150,  "max_price": 350},
        {"key": "premium", "label": "350€+ — Роботи & Dyson",           "emoji": "👑", "min_price": 350,  "max_price": None},
    ],
    "cooking":    [
        {"key": "budget",  "label": "До 400€ — Стандартни",             "emoji": "💰", "min_price": 150,  "max_price": 400},
        {"key": "mid",     "label": "400–800€ — С конвекция",           "emoji": "⚡", "min_price": 400,  "max_price": 800},
        {"key": "premium", "label": "800€+ — Индукция & Smart",         "emoji": "👑", "min_price": 800,  "max_price": None},
    ],
    "dishwasher": [
        {"key": "budget",  "label": "До 500€ — Базови",                 "emoji": "💰", "min_price": 250,  "max_price": 500},
        {"key": "mid",     "label": "500–800€ — Тихи",                  "emoji": "⚡", "min_price": 500,  "max_price": 800},
        {"key": "premium", "label": "800€+ — Вградени & Smart",         "emoji": "👑", "min_price": 800,  "max_price": None},
    ],
}

# Keyword blocklist — products whose names contain these are excluded from candidates
_CAT_BLOCKLIST = {
    # Feature phones are named "Мобилен телефон GSM ..." in BG stores;
    # real smartphones are always "Смартфон GSM ..."  — this one filter is enough.
    "phones":     ["мобилен телефон"],
    "tablets":    ["рисуване", "drawing", "natec", "графичен", "wacom"],
    "gaming":     ["калъф", "case", "протектор", "стъкло", "кабел", "слушалк"],
    "headphones": ["калъф", "case", "кабел"],
}


def _home_pick_candidates(cat: str) -> list[dict]:
    """Return meaningful candidates for a category — filtered by price and blocklist."""
    prods = _local_search(query="", category=cat, limit=100)
    min_price = _CAT_MIN_PRICE.get(cat, 0)
    blocklist = [w.lower() for w in _CAT_BLOCKLIST.get(cat, [])]

    filtered = []
    for p in prods:
        if not p.get("image_url"):
            continue
        price = p.get("price") or 0
        if price < min_price:
            continue
        name_lower = (p.get("raw_name") or "").lower()
        if any(bl in name_lower for bl in blocklist):
            continue
        filtered.append(p)

    if not filtered:
        return []

    # Sort by alex_score, then pick from the mid-to-upper tier (skip absolute cheapest)
    filtered.sort(key=lambda p: alex_score(p), reverse=True)
    # Take top 12 by score, but cap at products up to 2.5× the minimum price or above
    sweet_spot = [p for p in filtered if (p.get("price") or 0) >= min_price * 1.3]
    candidates = sweet_spot[:10] if len(sweet_spot) >= 3 else filtered[:10]
    return candidates


def _generate_home_picks() -> list[dict]:
    settings = get_settings()
    sections: list[str] = []
    cat_products: dict[str, list[dict]] = {}

    for cat in _HOME_CATEGORIES:
        candidates = _home_pick_candidates(cat)
        if not candidates:
            continue
        cat_products[cat] = candidates

        # Include price context so Claude can reason about value tiers
        prices = [p["price"] for p in candidates]
        avg_price = sum(prices) / len(prices)
        lines = "\n".join(
            f"  - {p['raw_name']} | €{p['price']:.0f}"
            + (f" | -{p['discount_pct']}% намален" if p.get("discount_pct") else "")
            + f" | score {alex_score(p):.1f}"
            for p in candidates[:8]
        )
        sections.append(f"[{cat}] (средна цена сред кандидатите: €{avg_price:.0f})\n{lines}")

    if not sections:
        return []

    prompt = f"""Ти си независим AI съветник за електроника в България. Задачата ти е да намериш продуктите с НАЙ-ДОБРО съотношение качество-цена от списъка по-долу.

ВАЖНО — какво означава "добро качество-цена":
- НЕ е най-евтиният продукт в категорията
- Е продукт малко над дъното по цена, но значително по-добър по характеристики
- При лаптоп: предпочитай по-бърз процесор, повече RAM, по-голям екран пред само €50 пестене
- При телефон: предпочитай камера/5G/AMOLED пред base модел
- При слушалки: предпочитай ANC/безжични пред базови кабелни
- При телевизор: предпочитай Smart/4K пред HD-ready

Кандидати по категория:
{chr(10).join(sections)}

Избери 1-2 продукта от всяка категория, при които потребителят получава РЕАЛНО повече за малко повече пари. Обясни конкретно с кои характеристики — не пиши "висок score" или "добра цена", а конкретни неща (напр. "AMOLED екран и 5G за €180", "16GB RAM и SSD за €550").

Отговори САМО с валиден JSON (без обяснения, без markdown):
{{
  "picks": [
    {{"category": "laptops", "name": "ТОЧНО ИМЕ ОТ СПИСЪКА", "reason": "до 18 конкретни думи защо — с характеристики"}},
    ...
  ]
}}

Максимум 8 picks общо. Използвай ТОЧНИ имена от списъка."""

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())

        result = []
        for entry in data.get("picks", []):
            cat = entry.get("category", "")
            prods = cat_products.get(cat, [])
            prod = _match_product(entry.get("name", ""), prods)
            if not prod:
                continue
            result.append({
                "category":    cat,
                "cat_label":   _CAT_LABELS.get(cat, cat),
                "reason":      entry.get("reason", ""),
                "raw_name":    prod["raw_name"],
                "price":       prod["price"],
                "old_price":   prod.get("old_price"),
                "discount_pct": prod.get("discount_pct"),
                "store":       prod["store"],
                "image_url":   prod.get("image_url", ""),
                "url":         prod.get("url", ""),
                "alex_score":  alex_score(prod),
            })
        return result

    except Exception as exc:
        logger.error("[alex/homepage-picks] %s", exc)
        return []


@router.get("/alex/homepage-picks")
async def alex_homepage_picks(refresh: bool = False):
    """Cross-category AI-curated picks for the homepage (cached 6h)."""
    global _HOME_PICKS_CACHE, _HOME_PICKS_TS
    now = _time.time()
    if not refresh and _HOME_PICKS_CACHE is not None and (now - _HOME_PICKS_TS) < _PICKS_TTL:
        return {"picks": _HOME_PICKS_CACHE}
    picks = _generate_home_picks()
    _HOME_PICKS_CACHE = picks
    _HOME_PICKS_TS = now
    return {"picks": picks}


def _match_product(name: str, products: list[dict]) -> dict | None:
    name_l = name.lower()
    for p in products:
        if name_l in (p.get("raw_name") or "").lower():
            return p
    words = name_l.split()[:5]
    best, best_hits = None, 0
    for p in products:
        pname = (p.get("raw_name") or "").lower()
        hits  = sum(1 for w in words if len(w) > 2 and w in pname)
        if hits > best_hits:
            best, best_hits = p, hits
    return best if best_hits >= 2 else None


def _picks_candidates(category: str) -> list[dict]:
    """Stratified fetch: 20 products from each price tier so picks cover budget+mid+premium."""
    min_price = _CAT_MIN_PRICE.get(category, 0)
    blocklist = [w.lower() for w in _CAT_BLOCKLIST.get(category, [])]

    # Build tier ranges from SEGMENT_CONFIG; fall back to two broad buckets
    segs = SEGMENT_CONFIG.get(category, [])
    tiers: list[tuple[float, float | None]] = (
        [(s["min_price"], s["max_price"]) for s in segs]
        if segs else
        [(min_price, min_price * 4), (min_price * 4, None)]
    )

    all_prods: list[dict] = []
    try:
        sb = get_supabase()
        for lo, hi in tiers:
            q = (
                sb.table("electronics_offers")
                .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url")
                .eq("category", category)
                .not_.is_("image_url", "null")
                .neq("image_url", "")
                .gte("price", lo)
            )
            if hi is not None:
                q = q.lte("price", hi)
            resp = q.order("price", desc=False).limit(20).execute()
            all_prods.extend(resp.data or [])
    except Exception as exc:
        logger.warning("[alex/picks_candidates] Supabase failed for %s: %s", category, exc)
        all_prods = [
            o for o in _load_local()
            if o.get("category") == category
            and o.get("image_url")
            and (o.get("price") or 0) >= min_price
        ]

    candidates = []
    for p in all_prods:
        name_lower = (p.get("raw_name") or "").lower()
        if any(bl in name_lower for bl in blocklist):
            continue
        candidates.append({
            "raw_name":    p.get("raw_name", ""),
            "brand":       p.get("brand", ""),
            "category":    category,
            "price":       p.get("price") or 0,
            "old_price":   p.get("old_price"),
            "discount_pct": p.get("discount_pct"),
            "store":       p.get("store", ""),
            "image_url":   p.get("image_url", ""),
            "url":         p.get("url", ""),
        })

    candidates.sort(key=lambda x: alex_score(x), reverse=True)
    return candidates[:25]


def _generate_picks(category: str) -> dict | None:
    settings = get_settings()
    all_prods = _picks_candidates(category)
    if len(all_prods) < 4:
        return None

    # Group candidates by SEGMENT tier so Claude sees budget / mid / premium separately.
    # Sorting ALL by Alex Score first would push every cheap product to the top
    # (they get price-below-median bonus) and Claude would never see mid/premium.
    segs = SEGMENT_CONFIG.get(category)
    if segs:
        tier_blocks: list[str] = []
        for seg in segs:
            lo, hi = seg["min_price"], seg["max_price"]
            tier_prods = [
                p for p in all_prods
                if p["price"] >= lo and (hi is None or p["price"] <= hi)
            ]
            tier_prods.sort(key=lambda x: alex_score(x), reverse=True)
            if tier_prods:
                lines = "\n".join(
                    f"  - {p['raw_name']} | €{p['price']:.0f}"
                    + (f" | -{p['discount_pct']}%" if p.get("discount_pct") else "")
                    for p in tier_prods[:7]
                )
                tier_blocks.append(f"[{seg['label']}]\n{lines}")
        product_list = "\n\n".join(tier_blocks) if tier_blocks else ""
    else:
        product_list = "\n".join(
            f"- {p['raw_name']} | €{p['price']:.0f}" for p in all_prods[:20]
        )

    if not product_list:
        return None

    prompt = f"""Ти си независим AI съветник за електроника в България.
Избери 4 продукта от категория "{category}" — ЗАДЪЛЖИТЕЛНО от РАЗЛИЧНИ ценови нива.

{product_list}

ДЕФИНИЦИИ (спазвай ги стриктно):
• best_value   = НАЙ-ДОБРО съотношение цена-качество — НЕ задължително най-евтин.
                 Търси продукт от СРЕДЕН клас, при когото за малко повече пари получаваш значително повече функции.
• best_budget  = Най-добрият избор от НАЙ-НИСКИЯ ценови клас.
• mid_range    = Препоръка от СРЕДНИЯ ценови клас.
• overall_best = Топ продукт без компромис — от НАЙ-ГОРНИЯ клас.

ПРАВИЛА:
- best_value и best_budget НЕ могат да са с близки цени (разликата трябва да е поне 40%).
- overall_best трябва да е от най-горния ценови клас.
- Пиши ТОЧНИ имена от списъка.

Отговори САМО с валиден JSON (без обяснения, без markdown):
{{
  "best_value":   {{"name": "ТОЧНО ИМЕ", "reason": "до 15 думи — конкретни предимства"}},
  "best_budget":  {{"name": "ТОЧНО ИМЕ", "reason": "до 15 думи"}},
  "mid_range":    {{"name": "ТОЧНО ИМЕ", "reason": "до 15 думи"}},
  "overall_best": {{"name": "ТОЧНО ИМЕ", "reason": "до 15 думи"}},
  "verdict": "2-3 изречения за текущото състояние на пазара в тази категория"
}}"""

    try:
        client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = response.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())

        # Enrich picks with real product data
        labels = {
            "best_value":   {"label": "Най-добра стойност", "icon": "💰"},
            "best_budget":  {"label": "Най-добър бюджет",   "icon": "🎯"},
            "mid_range":    {"label": "Среден клас",         "icon": "⚡"},
            "overall_best": {"label": "Без компромис",       "icon": "👑"},
        }
        picks = {"verdict": data.get("verdict", ""), "items": []}
        for key, meta in labels.items():
            entry = data.get(key, {})
            prod  = _match_product(entry.get("name", ""), all_prods)
            if prod:
                picks["items"].append({
                    "key":       key,
                    "label":     meta["label"],
                    "icon":      meta["icon"],
                    "reason":    entry.get("reason", ""),
                    "raw_name":  prod["raw_name"],
                    "price":     prod["price"],
                    "old_price": prod.get("old_price"),
                    "discount_pct": prod.get("discount_pct"),
                    "store":     prod["store"],
                    "image_url": prod.get("image_url", ""),
                    "url":       prod.get("url", ""),
                    "alex_score": alex_score(prod),
                })
        return picks

    except Exception as exc:
        logger.error("[alex/picks] %s: %s", category, exc)
        return None


@router.get("/alex/category/{category}")
async def alex_category_products(
    category: str,
    brand:     Optional[str]   = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    sort:      str              = Query("price_asc"),
    limit:     int              = Query(60, le=120),
):
    """Products for a category page — direct Supabase query with local fallback."""
    # Try Supabase first (fast, no full-table load)
    try:
        sb = get_supabase()
        pg_sort = {
            "price_asc":  ("price", False),
            "price_desc": ("price", True),
            "discount":   ("discount_pct", True),
            "score":      ("price", False),
        }.get(sort, ("price", False))

        q = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url")
            .eq("category", category)
            .not_.is_("image_url", "null")
            .neq("image_url", "")
        )
        if brand:
            q = q.ilike("brand", brand)
        if min_price:
            q = q.gte("price", min_price)
        if max_price:
            q = q.lte("price", max_price)

        # Get total count first
        total_resp = (
            sb.table("electronics_offers")
            .select("id", count="exact")
            .eq("category", category)
            .not_.is_("image_url", "null")
            .neq("image_url", "")
            .execute()
        )
        total_count = total_resp.count or 0

        resp = q.order(pg_sort[0], desc=pg_sort[1]).limit(limit).execute()
        if resp.data is not None:
            results = [
                {**r, "alex_score": alex_score(r)}
                for r in resp.data
            ]
            if sort == "score":
                results.sort(key=lambda x: x["alex_score"], reverse=True)
            return {"results": results, "count": len(results), "total_count": total_count}
    except Exception as exc:
        logger.warning("[alex] category Supabase failed, using local: %s", exc)

    # Fallback: local JSON
    offers = _load_local()
    cat_offers = [o for o in offers if o.get("category") == category and o.get("image_url")]
    total_count = len(cat_offers)
    results = []
    for o in cat_offers:
        price = o.get("price") or 0
        if brand and (o.get("brand") or "").lower() != brand.lower():
            continue
        if min_price and price < min_price:
            continue
        if max_price and price > max_price:
            continue
        results.append({
            "raw_name":    o.get("raw_name", ""),
            "brand":       o.get("brand", ""),
            "category":    category,
            "price":       price,
            "old_price":   o.get("old_price"),
            "discount_pct": o.get("discount_pct"),
            "store":       o.get("store", ""),
            "image_url":   o.get("image_url", ""),
            "url":         o.get("url", ""),
            "alex_score":  alex_score(o),
        })

    if   sort == "price_desc": results.sort(key=lambda x: x["price"], reverse=True)
    elif sort == "discount":   results.sort(key=lambda x: x.get("discount_pct") or 0, reverse=True)
    elif sort == "score":      results.sort(key=lambda x: x["alex_score"], reverse=True)
    else:                      results.sort(key=lambda x: x["price"])

    return {"results": results[:limit], "count": len(results), "total_count": total_count}


@router.get("/alex/brands/{category}")
async def alex_brands(category: str):
    """Unique brands for a category — direct Supabase query."""
    try:
        sb = get_supabase()
        resp = (
            sb.table("electronics_offers")
            .select("brand")
            .eq("category", category)
            .not_.is_("brand", "null")
            .neq("brand", "")
            .execute()
        )
        if resp.data is not None:
            brands = sorted(set(r["brand"].strip() for r in resp.data if r.get("brand")))
            return {"brands": brands}
    except Exception as exc:
        logger.warning("[alex] brands Supabase failed, using local: %s", exc)

    offers = _load_local()
    brands = sorted(set(
        o.get("brand", "").strip()
        for o in offers
        if o.get("category") == category and o.get("brand")
    ))
    return {"brands": brands}


@router.get("/alex/picks/{category}")
async def alex_picks_endpoint(category: str):
    """Expert picks for a category, cached 6 h."""
    now    = _time.time()
    cached = _PICKS_CACHE.get(category)
    if cached and now - cached.get("_ts", 0) < _PICKS_TTL:
        return {k: v for k, v in cached.items() if k != "_ts"}

    # Run sync Anthropic client in thread pool — never block the event loop
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, _generate_picks, category)
    if picks is None:
        raise HTTPException(status_code=404, detail=f"Not enough products for {category}")

    _PICKS_CACHE[category] = {**picks, "_ts": now}
    return picks


@router.get("/alex/segments/{category}")
async def alex_segments(category: str):
    """3 price-segment rows for the category page — top 6 products per tier."""
    segs = SEGMENT_CONFIG.get(category)
    if not segs:
        raise HTTPException(status_code=404, detail=f"No segment config for {category}")

    result: list[dict] = []
    for seg in segs:
        prods: list[dict] = []
        try:
            sb = get_supabase()
            q = (
                sb.table("electronics_offers")
                .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url")
                .eq("category", category)
                .not_.is_("image_url", "null")
                .neq("image_url", "")
                .gte("price", seg["min_price"])
            )
            if seg["max_price"] is not None:
                q = q.lte("price", seg["max_price"])
            resp = q.order("price", desc=False).limit(40).execute()
            prods = resp.data or []
        except Exception as exc:
            logger.warning("[alex/segments] Supabase failed for %s/%s: %s", category, seg["key"], exc)
            for o in _load_local():
                if o.get("category") != category or not o.get("image_url"):
                    continue
                price = o.get("price") or 0
                if price < seg["min_price"]:
                    continue
                if seg["max_price"] is not None and price > seg["max_price"]:
                    continue
                prods.append(o)

        top6 = sorted(
            [{**p, "alex_score": alex_score(p)} for p in prods],
            key=lambda x: x["alex_score"],
            reverse=True,
        )[:6]

        result.append({
            "key":      seg["key"],
            "label":    seg["label"],
            "emoji":    seg["emoji"],
            "products": top6,
        })

    return {"segments": result}


@router.get("/alex/verdict")
async def alex_verdict_endpoint(
    name:     str            = Query(...),
    price:    float          = Query(...),
    store:    str            = Query(...),
    category: Optional[str] = Query(""),
):
    """Short Claude Haiku verdict for a specific product (cached 12 h)."""
    cache_key = f"{name}|{store}"
    cached = _VERDICT_CACHE.get(cache_key)
    if cached and _time.time() - cached["ts"] < _VERDICT_TTL:
        return {"verdict": cached["verdict"]}

    settings = get_settings()
    if not settings.anthropic_api_key:
        return {"verdict": ""}

    prompt = (
        f"Ти си Alex — независим AI съветник за електроника в България.\n"
        f"Дай кратко мнение (2-3 изречения, максимум 60 думи) за:\n\n"
        f"Продукт: {name}\nЦена: €{price:.2f}\nМагазин: {store}\n"
        + (f"Категория: {category}\n" if category else "")
        + "\nБъди директен. Кажи дали си заслужава, за кого е подходящ, и ключовото предимство. "
          "НЕ повтаряй цената или магазина в отговора."
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        verdict = resp.content[0].text.strip()
        _VERDICT_CACHE[cache_key] = {"verdict": verdict, "ts": _time.time()}
        return {"verdict": verdict}
    except Exception as exc:
        logger.warning("[alex/verdict] %s", exc)
        return {"verdict": ""}


@router.get("/alex/price-history")
async def price_history(url: str = Query(..., description="Product URL")):
    """Return 30-day price history for a product + deal authenticity score."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    try:
        sb = get_supabase()
        resp = (
            sb.table("price_history")
            .select("price, old_price, scraped_at")
            .eq("product_url", url)
            .gte("scraped_at", cutoff)
            .order("scraped_at", desc=False)
            .limit(60)
            .execute()
        )
        rows = resp.data or []
    except Exception as exc:
        logger.warning("[alex/price-history] %s", exc)
        rows = []

    deal_score = "unknown"
    if rows:
        prices = [r["price"] for r in rows]
        max_real = max(prices)
        # Check the most recent old_price claim
        latest_old = next(
            (r["old_price"] for r in reversed(rows) if r.get("old_price")), None
        )
        if latest_old:
            deal_score = "real" if max_real >= latest_old * 0.88 else "suspicious"

    return {
        "history": rows,
        "deal_score": deal_score,  # "real" | "suspicious" | "unknown"
        "data_points": len(rows),
    }


class WatchlistAddRequest(BaseModel):
    user_id: str
    email: str
    product_url: str
    store: str
    raw_name: str
    category: Optional[str] = None
    image_url: Optional[str] = None
    target_price: float
    current_price: float


@router.post("/alex/watchlist")
async def watchlist_add(req: WatchlistAddRequest):
    """Add a product to the user's watchlist."""
    try:
        sb = get_supabase()
        sb.table("watchlists").upsert({
            "user_id":          req.user_id,
            "email":            req.email,
            "product_url":      req.product_url,
            "store":            req.store,
            "raw_name":         req.raw_name,
            "category":         req.category,
            "image_url":        req.image_url,
            "target_price":     req.target_price,
            "last_known_price": req.current_price,
        }, on_conflict="user_id,product_url").execute()
        return {"ok": True}
    except Exception as exc:
        logger.error("[alex/watchlist] add failed: %s", exc)
        raise HTTPException(status_code=500, detail="Грешка при запис")


@router.get("/alex/watchlist/{user_id}")
async def watchlist_get(user_id: str):
    """Return all watchlist items for a user."""
    try:
        sb = get_supabase()
        resp = (
            sb.table("watchlists")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        items = []
        for row in (resp.data or []):
            # Enrich with current price from electronics_offers
            try:
                cur = (
                    sb.table("electronics_offers")
                    .select("price")
                    .eq("url", row["product_url"])
                    .limit(1)
                    .execute()
                )
                row["current_price"] = cur.data[0]["price"] if cur.data else row.get("last_known_price")
            except Exception:
                row["current_price"] = row.get("last_known_price")
            items.append(row)
        return {"items": items}
    except Exception as exc:
        logger.error("[alex/watchlist] get failed: %s", exc)
        return {"items": []}


@router.delete("/alex/watchlist/{item_id}")
async def watchlist_remove(item_id: int, user_id: str = Query(...)):
    """Remove a watchlist item (user must own it)."""
    try:
        sb = get_supabase()
        sb.table("watchlists").delete().eq("id", item_id).eq("user_id", user_id).execute()
        return {"ok": True}
    except Exception as exc:
        logger.error("[alex/watchlist] remove failed: %s", exc)
        raise HTTPException(status_code=500, detail="Грешка при изтриване")


@router.get("/alex/stats")
async def alex_stats():
    """Quick stats for the Alex homepage."""
    try:
        sb = get_supabase()
        total  = sb.table("electronics_offers").select("id", count="exact").execute()
        stores = sb.table("electronics_offers").select("store").execute()
        store_counts: dict[str, int] = {}
        for row in (stores.data or []):
            store_counts[row["store"]] = store_counts.get(row["store"], 0) + 1
        if total.count:
            return {"total_products": total.count, "stores": store_counts}
    except Exception as exc:
        logger.warning("[alex] Supabase stats failed, using local JSON: %s", exc)

    # Fallback: count from local JSON
    offers = _load_local()
    store_counts = {}
    for o in offers:
        s = o.get("store", "unknown")
        store_counts[s] = store_counts.get(s, 0) + 1
    return {"total_products": len(offers), "stores": store_counts}
