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
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
import anthropic

from api.config import get_settings
from api.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["alex"])

# ── Image proxy ────────────────────────────────────────────────────────────────
# Bulgarian store CDNs block hotlinking. We proxy images server-side to avoid 403s.

_ALLOWED_IMG_HOSTS = {
    "cdn.emag.bg", "emag.bg",
    "technopolis.bg", "www.technopolis.bg",
    "technomarket.bg", "www.technomarket.bg",
    "ardes.bg", "www.ardes.bg",
    "technomix.bg", "www.technomix.bg",
    "zorashop.bg", "www.zorashop.bg",
    "frankfurt.apollo.olxcdn.com",
    "bazar.bg", "www.bazar.bg",
    "img.bazar.bg",
}


@router.get("/alex/img")
async def img_proxy(url: str = Query(...)):
    """Proxy product images to bypass store hotlink protection."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(400, "Invalid URL scheme")
        host = parsed.netloc.lstrip("www.").lower()
        # Allow if host or its parent is in whitelist
        if not any(parsed.netloc.lower() == h or parsed.netloc.lower().endswith("." + h)
                   for h in _ALLOWED_IMG_HOSTS):
            raise HTTPException(403, "Host not allowed")

        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Pazarko/1.0)",
                "Referer": parsed.scheme + "://" + parsed.netloc + "/",
            })
        if r.status_code != 200:
            raise HTTPException(502, f"Upstream returned {r.status_code}")

        ct = r.headers.get("content-type", "image/jpeg")
        return Response(
            content=r.content,
            media_type=ct,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("[img-proxy] %s — %s", url[:80], exc)
        raise HTTPException(502, "Could not fetch image")


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


# Non-electronics keywords that slip through scraper categorisation
_NON_ELECTRONICS = [
    "кафе", "nescafe", "lavazza", "dolce gusto", "espresso capsul",
    "капсул", "прах за", "перил", "препарат", "сапун", "дезодорант",
    "шоколад", "бисквит", "чай", "вода", "сок", "бира", "вино",
]


def _is_electronics(raw_name: str) -> bool:
    name = (raw_name or "").lower()
    return not any(kw in name for kw in _NON_ELECTRONICS)


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
        if not _is_electronics(o.get("raw_name", "")):
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
СТЪПКА 0 — КЛАСИФИКАЦИЯ НА НАМЕРЕНИЕТО
════════════════════════════════════════

Преди всеки отговор определи типа заявка:

▶ BROWSING  — обща/неясна заявка без бюджет и употреба ("търся лаптоп", "искам слушалки")
   → Режим: ASK_FIRST
   → Задай максимум 2 кратки въпроса (бюджет + употреба). НЕ търси и НЕ показвай таблица.
   → При второ съобщение или ако потребителят е нетърпелив — действай с наличното.

▶ SPECIFIC  — конкретен модел или марка ("Samsung S25", "Sony WH-1000XM5", "iPhone 16")
   → Режим: SPEAK — директно търси и отговори без уточняващи въпроси.

▶ COMPARISON — явно сравнение ("X срещу Y", "кой е по-добър X или Y")
   → Режим: SPEAK — сравнителна таблица. Не търси отново ако вече имаш данните.

▶ BUDGET_MATCH — категория + бюджет ("лаптоп до 800 лв.", "телефон около 400€")
   → Режим: SPEAK — директно търси в ценовия диапазон.

▶ EXPLANATION — обяснение на препоръка или термин ("защо препоръча X", "какво е OLED")
   → Режим: SPEAK — отговори от вече намерените данни или от знанията си. Не търси отново.

════════════════════════════════════════
РЕЖИМИ НА ОТГОВОР
════════════════════════════════════════

SPEAK                   → пълен отговор с таблица и препоръки
SPEAK_WITH_CONSTRAINTS  → отговор + ясна забележка за ограничение на данните
ASK_FIRST               → само 1-2 въпроса, без таблица и без търсене

════════════════════════════════════════
АБСОЛЮТНИ ПРАВИЛА (нарушението е грешка)
════════════════════════════════════════

❌ ЗАБРАНЕНО: Цена, която не е от базата данни
❌ ЗАБРАНЕНО: Приблизителни цени (~€45, "около €50", "между €40-60")
❌ ЗАБРАНЕНО: Да препоръчваш продукт, който не е намерен в търсенето
❌ ЗАБРАНЕНО: "лв." — само EUR (€)
❌ ЗАБРАНЕНО: Таблица с продукти БЕЗ колона "Линк"
❌ ЗАБРАНЕНО: "Мога ли да помогна с нещо друго?" — задай конкретен въпрос

✅ ЗАДЪЛЖИТЕЛНО: Само продукти от search_products резултатите в таблиците
✅ ЗАДЪЛЖИТЕЛНО: Точни цени от базата — нито стотинка повече или по-малко
✅ ЗАДЪЛЖИТЕЛНО: Всеки продукт в таблицата има линк [Виж →](url)

════════════════════════════════════════
ЧЕСТНОСТ И ЕПИСТЕМНА СКРОМНОСТ
════════════════════════════════════════

✅ Казвай КОЛКО резултата реално си намерил: "От 8-те модела, които намерих..."
✅ При < 3 реални резултата → SPEAK_WITH_CONSTRAINTS + "Намерих само X модела, данните са ограничени."
✅ Спецификации от твоето знание (не от базата) → маркирай: "По спецификация на модела: ..."
✅ При несигурност → кажи директно: "Не разполагам с тази информация в момента."

❌ НЕ сравнявай с продукти, които НЕ СИ търсил в тази сесия
❌ НЕ казвай "X е най-добрият на пазара" — казвай "От намерените, X предлага най-добро съотношение"
❌ НЕ давай цени по памет — само от базата

════════════════════════════════════════
САМОПРОВЕРКА ПРЕДИ ОТГОВОР (Thesis Breaker)
════════════════════════════════════════

Преди да изпратиш препоръката, провери мълчаливо:
□ Отговарям ли на реалния въпрос — не на по-лесна версия?
□ Намерих ли ≥ 3 продукта за обективна препоръка?
□ Всички цени в таблицата — от базата ли са?
□ Препоръката отчита ли бюджета/употребата, споменати по-рано в разговора?
□ Прекалено ли е дълъг отговорът за простотата на въпроса?

Ако отговорът на някое е НЕ → SPEAK_WITH_CONSTRAINTS или съкрати отговора.

════════════════════════════════════════
KILL CONDITION — ЗАДЪЛЖИТЕЛНО ЗА ТОП 1 И 2
════════════════════════════════════════

Под всяка от първите две препоръки добави:
> ⚠️ Не е за теб ако: [едно конкретно условие, при което тази препоръка не важи]

Примери:
- ⚠️ Не е за теб ако: имаш нужда само от офис работа — виж по-евтин вариант
- ⚠️ Не е за теб ако: бюджетът е под €600 — тогава Nitro V15 е по-добрият избор
- ⚠️ Не е за теб ако: ползваш основно iOS екосистемата

════════════════════════════════════════
ЧЕСТНА ТЕХНИЧЕСКА ОЦЕНКА
════════════════════════════════════════

Когато има значима разлика между маркетинг и реалност, използвай формата:
**Общото мнение:** [X]. **Истината:** [Y].

Примери:
- **Общото мнение:** RTX 4060 е достатъчен за 1080p gaming. **Истината:** при AAA заглавия с Ray Tracing производителността пада чувствително.
- **Общото мнение:** OLED = по-добро за всичко. **Истината:** при ярко осветени стаи IPS панелите са по-четими.
- **Общото мнение:** повече мегапиксели = по-добра камера. **Истината:** сензорният размер и обработката на изображения имат по-голямо значение.

Използвай САМО когато разликата е значима и добавя реална стойност. Не преувеличавай.

════════════════════════════════════════
ПАМЕТ И ПРИЕМСТВЕНОСТ В РАЗГОВОРА
════════════════════════════════════════

✅ Следи какво е намерено и казано по-рано в разговора
✅ При follow-up адаптирай: "Преди намерих X до €Y. Сега ще потърся в по-висок диапазон..."
✅ При ново ограничение ("само Sony", "само за игри") → филтрирай от ВЕЧЕ намереното, не търси отново
✅ При ново изискване, изискващо нова категория или марка → ново търсене

❌ НЕ третирай всяко съобщение като нов разговор — изгради върху казаното

════════════════════════════════════════
ПРАВИЛО ЗА КРАТКОСТ (Development Stop)
════════════════════════════════════════

При прост въпрос → прост отговор. Не добавяй:
- Технически обяснения без питане
- Исторически контексти
- Секции, които потребителят не е поискал
- Повече от 2 препоръки при директен въпрос ("кой е по-добър X или Y?")

Тест: ако можеш да отговориш в 2 изречения → направи го в 2 изречения.

════════════════════════════════════════
СТРАТЕГИЯ ЗА ТЪРСЕНЕ
════════════════════════════════════════

При "безжични слушалки до 100€" направи ПОСЛЕДОВАТЕЛНО:
1. search_products(query="bluetooth", category="headphones", max_price=100, limit=20)
2. Ако < 5 резултата → search_products(query="wireless", category="headphones", max_price=100, limit=20)
3. Ако пак < 5 → search_products(query="", category="headphones", max_price=100, limit=20)
4. Работи с намереното — не измисляй

По марка: "Sony слушалки" → query="Sony", category="headphones"
По размер TV: query="55", category="tvs"
НЕ търси с български думи — само английски: "bluetooth", "wireless", "Samsung", "laptop"

════════════════════════════════════════
ГЕЙМИНГ ЛАПТОПИ — СПЕЦИАЛНА СТРАТЕГИЯ
════════════════════════════════════════

❗ В базата продуктите НЕ съдържат "gaming" или "RTX" в имената!
Търси по КОНКРЕТНИ гейминг серии:

При "лаптоп за игри" / "gaming laptop" → направи ВСИЧКИ:
1. search_products(query="LOQ", category="laptops", limit=20)
2. search_products(query="Legion", category="laptops", limit=20)
3. search_products(query="Nitro", category="laptops", limit=20)
4. search_products(query="ROG", category="laptops", limit=20)
5. search_products(query="Victus", category="laptops", limit=20)
6. Ако < 5 общо → search_products(query="TUF", category="laptops", limit=20)

❌ НЕ търси: "gaming", "RTX", "GeForce"
✅ ТЪРСИ: "LOQ", "Legion", "Nitro", "ROG", "Victus", "Omen", "TUF", "Cyborg"

При бюджет → добави max_price: budget≈800, mid≈1400, premium≈2000+

════════════════════════════════════════
СТРУКТУРА НА ОТГОВОРА (при SPEAK режим)
════════════════════════════════════════

**СТЪПКА 1 — ТЪРСЕНЕ:**
Извикай search_products. При нужда — 2-3 различни заявки за по-добро покритие.

**СТЪПКА 2 — ТОП ПРОДУКТИ ОТ НАМЕРЕНОТО:**

| # | Критерий | Модел | Цена | Защо? | Линк |
|---|----------|-------|------|-------|------|
| 🥇 | Най-добра стойност | [реален модел от базата] | €XX | ... | [Виж →](url) |
| 🥈 | Бюджетен избор | [реален модел от базата] | €XX | ... | [Виж →](url) |
| 🥉 | Премиум | [реален модел от базата] | €XX | ... | [Виж →](url) |
| 4️⃣ | За [конкретна употреба] | [реален модел от базата] | €XX | ... | [Виж →](url) |
| 5️⃣ | Скрита перла | [реален модел от базата] | €XX | ... | [Виж →](url) |

След №1 и №2 → добави ⚠️ Не е за теб ако: [условие]
Ако имаш < 3 реални резултата — показвай само намерените + SPEAK_WITH_CONSTRAINTS.

**СТЪПКА 3 — СРАВНЕНИЕ НА ТОП 2 (само при > 1 резултат):**
Цените — точно от базата. Спецификациите — от знанията ти за модела, маркирани "По спецификация: ...".

| Характеристика | [Модел 1] | [Модел 2] |
|---|---|---|
| Цена | €XX | €XX |
| Магазин | store | store |
| Линк | [Виж →](url) | [Виж →](url) |
| [Спец] | По спецификация: ... | По спецификация: ... |

**СТЪПКА 4 — КОНКРЕТЕН ВЪПРОС:**
Завърши с конкретен следващ въпрос. Никога с "Мога ли да помогна с нещо друго?"

════════════════════════════════════════
FOLLOW-UP ПРАВИЛА
════════════════════════════════════════

- Follow-up (сравни, кой е по-добър, ти кой би избрал) → НЕ търси отново, работи с намереното
- Ново търсене само при нова категория, нова марка или нов продукт
- Помни бюджет, употреба и предпочитания, споменати по-рано

════════════════════════════════════════
КОГА ДА КУПЯ (get_buy_timing)
════════════════════════════════════════

Викай get_buy_timing когато потребителят пита:
  "кога да купя", "добра ли е цената", "ще поевтинее ли", "чакам ли"
Подавай product_url ако имаш URL от предишно търсене.

При интерпретация на резултата:
• verdict=buy_now + at_historical_min → "Цената е на историческо дъно — добър момент"
• verdict=wait + at_historical_max + trend=rising → "Цената расте, изчакай"
• trend=falling → "Цената пада последно — вероятно ще продължи"
• has_history=False → ползвай общите си познания за сезонни цикли:
  - Черен петък (ноември): ~15-25% намаления на техника
  - Нова година (декември-януари): добри оферти на ТВ и лаптопи
  - Back to school (август-септември): намаления на лаптопи и таблети
  - Смяна на модел (напр. нов iPhone/Samsung): старият пада 15-20%

════════════════════════════════════════
НОВИ МОДЕЛИ — КОГА ДА ИЗЧАКАШ
════════════════════════════════════════

При препоръка на продукт, ЗАДЪЛЖИТЕЛНО провери дали предстои нов модел:
• iPhone → нов модел всеки септември; ако е юли-август → препоръчай изчакване
• Samsung Galaxy S → нов модел всеки януари; ако е ноември-декември → изчакай
• MacBook → Apple обновява ~веднъж годишно; следи Apple Event обяви
• Sony WH слушалки → нов XM модел на ~2 години
• PlayStation / Xbox → дълги цикли (5-7 г.); текущото поколение е актуално

Ако предстои нов модел скоро: "⚠️ Очаква се [нов модел] след ~X месеца.
Старият ще поевтинее с ~15-20%. Препоръчвам да изчакаш ако не бързаш."

════════════════════════════════════════
BUDGET DISTRIBUTOR — РАЗПРЕДЕЛЕНИЕ НА БЮДЖЕТ
════════════════════════════════════════

Когато потребителят дава общ бюджет за НЯКОЛКО продукта (setup, комплект):
1. Предложи разпределение по категории (напр. "800 лв. работен setup")
2. Направи search_products за всеки компонент с правилния max_price
3. Представи като таблица с компонент / препоръчан продукт / цена

Стандартни разпределения (ориентировъчни):
• Работен setup: 60% лаптоп, 15% монитор, 15% слушалки, 10% периферия
• Гейминг setup: 65% лаптоп/конзола, 20% монитор, 10% слушалки, 5% аксесоари
• Домашно кино: 70% ТВ, 20% звукова система, 10% стриймър

════════════════════════════════════════
МАГАЗИНИ — НАДЕЖДНОСТ И ОСОБЕНОСТИ
════════════════════════════════════════

eMAG: Най-бърза доставка (до 24ч), голям избор, добра политика за връщане.
  Идеален за: спешни покупки, стоки с бърза наличност.

Технополис: Физически магазини с демо зали, отлично гаранционно обслужване.
  Идеален за: скъпа техника, когато искаш да видиш продукта на живо, сервиз.

Техномаркет: Широка мрежа физически магазини, добро обслужване.
  Идеален за: покупки с консултация на място.

Ардес: Онлайн фокус, конкурентни цени.
  Идеален за: добра цена без нужда от физически магазин.

Зора: По-малък онлайн магазин.
  Идеален за: когато другите нямат наличност.

Когато препоръчваш магазин — не само цената, но и тези особености.
Пример: "eMAG е €2 по-скъп, но доставя утре и с безплатно връщане."

════════════════════════════════════════
ВТОРА РЪКА (search_secondhand)
════════════════════════════════════════

Викай search_secondhand САМО когато:
1. Вече си показал нови цени от магазини (след search_products или get_prices)
2. Продуктът е конкретен модел с ясно название (не "евтин лаптоп")
3. Категорията е phones / laptops / tvs / tablets И цената е над 300 лв.
   — ИЛИ потребителят изрично пита за "втора ръка", "употребявано", "second hand"

❌ НЕ викай за: слушалки, аксесоари, малки уреди, неизвестен модел
✅ ВИКАЙ за: "iPhone 13 Pro", "Samsung S23", "MacBook Air M2", "LG 55 OLED"

При представяне — не разписвай всяка обява. Спомени броя и препоръчай да погледнат картите.
Пример: "Намерих и 5 обяви втора ръка (3 в OLX, 2 в Bazar) — виж картите по-долу."

**Магазини:** eMAG · Технополис · Ардес · Техномаркет
**Категории:** phones · laptops · tvs · headphones · tablets · gaming · cameras · accessories · cooking (печки/котлони/фурни) · washing (перални/сушилни) · fridges (хладилници) · vacuum (прахосмукачки) · ac (климатици) · dishwasher (съдомиялни) · appliances (др. уреди)
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
                    "description": (
                        "Категория (незадължително). Използвай точния slug: "
                        "phones=смартфони, laptops=лаптопи, tvs=телевизори, headphones=слушалки, "
                        "tablets=таблети, gaming=геймърско, cameras=фотоапарати, "
                        "cooking=печки/котлони/фурни, washing=перални/сушилни, "
                        "fridges=хладилници/фризери, vacuum=прахосмукачки/роботи, "
                        "ac=климатици, dishwasher=съдомиялни, "
                        "appliances=други домакински уреди, accessories=аксесоари"
                    ),
                    "enum": [
                        "headphones", "phones", "laptops", "tvs", "tablets",
                        "gaming", "cameras", "accessories",
                        "cooking", "washing", "fridges", "vacuum", "ac", "dishwasher",
                        "appliances"
                    ]
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
                    "description": (
                        "Категория: phones, laptops, tvs, headphones, tablets, gaming, cameras, accessories, "
                        "cooking, washing, fridges, vacuum, ac, dishwasher, appliances"
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": "Брой оферти (по подразбиране 8)",
                    "default": 8
                }
            },
            "required": []
        }
    },
    {
        "name": "get_buy_timing",
        "description": (
            "Анализира историята на цените на продукт и дава препоръка 'купи сега' или 'изчакай'. "
            "Използвай когато потребителят пита 'кога да купя', 'добра ли е цената сега', "
            "'ще поевтинее ли', 'чакам ли промоция'. "
            "Подавай URL на продукта ако го имаш, иначе подавай product_name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_url": {
                    "type": "string",
                    "description": "URL на продукта в магазина (предпочитано)"
                },
                "product_name": {
                    "type": "string",
                    "description": "Название на продукта ако няма URL"
                }
            },
            "required": []
        }
    },
    {
        "name": "estimate_tradein",
        "description": (
            "Оценява колко струва дадено устройство на вторичния пазар (OLX). "
            "Използвай когато потребителят пита 'колко мога да продам моя X', "
            "'trade-in стойност', 'колко е стар ми Y на вторичния пазар', "
            "или когато предлагаш надграждане и искаш да покажеш реалната доплата."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "Устройство за оценка (напр. 'iPhone 13 Pro 256GB', 'Samsung Galaxy S22', 'MacBook Air M2')"
                }
            },
            "required": ["device"]
        }
    },
    {
        "name": "search_secondhand",
        "description": (
            "Търси обяви за употребявани продукти в OLX.bg и Bazar.bg. "
            "Използвай СЛЕД като вече си показал цени от магазини, когато: "
            "(1) продуктът е конкретен модел (напр. 'iPhone 13 Pro', 'Samsung S22'), "
            "(2) категорията е phones/laptops/tvs/tablets и цената е над 300 лв., "
            "(3) или потребителят изрично пита за втора ръка / употребявано / second hand. "
            "НЕ използвай за аксесоари, малки уреди или при неизвестен точен модел."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Конкретен модел за търсене (напр. 'iPhone 13 Pro', 'Samsung Galaxy S22', 'MacBook Air M2')"
                },
                "max_per_source": {
                    "type": "integer",
                    "description": "Брой резултати от всеки сайт (по подразбиране 4, макс 8)",
                    "default": 4
                }
            },
            "required": ["query"]
        }
    }
]

# ── Tool execution ────────────────────────────────────────────────────────────

def _exec_search_products(args: dict) -> list[dict]:
    query = args.get("query", "").strip()
    _log_search(query, args.get("category"))
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


def _log_search(query: str, category: str | None = None) -> None:
    """Fire-and-forget: log search query for trending analytics."""
    if not query or len(query.strip()) < 2:
        return
    try:
        sb = get_supabase()
        sb.table("search_queries").insert({
            "query":    query.lower().strip()[:120],
            "category": category or None,
        }).execute()
    except Exception:
        pass


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
            clean = [r for r in resp.data if _is_electronics(r.get("raw_name", ""))]
            return _dedup_deals(clean, limit)
    except Exception as exc:
        logger.warning("[alex] Supabase get_top_deals failed, using local JSON: %s", exc)

    return _dedup_deals(_local_deals(category=args.get("category"), limit=60), limit)


def _exec_get_buy_timing(args: dict) -> dict:
    product_url  = (args.get("product_url")  or "").strip()
    product_name = (args.get("product_name") or "").strip()

    if not product_url and not product_name:
        return {"has_history": False, "message": "Подай product_url или product_name"}

    try:
        sb = get_supabase()
        q  = sb.table("price_history").select("price, scraped_at, store")
        if product_url:
            q = q.eq("product_url", product_url)
        else:
            q = q.ilike("raw_name", f"%{product_name}%")
        resp    = q.order("scraped_at", desc=False).limit(60).execute()
        history = resp.data or []
    except Exception as exc:
        logger.warning("[buy_timing] DB error: %s", exc)
        history = []

    if len(history) < 2:
        return {
            "has_history": False,
            "data_points": len(history),
            "message": (
                "Недостатъчна ценова история в базата. "
                "Използвай общите си познания за сезонни цикли и пазарни тенденции."
            ),
        }

    prices  = [float(r["price"]) for r in history if r.get("price")]
    current = prices[-1]
    mn, mx  = min(prices), max(prices)
    avg     = round(sum(prices) / len(prices), 2)

    # Trend: last third vs first third
    chunk      = max(1, len(prices) // 3)
    recent_avg = sum(prices[-chunk:]) / chunk
    early_avg  = sum(prices[:chunk])  / chunk
    if recent_avg < early_avg * 0.97:
        trend = "falling"
    elif recent_avg > early_avg * 1.03:
        trend = "rising"
    else:
        trend = "stable"

    at_min = current <= mn * 1.03
    at_max = current >= mx * 0.97

    return {
        "has_history": True,
        "data_points": len(prices),
        "current_price": current,
        "min_price":     mn,
        "max_price":     mx,
        "avg_price":     avg,
        "trend":         trend,
        "at_historical_min": at_min,
        "at_historical_max": at_max,
        "verdict": (
            "buy_now"  if at_min or trend == "falling" else
            "wait"     if at_max and trend == "rising"  else
            "neutral"
        ),
    }


def _exec_estimate_tradein(args: dict) -> dict:
    from alex.scrapers.olx import search_olx

    device = (args.get("device") or "").strip()
    if not device:
        return {"error": "Не е зададено устройство"}

    listings = search_olx(device, max_results=10)
    prices   = sorted(
        p for l in listings
        if (p := l.get("price", 0)) and p > 50
    )

    if not prices:
        return {"found": 0, "message": f"Няма намерени обяви за '{device}' в OLX"}

    # Drop top and bottom outlier if enough data
    if len(prices) >= 5:
        cut    = max(1, len(prices) // 5)
        prices = prices[cut:-cut]

    median = sorted(prices)[len(prices) // 2]
    low    = round(median * 0.85)   # conservative seller estimate
    high   = round(median * 1.00)

    return {
        "found":          len(listings),
        "median_price":   round(median),
        "estimated_low":  low,
        "estimated_high": high,
        "currency":       "BGN",
        "sample_count":   len(prices),
        "top_listings":   listings[:3],
        "advice": (
            f"При продажба в OLX очаквай {low}–{high} лв. "
            f"(базирано на {len(listings)} обяви)."
        ),
    }


def _exec_search_secondhand(args: dict) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from alex.scrapers.olx   import search_olx
    from alex.scrapers.bazar import search_bazar

    query      = (args.get("query") or "").strip()
    max_each   = min(int(args.get("max_per_source", 4)), 8)

    if not query:
        return {"secondhand": [], "message": "Няма зададена заявка"}

    olx_res, bazar_res = [], []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {
            ex.submit(search_olx,   query, max_each): "olx",
            ex.submit(search_bazar, query, max_each): "bazar",
        }
        for fut in as_completed(futs):
            src = futs[fut]
            try:
                data = fut.result()
            except Exception as exc:
                logger.warning("[secondhand] %s failed: %s", src, exc)
                data = []
            if src == "olx":
                olx_res = data
            else:
                bazar_res = data

    combined = olx_res + bazar_res
    return {
        "secondhand":  combined,
        "olx_count":   len(olx_res),
        "bazar_count": len(bazar_res),
    }


def _run_tool(tool_name: str, tool_input: dict) -> Any:
    if tool_name == "search_products":
        return _exec_search_products(tool_input)
    if tool_name == "get_prices":
        return _exec_get_prices(tool_input)
    if tool_name == "get_top_deals":
        return _exec_get_top_deals(tool_input)
    if tool_name == "search_secondhand":
        return _exec_search_secondhand(tool_input)
    if tool_name == "get_buy_timing":
        return _exec_get_buy_timing(tool_input)
    if tool_name == "estimate_tradein":
        return _exec_estimate_tradein(tool_input)
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
                if tc["name"] == "search_secondhand":
                    listings = raw.get("secondhand", []) if isinstance(raw, dict) else []
                    if listings:
                        yield f"data: {json.dumps({'secondhand': listings})}\n\n"
                elif isinstance(raw, list) and raw:
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


def _picks_candidates_by_tier(category: str) -> dict[str, list[dict]]:
    """Fetch top-scored products per price tier. Returns {tier_key: [products]}."""
    segs = SEGMENT_CONFIG.get(category, [])
    if not segs:
        return {}
    blocklist = [w.lower() for w in _CAT_BLOCKLIST.get(category, [])]

    def _clean(p: dict) -> dict:
        return {
            "raw_name":    p.get("raw_name", ""),
            "brand":       p.get("brand", ""),
            "category":    category,
            "price":       p.get("price") or 0,
            "old_price":   p.get("old_price"),
            "discount_pct": p.get("discount_pct"),
            "store":       p.get("store", ""),
            "image_url":   p.get("image_url", ""),
            "url":         p.get("url", ""),
        }

    result: dict[str, list[dict]] = {}
    try:
        sb = get_supabase()
        for seg in segs:
            lo, hi = seg["min_price"], seg["max_price"]
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
            resp = q.order("price", desc=False).limit(30).execute()
            prods = [
                _clean(p) for p in (resp.data or [])
                if not any(bl in (p.get("raw_name") or "").lower() for bl in blocklist)
            ]
            prods.sort(key=lambda x: alex_score(x), reverse=True)
            result[seg["key"]] = prods
    except Exception as exc:
        logger.warning("[alex/picks_candidates] Supabase failed for %s: %s", category, exc)
        # local fallback — bucket by price range
        local_all = [
            _clean(o) for o in _load_local()
            if o.get("category") == category
            and o.get("image_url")
            and not any(bl in (o.get("raw_name") or "").lower() for bl in blocklist)
        ]
        for seg in segs:
            lo, hi = seg["min_price"], seg["max_price"]
            bucket = [p for p in local_all if p["price"] >= lo and (hi is None or p["price"] <= hi)]
            bucket.sort(key=lambda x: alex_score(x), reverse=True)
            result[seg["key"]] = bucket

    return result


def _generate_picks(category: str) -> dict | None:
    """
    Python selects one product strictly from each price tier (no duplicates).
    Claude only writes short reason text + verdict — it cannot change which product is shown.
    """
    settings = get_settings()
    segs = SEGMENT_CONFIG.get(category)
    if not segs:
        return None

    tier_prods = _picks_candidates_by_tier(category)

    # Slot definitions: (slot_key, tier_key, label, icon)
    # budget→best_budget, mid→best_value, mid→mid_range (2nd choice), premium→overall_best
    # Split mid tier by median price so best_value ≠ mid_range in cost
    mid_prods = tier_prods.get("mid", [])
    if mid_prods:
        mid_prices = sorted(p["price"] for p in mid_prods)
        mid_split  = mid_prices[len(mid_prices) // 2]          # median
        mid_low    = [p for p in mid_prods if p["price"] <= mid_split]
        mid_high   = [p for p in mid_prods if p["price"] >  mid_split]
        if not mid_low:  mid_low  = mid_prods[:5]
        if not mid_high: mid_high = mid_prods[-5:]
    else:
        mid_low = mid_high = []

    # Slot → candidate pool
    SLOTS: list[tuple[str, list[dict], str, str]] = [
        ("best_budget",  tier_prods.get("budget",  []), "Най-добър бюджет",   "🎯"),
        ("best_value",   mid_low,                        "Най-добра стойност",  "💰"),
        ("mid_range",    mid_high,                       "Среден клас",          "⚡"),
        ("overall_best", tier_prods.get("premium", []), "Без компромис",        "👑"),
    ]

    # Pick one product per slot — deduplicate by URL *and* brand
    used_urls:   set[str] = set()
    used_brands: set[str] = set()
    selected: list[tuple[str, str, str, dict]] = []

    def _brand(p: dict) -> str:
        return (p.get("brand") or p.get("raw_name", "").split()[0]).strip().lower()

    def _pick_from(candidates: list[dict], strict_brand: bool = True) -> dict | None:
        for p in candidates:
            if p.get("url") in used_urls:
                continue
            if strict_brand and _brand(p) in used_brands:
                continue
            return p
        if strict_brand:
            return _pick_from(candidates, strict_brand=False)  # relax brand on retry
        return None

    for slot_key, candidates, label, icon in SLOTS:
        prod = _pick_from(candidates)
        if prod is None:
            # Fallback: try all tiers
            for seg in segs:
                prod = _pick_from(tier_prods.get(seg["key"], []))
                if prod:
                    break
        if prod:
            selected.append((slot_key, label, icon, prod))
            used_urls.add(prod.get("url", f"__no_url_{slot_key}"))
            used_brands.add(_brand(prod))

    if len(selected) < 2:
        return None

    # Ask Claude only for reason text + verdict (not product selection)
    prod_lines = "\n".join(
        f'{slot_key}: {prod["raw_name"]} | €{prod["price"]:.0f} | {prod["store"]}'
        for slot_key, _, _, prod in selected
    )
    prompt = f"""Ти си AI съветник за електроника. Тези продукти са избрани за категория "{category}".
