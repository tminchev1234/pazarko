"""
Supabase client — само сървърът говори с базата:
  get_supabase()         → service_role key, ако е зададен (RLS е включен на всички
                           таблици на Пазарко → anon ключът не вижда нищо, виж migrations/008)
  get_supabase_admin()   → същото; оставено за съвместимост със скрейпърите
"""

from functools import lru_cache
from supabase import create_client, Client
from api.config import get_settings


@lru_cache()
def get_supabase() -> Client:
    """Server client. Prefers SUPABASE_SERVICE_KEY; falls back to SUPABASE_KEY."""
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key or s.supabase_key)


@lru_cache()
def get_supabase_admin() -> Client:
    """
    Service-role client — bypasses RLS, for scraper writes.
    Falls back to anon key if SUPABASE_SERVICE_KEY is not set.
    """
    s = get_settings()
    key = s.supabase_service_key or s.supabase_key
    return create_client(s.supabase_url, key)
