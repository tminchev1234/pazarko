"""
Weekly scraping scheduler
Runs every Sunday at 03:00 BG time — scrapes all stores and upserts to Supabase
"""

from __future__ import annotations
import logging
import json
from datetime import datetime, timezone
from typing import List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic

from scrapers.kaufland import KauflandScraper
from scrapers.billa import BillaScraper
from scrapers.fantastico import FantasticoScraper
from scrapers.base import RawProduct
from engines.matcher import ProductMatcher
from api.db import get_supabase
from api.config import get_settings

logger = logging.getLogger(__name__)


# ── main scrape + upsert ──────────────────────────────────────────────────────

def run_full_scrape():
    """
    1. Scrape all stores
    2. Match each product to canonical via ProductMatcher
    3. Upsert prices into Supabase
    """
    logger.info("=== Starting weekly scrape at %s ===", datetime.now(timezone.utc).isoformat())
    started = datetime.now(timezone.utc)

    all_products: List[RawProduct] = []

    scrapers = [KauflandScraper(), BillaScraper(), FantasticoScraper()]
    for scraper in scrapers:
        try:
            with scraper:
                products = scraper.scrape_all()
                all_products.extend(products)
                logger.info("[%s] Scraped %d products", scraper.store_id, len(products))
        except Exception as exc:
            logger.error("[%s] Scrape failed: %s", scraper.store_id, exc, exc_info=True)

    logger.info("Total products scraped: %d", len(all_products))

    if not all_products:
        logger.warning("Nothing scraped — aborting upsert")
        return

    # Match & upsert
    settings = get_settings()
    matcher = ProductMatcher(anthropic_api_key=settings.anthropic_api_key)
    sb = get_supabase()

    upserted = 0
    errors = 0

    for raw in all_products:
        try:
            # 1. Find or create canonical product
            product_id = matcher.get_or_create_product(raw, sb)
            if not product_id:
                continue

            # 2. Insert price record
            sb.table("price_history").insert({
                "product_id": product_id,
                "store": raw.store,
                "price": raw.price,
                "unit": raw.unit,
                "url": raw.url,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            # 3. Upsert latest_prices (store current best)
            sb.table("prices").upsert({
                "product_id": product_id,
                "store": raw.store,
                "price": raw.price,
                "unit": raw.unit,
                "url": raw.url,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="product_id,store").execute()

            upserted += 1

        except Exception as exc:
            logger.debug("Upsert failed for %r: %s", raw.raw_name, exc)
            errors += 1

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info(
        "=== Scrape complete: %d upserted, %d errors, %.0f seconds ===",
        upserted, errors, duration
    )


# ── manual trigger ────────────────────────────────────────────────────────────

def run_store(store_name: str):
    """Run a single store scrape — useful for testing."""
    mapping = {
        "kaufland":   KauflandScraper,
        "billa":      BillaScraper,
        "fantastico": FantasticoScraper,
    }
    cls = mapping.get(store_name.lower())
    if not cls:
        print(f"Unknown store: {store_name}. Choose from: {list(mapping.keys())}")
        return

    with cls() as scraper:
        products = scraper.scrape_all()
        print(f"\n{store_name}: {len(products)} products scraped")
        for p in products[:10]:
            print(f"  {p.raw_name!r:55s}  {p.price:.2f} лв.")

        # Save sample to JSON
        fname = f"{store_name}_sample.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(
                [vars(p) for p in products[:50]],
                f, ensure_ascii=False, indent=2
            )
        print(f"\nTop 50 saved to {fname}")


# ── scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BlockingScheduler(timezone="Europe/Sofia")

    # Weekly: Sunday 03:00
    scheduler.add_job(
        run_full_scrape,
        CronTrigger(day_of_week="sun", hour=3, minute=0),
        id="weekly_scrape",
        name="Weekly full scrape",
        misfire_grace_time=3600,
    )

    logger.info("Scheduler started. Next run: Sunday 03:00 Sofia time.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "now":
            run_full_scrape()
        elif cmd in ("kaufland", "billa", "fantastico"):
            run_store(cmd)
        else:
            print("Usage: python -m scrapers.scheduler [now|kaufland|billa|fantastico]")
    else:
        start_scheduler()
