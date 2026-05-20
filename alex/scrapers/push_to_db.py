"""
Direct PostgreSQL push — bypasses PostgREST/PGRST entirely.

Usage:
  py -m alex.scrapers.push_to_db                        # push emag_offers.json
  py -m alex.scrapers.push_to_db --file technopolis_offers.json
  py -m alex.scrapers.push_to_db --store emag            # delete+reinsert only emag rows

Connection string is read from SUPABASE_DB_URL env var, or you will be prompted.
Format:  postgresql://postgres:<PASSWORD>@db.<PROJECT_REF>.supabase.co:5432/postgres
"""

from __future__ import annotations
import json
import os
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.electronics_offers (
    id              BIGSERIAL PRIMARY KEY,
    store           TEXT        NOT NULL,
    raw_name        TEXT        NOT NULL,
    brand           TEXT,
    category        TEXT,
    category_raw    TEXT,
    price           NUMERIC(10,2) NOT NULL,
    old_price       NUMERIC(10,2),
    discount_pct    NUMERIC(5,1),
    image_url       TEXT,
    url             TEXT,
    in_stock        BOOLEAN     DEFAULT TRUE,
    scraped_at      TIMESTAMPTZ
);
"""

INSERT_SQL = """
INSERT INTO public.electronics_offers
    (store, raw_name, brand, category, category_raw,
     price, old_price, discount_pct, image_url, url, in_stock, scraped_at)
VALUES
    (%(store)s, %(raw_name)s, %(brand)s, %(category)s, %(category_raw)s,
     %(price)s, %(old_price)s, %(discount_pct)s, %(image_url)s, %(url)s,
     %(in_stock)s, %(scraped_at)s)
"""


def _get_conn_string() -> str:
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if url:
        return url
    print()
    print("Paste your Supabase direct DB connection string:")
    print("  (Supabase Dashboard → Settings → Database → Connection string → URI tab)")
    print("  Format: postgresql://postgres:<PASSWORD>@db.<REF>.supabase.co:5432/postgres")
    print()
    url = input("Connection string: ").strip()
    if not url:
        print("No connection string provided — exiting.")
        sys.exit(1)
    return url


def push(offers: list[dict], conn_string: str, store: str | None = None) -> int:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("\npsycopg2 not installed. Run:  py -m pip install psycopg2-binary\n")
        sys.exit(1)

    conn = psycopg2.connect(conn_string)
    conn.autocommit = False
    cur = conn.cursor()

    # Ensure table exists
    cur.execute(CREATE_TABLE_SQL)

    # Delete existing rows for this store
    target_store = store or (offers[0]["store"] if offers else None)
    if target_store:
        cur.execute("DELETE FROM public.electronics_offers WHERE store = %s", (target_store,))
        logger.info("Deleted existing rows for store=%s", target_store)

    # Batch insert
    batch_size = 100
    total = 0
    for i in range(0, len(offers), batch_size):
        batch = offers[i : i + batch_size]
        psycopg2.extras.execute_batch(cur, INSERT_SQL, batch, page_size=batch_size)
        total += len(batch)
        logger.info("Inserted %d / %d", total, len(offers))

    conn.commit()
    cur.close()
    conn.close()
    return total


def main():
    # ── parse args ──────────────────────────────────────────────────
    args = sys.argv[1:]
    json_file = "emag_offers.json"
    store_filter = None

    for i, arg in enumerate(args):
        if arg == "--file" and i + 1 < len(args):
            json_file = args[i + 1]
        if arg == "--store" and i + 1 < len(args):
            store_filter = args[i + 1]

    json_path = Path(json_file)
    if not json_path.exists():
        # try relative to project root
        json_path = Path(__file__).resolve().parents[2] / json_file
    if not json_path.exists():
        print(f"File not found: {json_file}")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        offers = json.load(f)

    print(f"Loaded {len(offers)} offers from {json_path}")

    conn_string = _get_conn_string()
    saved = push(offers, conn_string, store=store_filter)
    print(f"\n✅ Inserted {saved} rows into electronics_offers")


if __name__ == "__main__":
    main()
