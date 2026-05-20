"""
Push all local JSON offer files to Supabase electronics_offers table.
Run once to seed production data:
  cd C:/Users/Lenovo/Desktop/pazarko
  python push_to_supabase.py
"""
import json
import sys
from pathlib import Path

FILES = [
    ("emag_offers.json",        "emag"),
    ("technomarket_offers.json","technomarket"),
    ("zora_offers.json",        "zora"),
    ("technopolis_offers.json", "technopolis"),
    ("ardes_offers.json",       "ardes"),
]

ROOT = Path(__file__).parent

def main():
    from api.db import get_supabase_admin
    sb = get_supabase_admin()

    total = 0
    for fname, store in FILES:
        path = ROOT / fname
        if not path.exists():
            print(f"  SKIP  {fname} (not found)")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data:
            print(f"  SKIP  {fname} (empty)")
            continue

        print(f"  Pushing {len(data):>5} offers from {fname} ...", end=" ", flush=True)
        # Clear existing store data
        sb.table("electronics_offers").delete().eq("store", store).execute()
        # Insert in batches of 100
        for i in range(0, len(data), 100):
            sb.table("electronics_offers").insert(data[i:i+100]).execute()
        total += len(data)
        print("OK")

    print(f"\nDone. Total pushed: {total}")

if __name__ == "__main__":
    main()
