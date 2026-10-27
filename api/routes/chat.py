"""
AI Chat — Pazarko Assistant
Персонализиран асистент за пазаруване с памет за навиците на потребителя
"""

from __future__ import annotations
import logging
import json
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import anthropic

from api.config import get_settings
from api.db import get_supabase
from api.routes.alex import _chat_rate_check

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# Legacy (храни) чат — без auth, затова евтин модел + таван. Беше Opus 4.5 без лимит.
_LEGACY_MODEL      = "claude-haiku-4-5-20251001"
_LEGACY_MAX_TOKENS = 600
_LEGACY_MAX_MSGS   = 12      # само последните N съобщения от клиента
_LEGACY_MAX_CHARS  = 2000    # на съобщение
_LIMIT_MSG = "Достигна дневния лимит въпроси за днес. Нулира се в полунощ."


def _trim(messages: List["ChatMessage"]) -> list[dict]:
    out = [{"role": m.role, "content": (m.content or "")[:_LEGACY_MAX_CHARS]}
           for m in messages[-_LEGACY_MAX_MSGS:]]
    while out and out[0]["role"] != "user":   # API-то иска първото да е от user
        out.pop(0)
    return out


# ── models ────────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str       # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    user_id: Optional[str] = None
    session_id: Optional[str] = None


# ── system prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(user_dna: Optional[dict] = None) -> str:
    base = """Ти си Пазарко — персонализиран AI асистент за умно пазаруване в България.

Помагаш на потребителите да:
- Намерят най-евтините продукти в Kaufland, Billa, Фантастико и eBag
- Сравнят цени и спестят пари
- Планират списъци за пазаруване
- Разберат тенденциите в инфлацията на хранителни продукти
- Открият оферти и промоции

Говориш на БЪЛГАРСКИ. Отговарябш кратко, конкретно и полезно.
Когато цитираш цени, добавяй "лв." след числото.
Когато препоръчваш магазин, обяснявай защо (цена, разстояние, асортимент).

НЕ измисляш цени — ако нямаш реален резултат от търсачката, казваш го честно.
"""

    if user_dna:
        savings = user_dna.get("total_saved", 0)
        sensitivity = user_dna.get("price_sensitivity", 0.5)
        stores = user_dna.get("preferred_stores", [])
        dietary = user_dna.get("dietary_tags", [])

        persona = f"""
Профил на потребителя (Shopping DNA):
- Спестил(а) досега: {savings:.2f} лв. с Пазарко
- Ценова чувствителност: {"висока" if sensitivity > 0.6 else "умерена" if sensitivity > 0.3 else "ниска"}
- Предпочитани магазини: {", ".join(stores) if stores else "все още не е посочил"}
- Диетични предпочитания: {", ".join(dietary) if dietary else "без особени"}

Персонализирай препоръките спрямо този профил.
"""
        base += persona

    return base


# ── streaming chat ────────────────────────────────────────────────────────────

async def _stream_response(
    messages: List[ChatMessage],
    system: str,
) -> AsyncIterator[str]:
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    msg_dicts = _trim(messages)

    async with client.messages.stream(
        model=_LEGACY_MODEL,
        max_tokens=_LEGACY_MAX_TOKENS,
        system=system,
        messages=msg_dicts,
    ) as stream:
        async for text in stream.text_stream:
            yield f"data: {json.dumps({'text': text})}\n\n"

    yield "data: [DONE]\n\n"


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/chat")
async def chat(req: ChatRequest, http_request: Request):
    """
    Streaming chat endpoint — returns SSE stream.
    Automatically loads user DNA if user_id provided.
    """
    if not req.messages:
        raise HTTPException(status_code=400, detail="Няма съобщения")

    # Общ дневен таван по IP с /alex/chat
    allowed, _ = _chat_rate_check(http_request)
    if not allowed:
        async def _limit_stream():
            yield f"data: {json.dumps({'text': _LIMIT_MSG})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_limit_stream(), media_type="text/event-stream")

    # Load Shopping DNA for personalization
    user_dna = None
    if req.user_id:
        try:
            sb = get_supabase()
            resp = sb.table("user_dna").select("*").eq("user_id", req.user_id).single().execute()
            user_dna = resp.data
        except Exception:
            pass  # DNA optional — chat still works without it

    system = _build_system_prompt(user_dna)

    return StreamingResponse(
        _stream_response(req.messages, system),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/non-streaming")
async def chat_simple(req: ChatRequest, http_request: Request):
    """Non-streaming version — for integrations that don't support SSE."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="Няма съобщения")
    allowed, _ = _chat_rate_check(http_request)
    if not allowed:
        raise HTTPException(status_code=429, detail=_LIMIT_MSG)
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_dna = None
    if req.user_id:
        try:
            sb = get_supabase()
            resp = sb.table("user_dna").select("*").eq("user_id", req.user_id).single().execute()
            user_dna = resp.data
        except Exception:
            pass

    system = _build_system_prompt(user_dna)
    msg_dicts = _trim(req.messages)

    response = await client.messages.create(
        model=_LEGACY_MODEL,
        max_tokens=_LEGACY_MAX_TOKENS,
        system=system,
        messages=msg_dicts,
    )

    return {
        "response": response.content[0].text,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
