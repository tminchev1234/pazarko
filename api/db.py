"""
Supabase client — two clients:
  get_supabase()         → anon key  (public reads, used by API routes)
  get_supabase_admin()   → service_role key (writes by scrapers, bypasses RLS)
"""

from functools import lru_cache
from supabase import create_client, Client
from api.config import get_settings


@lru_cache()
def get_supabase() -> Client:
    """Anon/public client — for API reads."""
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)


@lru_cache()
def get_supabase_admin() -> Client:
    """
    Service-role client — bypasses RLS, for scraper writes.
    Falls back to anon key if SUPABASE_SERVICE_KEY is not set.
    """
    s = get_settings()
    key = s.supabase_service_key or s.supabase_key
    return create_client(s.supabase_url, key)
