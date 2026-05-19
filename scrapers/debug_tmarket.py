"""Диагностика 2 — показва структурата около h3 продуктите"""
import httpx
from bs4 import BeautifulSoup

URL = "https://tmarketonline.bg/selection/produkti-v-akciya"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "bg-BG,bg;q=0.9",
}

resp = httpx.get(URL, headers=HEADERS, follow_redirects=True, timeout=30)
soup = BeautifulSoup(resp.text, "html.parser")

# Покажи HTML на първия h3
h3_list = soup.select("h3")
print(f"Total h3: {len(h3_list)}")
print()

if h3_list:
    h3 = h3_list[0]
    print("=== Първи h3 ===")
    print(repr(str(h3)[:300]))
    print()

    # Вървим нагоре и печатаме всеки parent
    el = h3
    for i in range(6):
        el = el.parent
        if el is None:
            break
        print(f"Parent {i+1}: <{el.name} class='{el.get('class', '')}'>")
        # Намираме spans с цени
        spans = el.select("span")
        lv_spans = [s.get_text(strip=True) for s in spans if "лв" in s.get_text()]
        if lv_spans:
            print(f"  --> Намерени BGN spans: {lv_spans[:4]}")
            print(f"  --> Пълен HTML на контейнера:")
            print(str(el)[:800])
            break

print()
print("=== Spans с 'лв' в целия документ (първите 5) ===")
count = 0
for span in soup.select("span"):
    t = span.get_text(strip=True)
    if "лв" in t and any(c.isdigit() for c in t):
        print(f"  span class={span.get('class','')} → '{t}'")
        count += 1
        if count >= 5:
            break
