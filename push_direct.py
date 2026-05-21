"""
Push scraped data to Supabase + write price history + send watchlist alerts.
Usage:
  python push_direct.py URL SERVICE_ROLE_KEY [SMTP_USER SMTP_PASS]
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

FILES = [
    ("emag_offers.json",        "emag"),
    ("technomarket_offers.json","technomarket"),
    ("zora_offers.json",        "zora"),
    ("technopolis_offers.json", "technopolis"),
    ("ardes_offers.json",       "ardes"),
]


def main():
    if len(sys.argv) < 3:
        print("Usage: python push_direct.py SUPABASE_URL SERVICE_ROLE_KEY [SMTP_USER SMTP_PASS]")
        sys.exit(1)

    url  = sys.argv[1].rstrip("/")
    key  = sys.argv[2]
    smtp_user = sys.argv[3] if len(sys.argv) > 3 else os.getenv("SMTP_USER", "")
    smtp_pass = sys.argv[4] if len(sys.argv) > 4 else os.getenv("SMTP_PASS", "")

    from supabase import create_client
    sb = create_client(url, key)

    root  = Path(__file__).parent
    total = 0
    now_ts = datetime.now(timezone.utc).isoformat()

    for fname, store in FILES:
        path = root / fname
        if not path.exists():
            logger.info("SKIP  %s", fname)
            continue

        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("Pushing %d from %s ...", len(data), fname)

        # 1. Append to price_history (never delete)
        history_rows = [
            {
                "store":        store,
                "product_url":  row.get("url", ""),
                "raw_name":     row.get("raw_name", ""),
                "category":     row.get("category"),
                "brand":        row.get("brand"),
                "price":        row.get("price"),
                "old_price":    row.get("old_price"),
                "discount_pct": int(row["discount_pct"]) if row.get("discount_pct") is not None else None,
                "image_url":    row.get("image_url"),
                "scraped_at":   now_ts,
            }
            for row in data
            if row.get("url") and row.get("price")
        ]
        for i in range(0, len(history_rows), 200):
            sb.table("price_history").insert(history_rows[i:i+200]).execute()

        # 2. Replace current offers
        sb.table("electronics_offers").delete().eq("store", store).execute()
        for i in range(0, len(data), 100):
            sb.table("electronics_offers").insert(data[i:i+100]).execute()

        total += len(data)
        logger.info("OK: %d products", len(data))

    logger.info("Done. Total: %d", total)

    # 3. Check watchlist alerts via API (preferred) or direct SMTP fallback
    _trigger_alerts(url, key, smtp_user, smtp_pass)


def _trigger_alerts(supabase_url: str, supabase_key: str, smtp_user: str, smtp_pass: str):
    """
    Trigger alert check via the API endpoint /api/alex/run-alerts.
    Falls back to direct SMTP if the API call fails.
    """
    import urllib.request
    import urllib.parse

    # Derive API base URL from Supabase URL pattern, or use env var
    api_base = os.getenv("API_BASE_URL", "")
    if not api_base:
        logger.info("[alerts] API_BASE_URL not set — falling back to direct check")
        _check_alerts_direct(supabase_url, supabase_key, smtp_user, smtp_pass)
        return

    secret = os.getenv("SECRET_KEY", "")
    endpoint = f"{api_base.rstrip('/')}/api/alex/run-alerts?secret={urllib.parse.quote(secret)}"
    try:
        req = urllib.request.Request(endpoint, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
            logger.info("[alerts] API result: sent=%s skipped=%s errors=%s",
                        body.get("sent"), body.get("skipped"), body.get("errors"))
    except Exception as exc:
        logger.warning("[alerts] API call failed (%s) — falling back to direct check", exc)
        _check_alerts_direct(supabase_url, supabase_key, smtp_user, smtp_pass)


def _check_alerts_direct(supabase_url: str, supabase_key: str, smtp_user: str, smtp_pass: str):
    """Direct SMTP fallback — used when API is not reachable."""
    from supabase import create_client
    sb = create_client(supabase_url, supabase_key)
    _check_alerts(sb, smtp_user, smtp_pass)


def _check_alerts(sb, smtp_user: str, smtp_pass: str):
    """Send email alerts for watchlist items that hit their target price."""
    if not smtp_user or not smtp_pass:
        logger.info("[alerts] No SMTP credentials — skipping alert check")
        return

    try:
        resp = sb.table("watchlists").select("*").execute()
        items = resp.data or []
    except Exception as exc:
        logger.error("[alerts] Failed to load watchlists: %s", exc)
        return

    if not items:
        return

    logger.info("[alerts] Checking %d watchlist items...", len(items))
    sent = 0

    for item in items:
        try:
            # Get current price
            cur = (
                sb.table("electronics_offers")
                .select("price, url, raw_name, store, image_url")
                .eq("url", item["product_url"])
                .limit(1)
                .execute()
            )
            if not cur.data:
                continue

            prod = cur.data[0]
            current_price = float(prod["price"])
            target_price  = float(item["target_price"])

            if current_price > target_price:
                # Price hasn't dropped enough — update last_known_price silently
                sb.table("watchlists").update({"last_known_price": current_price}).eq("id", item["id"]).execute()
                continue

            # Check cooldown: don't re-alert within 24h
            alerted_at = item.get("alerted_at")
            if alerted_at:
                from datetime import datetime, timezone, timedelta
                last = datetime.fromisoformat(alerted_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last < timedelta(hours=24):
                    continue

            # Send alert
            from api.email_utils import send_price_alert
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
            )
            if ok:
                sb.table("watchlists").update({
                    "alerted_at":       datetime.now(timezone.utc).isoformat(),
                    "last_known_price": current_price,
                }).eq("id", item["id"]).execute()
                sent += 1

        except Exception as exc:
            logger.error("[alerts] Error processing item %s: %s", item.get("id"), exc)

    logger.info("[alerts] Sent %d alert(s)", sent)


if __name__ == "__main__":
    main()
