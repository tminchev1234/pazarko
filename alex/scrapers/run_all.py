"""
Alex — Run all electronics scrapers

Употреба:
  py -m alex.scrapers.run_all                      # всички магазини → JSON + Supabase
  py -m alex.scrapers.run_all --test               # 1 категория на магазин
  py -m alex.scrapers.run_all --json-only          # само JSON, без Supabase
  py -m alex.scrapers.run_all --store emag         # само един магазин
  py -m alex.scrapers.run_all --store technopolis
  py -m alex.scrapers.run_all --store ardes
  py -m alex.scrapers.run_all --store technomarket
"""

from __future__ import annotations
import sys
import logging
import json
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _run_emag(test: bool) -> list[dict]:
    from alex.scrapers.emag import scrape_emag
    return scrape_emag(max_categories=1 if test else 99, max_pages=2 if test else 5)


def _run_technopolis(test: bool) -> list[dict]:
    from alex.scrapers.technopolis import scrape_technopolis
    return scrape_technopolis(max_categories=1 if test else 99, max_pages=2 if test else 5)


def _run_ardes(test: bool) -> list[dict]:
    from alex.scrapers.ardes import scrape_ardes
    return scrape_ardes(max_categories=1 if test else 99, max_pages=2 if test else 5)


def _run_technomarket(test: bool) -> list[dict]:
    from alex.scrapers.technomarket import scrape_technomarket
    return scrape_technomarket(max_categories=1 if test else 99, max_pages=2 if test else 7)


def _run_zora(test: bool) -> list[dict]:
    from alex.scrapers.zora import scrape_zora
    return scrape_zora(max_categories=1 if test else 99, max_pages=2 if test else 12)


SCRAPERS = {
    "emag":         _run_emag,
    "technopolis":  _run_technopolis,
    "ardes":        _run_ardes,
    "technomarket": _run_technomarket,
    "zora":         _run_zora,
}


def save_to_supabase(offers: list[dict], store: str) -> int:
    from api.db import get_supabase_admin
    if not offers:
        return 0
    sb = get_supabase_admin()
    sb.table("electronics_offers").delete().eq("store", store).execute()
    total = 0
    for i in range(0, len(offers), 100):
        sb.table("electronics_offers").insert(offers[i:i + 100]).execute()
        total += len(offers[i:i + 100])
    return total


def save_to_json(offers: list[dict], filename: str):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)
    print(f"  Saved {len(offers)} → {filename}")


def main():
    test      = "--test"      in sys.argv
    json_only = "--json-only" in sys.argv
    store_arg = None
    if "--store" in sys.argv:
        idx = sys.argv.index("--store")
        store_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    stores_to_run = [store_arg] if store_arg else list(SCRAPERS.keys())

    print(f"\nAlex scraper - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Stores: {', '.join(stores_to_run)}  |  test={test}  |  supabase={not json_only}\n")

    all_results: dict[str, list[dict]] = {}

    for store in stores_to_run:
        if store not in SCRAPERS:
            print(f"❌ Unknown store: {store}")
            continue

        print(f"-- {store.upper()} --")
        try:
            offers = SCRAPERS[store](test)
            all_results[store] = offers
            print(f"  OK: {len(offers)} offers")

            if offers:
                save_to_json(offers, f"{store}_offers.json")

                if not json_only:
                    saved = save_to_supabase(offers, store)
                    print(f"  Saved {saved} to Supabase")

                print(f"  Sample:")
                for o in offers[:3]:
                    disc = f" [-{o['discount_pct']}%]" if o.get("discount_pct") else ""
                    name = o['raw_name'][:55].encode('ascii', errors='replace').decode('ascii')
                    print(f"    {name:<55} {o['price']:.2f} EUR{disc} [{o['store']}]")
        except Exception as exc:
            logger.error("Scraper %s failed: %s", store, exc)
            all_results[store] = []

    # Summary
    total = sum(len(v) for v in all_results.values())
    print(f"\n{'='*50}")
    print(f"TOTAL: {total} offers across {len(all_results)} stores")
    for store, offers in all_results.items():
        print(f"  {store}: {len(offers)}")


if __name__ == "__main__":
    main()
