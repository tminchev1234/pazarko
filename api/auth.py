"""
Clerk session token — проверка на сървъра (JWKS, без secret key).

Frontend-ът праща `Authorization: Bearer <Clerk session token>`. Тук проверяваме
подписа (RS256) срещу публичните ключове на Clerk инстанцията + exp/iss.
Анонимните localStorage UUID-и нямат токен — те остават разрешени; защитени са
само Clerk самоличностите (`user_…`), за да не може някой да ги ползва от чуждо име.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from api.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache()
def _jwks_client() -> jwt.PyJWKClient:
    issuer = get_settings().clerk_issuer.rstrip("/")
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


def is_clerk_id(user_id: Optional[str]) -> bool:
    return bool(user_id) and user_id.startswith("user_")


def verified_clerk_id(request: Request) -> Optional[str]:
    """Clerk user id от валиден Bearer токен, иначе None. Никога не хвърля."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    s = get_settings()
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            issuer=s.clerk_issuer.rstrip("/"),
            options={"require": ["exp", "iss", "sub"]},
            leeway=30,
        )
    except Exception as exc:
        logger.info("[auth] invalid Clerk token: %s", exc)
        return None
    # azp = origin-ът, издал токена. Проверява се само ако е зададен списък.
    parties = [p.strip() for p in s.clerk_authorized_parties.split(",") if p.strip()]
    if parties and claims.get("azp") and claims["azp"] not in parties:
        logger.info("[auth] azp %s not allowed", claims.get("azp"))
        return None
    return claims.get("sub")


def require_owner(request: Request, user_id: str) -> None:
    """Clerk id изисква токен на СЪЩИЯ потребител; анонимен UUID минава както досега."""
    if is_clerk_id(user_id) and verified_clerk_id(request) != user_id:
        raise HTTPException(status_code=401, detail="Нужен е вход")
