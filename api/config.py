"""
Central config — reads from .env
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Force-load .env from the project root, overriding any empty system env vars
_env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_env_path, override=True)


class Settings(BaseSettings):
    # Supabase
    supabase_url:         str = os.getenv("SUPABASE_URL", "")
    supabase_key:         str = os.getenv("SUPABASE_KEY", "")
    supabase_service_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")

    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # App
    environment: str = os.getenv("ENVIRONMENT", "development")
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # Email (Gmail SMTP)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    alert_from_name: str = os.getenv("ALERT_FROM_NAME", "Alex — AI Съветник")

    # Scraping
    scrape_timeout_seconds: int = 30
    scrape_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
