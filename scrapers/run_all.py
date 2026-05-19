"""
run_all.py — пуска всички скрейпъри и записва в Supabase

Употреба:
    py -m scrapers.run_all                  # всички магазини
    py -m scrapers.run_all --kaufland       # само Kaufland (Selenium)
    py -m scrapers.run_all --naoferta       # само naoferta API (Billa и др.)
    py -m scrapers.run_all --tmarket        # само T-Market
    py -m scrapers.run_all --lidl           # само Lidl (Selenium)
    py -m scrapers.run_all --fantastico     # само Fantastico (Selenium)
"""

from __future__ import annotations
import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

def run_kaufland():
    logger.info("=== Kaufland (Selenium) ===")
    try:
        from scrapers.kaufland_selenium import scrape_kaufland, save_to_supabase
        products = scrape_kaufland(headless=True)
        if products:
            saved = save_to_supabase(products)
            logger.info("Kaufland: %d products saved", saved)
        else:
            logger.warning("Kaufland: 0 products scraped")
    except Exception as e:
        logger.error("Kaufland scrape failed: %s", e)


def run_naoferta():
    """Billa + всички магазини от api.naoferta.net"""
    logger.info("=== naoferta.net API (Billa и др.) ===")
    try:
        from scrapers.naoferta import scrape_naoferta, save_to_supabase
        products = scrape_naoferta(offers_only=True)
        if products:
            saved = save_to_supabase(products)
            logger.info("naoferta: %d products saved", saved)
        else:
            logger.warning("naoferta: 0 products")
    except Exception as e:
        logger.error("naoferta failed: %s", e)


def run_tmarket():
    logger.info("=== T-Market Online ===")
    try:
        from scrapers.tmarket import scrape_tmarket, save_to_supabase
        products = scrape_tmarket()
        if products:
            saved = save_to_supabase(products)
            logger.info("T-Market: %d products saved", saved)
        else:
            logger.warning("T-Market: 0 products")
    except Exception as e:
        logger.error("T-Market failed: %s", e)


def run_lidl():
    logger.info("=== Lidl (Selenium) ===")
    try:
        from scrapers.lidl_selenium import scrape_lidl, save_to_supabase
        products = scrape_lidl(headless=True)
        if products:
            saved = save_to_supabase(products)
            logger.info("Lidl: %d products saved", saved)
        else:
            logger.warning("Lidl: 0 products scraped")
    except Exception as e:
        logger.error("Lidl scrape failed: %s", e)


def run_fantastico():
    logger.info("=== Fantastico (Selenium) ===")
    try:
        from scrapers.fantastico_selenium import scrape_fantastico, save_to_supabase
        products = scrape_fantastico(headless=True)
        if products:
            saved = save_to_supabase(products)
            logger.info("Fantastico: %d products saved", saved)
        else:
            logger.warning("Fantastico: 0 products scraped")
    except Exception as e:
        logger.error("Fantastico scrape failed: %s", e)


if __name__ == "__main__":
    args = sys.argv[1:]

    # Ако не са дадени аргументи — пускаме всичко
    run_kauf    = "--kaufland"   in args or not args
    run_naof    = "--naoferta"   in args or not args
    run_tmark   = "--tmarket"    in args or not args
    run_lid     = "--lidl"       in args or not args
    run_fant    = "--fantastico" in args or not args

    print(f"\n{'='*50}")
    print(f"  Pazarko — Scrape All Stores")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")

    if run_kauf:  run_kaufland()
    if run_naof:  run_naoferta()
    if run_tmark: run_tmarket()
    if run_lid:   run_lidl()
    if run_fant:  run_fantastico()

    print("\n✅ Done!")