Напиши за всеки кратка причина (до 12 думи) — конкретни предимства, НЕ повтаряй цената.
Добави и verdict: 2 изречения за пазара в тази категория.

{prod_lines}

Отговори САМО с JSON (без markdown):
{{
  "best_budget": "причина",
  "best_value": "причина",
  "mid_range": "причина",
  "overall_best": "причина",
  "verdict": "2 изречения"
}}"""

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        reasons: dict = json.loads(match.group()) if match else {}
    except Exception as exc:
        logger.error("[alex/picks] reason gen failed for %s: %s", category, exc)
        reasons = {}

    picks: dict = {"verdict": reasons.get("verdict", ""), "items": []}
    for slot_key, label, icon, prod in selected:
        picks["items"].append({
            "key":          slot_key,
            "label":        label,
            "icon":         icon,
            "reason":       reasons.get(slot_key, ""),
            "raw_name":     prod["raw_name"],
            "price":        prod["price"],
            "old_price":    prod.get("old_price"),
            "discount_pct": prod.get("discount_pct"),
            "store":        prod["store"],
            "image_url":    prod.get("image_url", ""),
            "url":          prod.get("url", ""),
            "alex_score":   alex_score(prod),
        })
    return picks


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


@router.get("/alex/trends/{category}")
async def alex_trends(category: str):
    """Daily avg price sparkline + top price-drop products for the last 30 days."""
    from collections import defaultdict
    try:
        sb = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        hist = (
            sb.table("price_history")
            .select("price, scraped_at, raw_name, product_url, store, image_url")
            .eq("category", category)
            .gte("scraped_at", cutoff)
            .execute()
        )
        rows = hist.data or []

        # Daily average prices
        daily: dict = defaultdict(list)
        for r in rows:
            if r.get("price"):
                daily[r["scraped_at"][:10]].append(float(r["price"]))
        daily_avg = sorted(
            [{"date": d, "avg": round(sum(v) / len(v), 2)} for d, v in daily.items()],
            key=lambda x: x["date"],
        )

        # Top price drops: earliest vs latest price per URL
        url_rows: dict = defaultdict(list)
        for r in rows:
            if r.get("product_url") and r.get("price"):
                url_rows[r["product_url"]].append(r)

        drops = []
        for url, pr in url_rows.items():
            pr.sort(key=lambda r: r["scraped_at"])
            if len(pr) < 2:
                continue
            p_old = float(pr[0]["price"])
            p_new = float(pr[-1]["price"])
            if p_old <= p_new:
                continue
            drop_pct = round((p_old - p_new) / p_old * 100, 1)
            if drop_pct < 2:
                continue
            lr = pr[-1]
            drops.append({
                "raw_name":  lr.get("raw_name", ""),
                "url":       url,
                "store":     lr.get("store", ""),
                "image_url": lr.get("image_url"),
                "price_now": p_new,
                "price_was": p_old,
                "drop_pct":  drop_pct,
            })
        drops.sort(key=lambda x: -x["drop_pct"])

        # Current market stats
        offers = (
            sb.table("electronics_offers")
            .select("price, discount_pct")
            .eq("category", category)
            .execute()
        )
        all_offers = offers.data or []
        prices  = [float(o["price"]) for o in all_offers if o.get("price")]
        on_sale = [o for o in all_offers if (o.get("discount_pct") or 0) > 0]
        avg_disc = round(sum(o["discount_pct"] for o in on_sale) / len(on_sale), 1) if on_sale else 0

        return {
            "daily_avg":     daily_avg,
            "top_drops":     drops[:4],
            "avg_price":     round(sum(prices) / len(prices), 2) if prices else 0,
            "on_sale_count": len(on_sale),
            "total_count":   len(all_offers),
            "avg_discount":  avg_disc,
            "data_days":     len(daily_avg),
        }
    except Exception as exc:
        logger.error("[alex/trends] %s: %s", category, exc)
        raise HTTPException(status_code=500, detail="Грешка при зареждане")


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
        row = {
            "user_id":          req.user_id,
            "email":            req.email,
            "product_url":      req.product_url,
            "store":            req.store,
            "raw_name":         req.raw_name,
            "category":         req.category,
            "image_url":        req.image_url,
            "target_price":     req.target_price,
            "last_known_price": req.current_price,
        }
        # Check if already tracked — update target price; otherwise insert
        existing = (
            sb.table("watchlists")
            .select("id")
            .eq("user_id", req.user_id)
            .eq("product_url", req.product_url)
            .limit(1)
            .execute()
        )
        if existing.data:
            sb.table("watchlists").update({"target_price": req.target_price, "email": req.email}).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.table("watchlists").insert(row).execute()
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


@router.get("/alex/deal-check")
async def deal_check(
    product_url:   str   = Query(...),
    product_name:  str   = Query(""),
    current_price: float = Query(...),
    old_price:     float = Query(0),
    store:         str   = Query(""),
):
    """
    Verify if a discount is genuine.
    Compares against competitor prices and 30-day price history.
    Returns verdict: real | suspicious | unknown | no_discount
    """
    if not old_price or old_price <= current_price:
        return {"verdict": "no_discount", "reason": "Няма посочено намаление.", "competitors": [], "avg_30d": None, "history_points": 0}

    claimed_pct = round((1 - current_price / old_price) * 100, 1)

    competitors: list[dict] = []
    avg_30d: float | None = None
    history_points = 0

    try:
        sb = get_supabase()

        # ── 1. Competitor prices (same URL, all stores) ──────────────────
        comp_resp = (
            sb.table("electronics_offers")
            .select("store, price, url")
            .eq("url", product_url)
            .execute()
        )
        for row in (comp_resp.data or []):
            competitors.append({"store": row["store"], "price": float(row["price"])})

        # ── 2. Price history (last 30 days) ──────────────────────────────
        from datetime import timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        hist_resp = (
            sb.table("price_history")
            .select("price, scraped_at")
            .eq("product_url", product_url)
            .gte("scraped_at", cutoff)
            .execute()
        )
        hist_prices = [float(r["price"]) for r in (hist_resp.data or []) if r.get("price")]
        history_points = len(hist_prices)
        if history_points >= 3:
            avg_30d = round(sum(hist_prices) / len(hist_prices), 2)

    except Exception as exc:
        logger.warning("[deal-check] DB error: %s", exc)

    # ── 3. Verdict ────────────────────────────────────────────────────────
    other_stores = [c for c in competitors if c["store"] != store]
    prices_all   = [c["price"] for c in competitors]
    prices_other = [c["price"] for c in other_stores]

    verdict    = "unknown"
    confidence = "low"
    reason     = "Недостатъчно данни за сравнение."

    if avg_30d and history_points >= 3:
        # History-based verdict (most reliable)
        if old_price > avg_30d * 1.15:
            verdict    = "suspicious"
            confidence = "high"
            reason     = f"Историческата средна цена е {avg_30d:.0f} лв. — обявената стара цена е {((old_price/avg_30d-1)*100):.0f}% по-висока от реалната пазарна."
        elif current_price <= avg_30d * 1.05:
            verdict    = "real"
            confidence = "high"
            reason     = f"Текущата цена ({current_price:.0f} лв.) е под или близо до историческата средна ({avg_30d:.0f} лв.). Намалението изглежда реално."
        else:
            verdict    = "suspicious"
            confidence = "medium"
            reason     = f"Цената e над историческата средна от {avg_30d:.0f} лв. — намалението може да е частично."

    elif prices_other:
        max_comp = max(prices_other)
        min_comp = min(prices_other)
        avg_comp = round(sum(prices_other) / len(prices_other), 2)

        if old_price > max_comp * 1.20:
            verdict    = "suspicious"
            confidence = "medium"
            reason     = f"Конкурентите продават на макс. {max_comp:.0f} лв. — обявената стара цена ({old_price:.0f} лв.) е {((old_price/max_comp-1)*100):.0f}% по-висока от пазара."
        elif current_price <= avg_comp * 1.05:
            verdict    = "real"
            confidence = "medium"
            reason     = f"Текущата цена ({current_price:.0f} лв.) съответства на пазарното ниво (средно {avg_comp:.0f} лв.). Намалението е реално спрямо конкурентите."
        elif current_price <= min_comp * 1.10:
            verdict    = "real"
            confidence = "medium"
            reason     = f"Цената е близо до най-евтиния конкурент ({min_comp:.0f} лв.)."
        else:
            verdict    = "unknown"
            confidence = "low"
            reason     = f"Конкурентните цени варират ({min_comp:.0f}–{max_comp:.0f} лв.). Не може да се прецени еднозначно."

    elif len(prices_all) >= 2:
        # Same store, multiple history entries
        avg_all = round(sum(prices_all) / len(prices_all), 2)
        verdict    = "unknown"
        confidence = "low"
        reason     = f"Само един магазин — не може да се сравни с конкуренти."

    return {
        "verdict":        verdict,       # real | suspicious | unknown | no_discount
        "confidence":     confidence,    # high | medium | low
        "reason":         reason,
        "claimed_pct":    claimed_pct,
        "current_price":  current_price,
        "old_price":      old_price,
        "competitors":    sorted(competitors, key=lambda c: c["price"]),
        "avg_30d":        avg_30d,
        "history_points": history_points,
    }


@router.post("/alex/run-alerts")
async def run_alerts(secret: str = Query("")):
    """
    Check all watchlist items and send email alerts when target price is hit.
    Call this after every scrape (push_direct.py) or via a cron job.
    Pass ?secret=<SECRET_KEY> to prevent unauthorized triggers.
    """
    settings = get_settings()
    if settings.secret_key and secret != settings.secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    smtp_user = settings.smtp_user
    smtp_pass = settings.smtp_pass
    if not smtp_user or not smtp_pass:
        return {"ok": False, "error": "SMTP credentials not configured", "sent": 0}

    try:
        sb = get_supabase()
        resp = sb.table("watchlists").select("*").execute()
        items = resp.data or []
    except Exception as exc:
        logger.error("[alerts] Failed to load watchlists: %s", exc)
        raise HTTPException(status_code=500, detail="DB error")

    from api.email_utils import send_price_alert
    from datetime import timezone, timedelta

    sent = 0
    skipped = 0
    errors = 0

    for item in items:
        try:
            cur = (
                sb.table("electronics_offers")
                .select("price, url, raw_name, store, image_url")
                .eq("url", item["product_url"])
                .limit(1)
                .execute()
            )
            if not cur.data:
                skipped += 1
                continue

            prod = cur.data[0]
            current_price = float(prod["price"])
            target_price  = float(item["target_price"])

            if current_price > target_price:
                sb.table("watchlists").update({"last_known_price": current_price}).eq("id", item["id"]).execute()
                skipped += 1
                continue

            # 24-hour cooldown
            alerted_at = item.get("alerted_at")
            if alerted_at:
                last = datetime.fromisoformat(alerted_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last < timedelta(hours=24):
                    skipped += 1
                    continue

            ok = send_price_alert(
                to_email=item["email"],
                product_name=item["raw_name"],
                current_price=current_price,
                target_price=target_price,
                store=prod["store"],
                product_url=prod["url"],
                image_url=item.get("image_url"),
                smtp_user=smtp_user,
                smtp_pass=smtp_pass,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                from_name=settings.alert_from_name,
            )
            if ok:
                sb.table("watchlists").update({
                    "alerted_at":       datetime.now(timezone.utc).isoformat(),
                    "last_known_price": current_price,
                }).eq("id", item["id"]).execute()
                sent += 1
            else:
                errors += 1

        except Exception as exc:
            logger.error("[alerts] Error on item %s: %s", item.get("id"), exc)
            errors += 1

    logger.info("[alerts] done — sent=%d skipped=%d errors=%d", sent, skipped, errors)
    return {"ok": True, "sent": sent, "skipped": skipped, "errors": errors}


@router.post("/alex/test-email")
async def test_email(secret: str = Query("")):
    """Send a test email to verify SMTP config. Returns {ok, message}."""
    settings = get_settings()
    if settings.secret_key and secret != settings.secret_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    from api.email_utils import send_test_email
    ok, msg = send_test_email(
        smtp_user=settings.smtp_user,
        smtp_pass=settings.smtp_pass,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
    )
    return {"ok": ok, "message": msg}


@router.get("/alex/related")
async def related_products(
    category:    str   = Query(""),
    price:       float = Query(0),
    exclude_url: str   = Query(""),
    limit:       int   = Query(6),
):
    """Return related products: same category, price ±40%, different URL."""
    limit = min(limit, 12)
    try:
        sb = get_supabase()
        q = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, price, old_price, discount_pct, store, image_url, url, alex_score")
            .eq("category", category)
            .order("alex_score", desc=True)
            .limit(40)
        )
        if price:
            q = q.gte("price", price * 0.6).lte("price", price * 1.4)
        resp = q.execute()
        results = [r for r in (resp.data or []) if r.get("url") != exclude_url]
        return {"results": results[:limit]}
    except Exception as exc:
        logger.warning("[related] DB failed, using local: %s", exc)
        offers = _load_local()
        results = [
            o for o in offers
            if o.get("category") == category
            and o.get("url") != exclude_url
            and (not price or abs(float(o.get("price", 0)) - price) / max(price, 1) <= 0.4)
        ]
        results.sort(key=lambda x: x.get("alex_score", 0) or 0, reverse=True)
        return {"results": results[:limit]}


@router.get("/alex/trending")
async def trending_searches(
    category: str = Query(""),
    limit:    int = Query(8),
    days:     int = Query(7),
):
    """Return top searched queries in the last N days."""
    limit = min(limit, 20)
    try:
        from datetime import timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sb = get_supabase()
        q = sb.table("search_queries").select("query").gte("searched_at", cutoff)
        if category:
            q = q.eq("category", category)
        resp = q.execute()
        counts: dict[str, int] = {}
        for row in (resp.data or []):
            w = (row.get("query") or "").strip()
            if w and len(w) >= 2:
                counts[w] = counts.get(w, 0) + 1
        top = sorted(counts.items(), key=lambda x: -x[1])[:limit]
        return {"trending": [{"query": q, "count": c} for q, c in top]}
    except Exception as exc:
        logger.warning("[trending] failed: %s", exc)
        return {"trending": []}


_CAT_LABELS = {
    "phones": "Смартфони", "laptops": "Лаптопи", "tvs": "Телевизори",
    "headphones": "Слушалки", "tablets": "Таблети", "gaming": "Геймърско",
    "cameras": "Фотоапарати", "appliances": "Уреди", "cooking": "Готварски уреди",
    "washing": "Перални", "fridges": "Хладилници", "vacuum": "Прахосмукачки",
    "ac": "Климатици", "dishwasher": "Съдомиялни", "accessories": "Аксесоари",
}

_KNOWN_BRANDS = {
    "samsung", "apple", "sony", "lg", "xiaomi", "huawei", "lenovo", "hp",
    "dell", "asus", "acer", "philips", "bosch", "bose", "jbl", "logitech",
    "panasonic", "hisense", "tcl", "canon", "nikon", "nintendo", "dyson",
}


def _alex_score(p: dict) -> float:
    """Simple value score 0-10 for homepage picks ranking."""
    score = 5.0
    disc  = p.get("discount_pct") or 0
    price = p.get("price") or 0
    brand = (p.get("brand") or p.get("raw_name", "").split()[0]).lower()

    if disc >= 30:   score += 2.5
    elif disc >= 20: score += 1.5
    elif disc >= 10: score += 0.8

    if brand in _KNOWN_BRANDS: score += 1.0

    # Sweet spot: 50-500 EUR
    if 50 <= price <= 500: score += 0.5

    return round(min(score, 10.0), 1)


@router.get("/alex/homepage-picks")
async def homepage_picks(limit: int = Query(12, le=24)):
    """Curated Alex picks for the homepage grid — best-value products across categories."""
    try:
        sb = get_supabase()
        resp = (
            sb.table("electronics_offers")
            .select("raw_name, brand, category, category_raw, price, old_price, discount_pct, store, image_url, url")
            .not_.is_("image_url", "null")
            .neq("image_url", "")
            .not_.is_("discount_pct", "null")
            .gt("discount_pct", 5)
            .order("discount_pct", desc=True)
            .limit(200)
            .execute()
        )
        candidates = [r for r in (resp.data or []) if _is_electronics(r.get("raw_name", ""))]
    except Exception:
        candidates = [
            r for r in _load_local()
            if r.get("image_url") and r.get("discount_pct", 0) > 5 and _is_electronics(r.get("raw_name", ""))
        ]

    # One pick per category, highest score wins
    by_cat: dict[str, dict] = {}
    for p in candidates:
        cat = p.get("category", "other")
        p["alex_score"] = _alex_score(p)
        p["cat_label"]  = _CAT_LABELS.get(cat, cat)
        if cat not in by_cat or p["alex_score"] > by_cat[cat]["alex_score"]:
            by_cat[cat] = p

    picks = sorted(by_cat.values(), key=lambda x: x["alex_score"], reverse=True)[:limit]
    return {"picks": picks, "count": len(picks)}


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


# ── "Моята електроника" — user device CRUD ────────────────────────────────────

class DeviceIn(BaseModel):
    device_name:     str
    brand:           Optional[str] = None
    category:        Optional[str] = None
    purchase_date:   Optional[str] = None   # ISO date string YYYY-MM-DD
    purchase_price:  Optional[float] = None
    store:           Optional[str] = None
    warranty_months: int = 24
    image_url:       Optional[str] = None
    product_url:     Optional[str] = None
    notes:           Optional[str] = None


@router.get("/alex/devices")
async def get_devices(user_id: str = Query(...)):
    """List all devices owned by this user."""
    try:
        sb   = get_supabase()
        resp = (
            sb.table("user_devices")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"devices": resp.data or []}
    except Exception as exc:
        logger.error("[devices] get failed: %s", exc)
        raise HTTPException(status_code=500, detail="Грешка при зареждане на устройствата")


@router.post("/alex/devices")
async def add_device(user_id: str = Query(...), body: DeviceIn = ...):
    """Add a device to the user's collection."""
    try:
        sb  = get_supabase()
        row = {
            "user_id":         user_id,
            "device_name":     body.device_name.strip(),
            "brand":           body.brand,
            "category":        body.category,
            "purchase_date":   body.purchase_date,
            "purchase_price":  body.purchase_price,
            "store":           body.store,
            "warranty_months": body.warranty_months,
            "image_url":       body.image_url,
            "product_url":     body.product_url,
            "notes":           body.notes,
        }
        resp = sb.table("user_devices").insert(row).execute()
        return {"device": resp.data[0] if resp.data else row}
    except Exception as exc:
        logger.error("[devices] add failed: %s", exc)
        raise HTTPException(status_code=500, detail="Грешка при запис на устройството")


@router.delete("/alex/devices/{device_id}")
async def delete_device(device_id: int, user_id: str = Query(...)):
    """Delete a device (only if it belongs to this user)."""
    try:
        sb = get_supabase()
        sb.table("user_devices").delete().eq("id", device_id).eq("user_id", user_id).execute()
        return {"ok": True}
    except Exception as exc:
        logger.error("[devices] delete failed: %s", exc)
        raise HTTPException(status_code=500, detail="Грешка при изтриване")
