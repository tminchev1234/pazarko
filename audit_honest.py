"""Одит на честността на витрините: флагва продукт, който се рекламира като сделка,
но живият verdict на картата (_price_analysis) го нарича фиктивно намаление.
Еднократен/при нужда: python audit_honest.py"""
import asyncio
import api.routes.alex as A
from api.routes.alex import (get_supabase, category_top_deal, _pick_day_offer,
                             _live_deal_score)

CATS = ["phones", "laptops", "tvs", "tablets", "headphones", "gaming",
        "cameras", "fridges", "washing", "ac", "vacuum"]


def main():
    sb = get_supabase()
    A._TOP_DEAL_CACHE.clear()
    A._CAT_RANK_CACHE.clear()
    A._REAL_DEALS_CACHE["data"] = None
    issues = []

    print("=" * 64)
    print("ОДИТ: рекламира ли се нещо, което картата нарича фиктивно?")
    print("=" * 64)

    # 1) Оферта на деня (героят на началната) — НИКОГА фиктивно
    print("\n— Оферта на деня —")
    dp = _pick_day_offer(sb, set())
    if not dp:
        print("  (няма избор днес)")
    else:
        v = _live_deal_score(sb, dp.get("url", ""))
        ok = v != "suspicious"
        print(f"  {dp['raw_name'][:46]} · verdict={v} · {'OK' if ok else 'FAIL ❌'}")
        if not ok:
            issues.append("Оферта на деня = фиктивно намаление")

    # 2) Топ оферта в категорията — бейдж за отстъпка vs жив verdict
    print("\n— Топ оферта в категорията —")
    for c in CATS:
        try:
            d = asyncio.run(category_top_deal(category=c)).get("deal")
        except Exception as e:
            print(f"  [{c}] грешка: {str(e)[:40]}")
            continue
        if not d:
            print(f"  [{c}] (няма)")
            continue
        v = _live_deal_score(sb, d.get("url", ""))
        bad = d["badge"] in ("Намалена цена", "Топ намаление") and v == "suspicious"
        print(f"  [{c:10}] {d['raw_name'][:34]:34} · бейдж={d['badge']:16} · verdict={v:10} · {'FAIL ❌' if bad else 'OK'}")
        if bad:
            issues.append(f"Топ оферта [{c}] рекламира фиктивно")

    # 3) Разпространение: колко от обявените намаления са фиктивни (контекст)
    print("\n— Обявени намаления: колко са фиктивни (по наши данни) —")
    for c in CATS:
        try:
            rows = (sb.table("electronics_offers")
                    .select("url, old_price, price").eq("category", c)
                    .not_.is_("old_price", "null").limit(25).execute().data or [])
        except Exception:
            rows = []
        claimed = [r for r in rows if (r.get("old_price") or 0) > (r.get("price") or 0) > 0]
        fake = sum(1 for r in claimed if _live_deal_score(sb, r["url"]) == "suspicious")
        if claimed:
            pct = round(100 * fake / len(claimed))
            print(f"  [{c:10}] обявени: {len(claimed):2} · фиктивни: {fake:2} ({pct}%)")

    print("\n" + "=" * 64)
    if issues:
        print(f"РЕЗУЛТАТ: ❌ {len(issues)} проблема(и):")
        for i in issues:
            print("  -", i)
    else:
        print("РЕЗУЛТАТ: ✅ Нито една витрина не рекламира фиктивно намаление.")
    print("=" * 64)


if __name__ == "__main__":
    main()
