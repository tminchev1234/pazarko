// Pazarko content script — показва реалната ценова история + честен verdict
// директно върху страницата на продукта. Изолиран в Shadow DOM (не се чупи от CSS-а на магазина).
(function () {
  if (window.__pazarkoInjected) return;
  window.__pazarkoInjected = true;

  chrome.runtime.sendMessage({ type: "PAZARKO_PRICE", url: location.href }, (resp) => {
    if (chrome.runtime.lastError) return;
    if (!resp || !resp.ok || !resp.data) return;
    const d = resp.data;
    if (!d.history || d.history.length < 2) return; // нямаме достатъчно история за този продукт
    render(d);
  });

  const BADGE = {
    lowest:     ["🏆 Най-ниска цена, откакто следим", "g"],
    good:       ["🟢 Близо до най-ниската", "g"],
    real:       ["✅ Реална отстъпка", "g"],
    suspicious: ["⚠️ Фиктивно „намаление“ — това е обичайната цена", "r"],
    high:       ["🔴 Близо до най-високата — по-добре изчакай", "r"],
    typical:    ["≈ Обичайна цена", "n"],
  };

  function fmt(n) {
    return (Math.round(n * 100) / 100).toLocaleString("bg-BG") + " лв";
  }

  function sparkline(history) {
    const pr = history.map((h) => h.price).filter((x) => x != null);
    if (pr.length < 2) return "";
    const min = Math.min(...pr), max = Math.max(...pr), rng = (max - min) || 1;
    const W = 244, H = 46, pad = 4;
    const pts = pr.map((p, i) => {
      const x = pad + (i / (pr.length - 1)) * (W - 2 * pad);
      const y = pad + (1 - (p - min) / rng) * (H - 2 * pad);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const lastX = W, lastY = (pad + (1 - (pr[pr.length - 1] - min) / rng) * (H - 2 * pad)).toFixed(1);
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="#8f88ff" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="${(pad + (W - 2 * pad)).toFixed(1)}" cy="${lastY}" r="2.6" fill="#8f88ff"/>
    </svg>`;
  }

  function render(d) {
    const host = document.createElement("div");
    host.id = "pazarko-widget-host";
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;right:18px;bottom:18px";
    (document.body || document.documentElement).appendChild(host);
    const sh = host.attachShadow({ mode: "open" });

    const badge = BADGE[d.deal_score];
    const cheaper = (d.price_30d_ago != null && d.current != null && d.price_30d_ago < d.current)
      ? `<div class="row">📉 Преди ~месец: <b>${fmt(d.price_30d_ago)}</b> · сега ${fmt(d.current)}</div>` : "";
    const lowline = (d.low != null)
      ? `<div class="row">Най-ниско: <b>${fmt(d.low)}</b>${d.lowest_date ? ` (${d.lowest_date})` : ""}</div>` : "";

    sh.innerHTML = `
      <style>
        .card{width:280px;font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
          background:#15172a;color:#edeef8;border:1px solid #262842;border-radius:14px;
          box-shadow:0 12px 40px rgba(0,0,0,.45);padding:14px 15px;box-sizing:border-box}
        .hd{display:flex;align-items:center;gap:8px;margin-bottom:10px}
        .logo{font-weight:800;font-size:15px}
        .sub{font-size:11px;color:#8f88ff;font-weight:600}
        .x{margin-left:auto;cursor:pointer;color:#71739a;font-size:15px;line-height:1}
        .x:hover{color:#edeef8}
        .badge{font-size:12.5px;font-weight:700;padding:7px 10px;border-radius:9px;margin-bottom:9px;line-height:1.3}
        .badge.g{background:#123528;color:#37d39a}
        .badge.r{background:#3a1520;color:#ff8095}
        .badge.n{background:#22243c;color:#a3a5c6}
        .row{font-size:12.5px;color:#a3a5c6;margin:3px 0}
        .row b{color:#edeef8}
        .spark{margin:8px 0 4px}
        .ft{font-size:10.5px;color:#71739a;border-top:1px solid #262842;padding-top:8px;margin-top:6px;display:flex;justify-content:space-between}
        .ft a{color:#8f88ff;text-decoration:none}
      </style>
      <div class="card">
        <div class="hd"><span class="logo">Pazarko</span><span class="sub">реалната цена</span><span class="x" id="x">✕</span></div>
        ${badge ? `<div class="badge ${badge[1]}">${badge[0]}</div>` : ""}
        ${cheaper}${lowline}
        <div class="spark">${sparkline(d.history)}</div>
        <div class="ft"><span>Проследено ${d.days_tracked || d.history.length} дни</span><a href="https://pazarko-1.onrender.com/alex/" target="_blank" rel="noopener">Питай Alex →</a></div>
      </div>`;
    sh.getElementById("x").addEventListener("click", () => host.remove());
  }
})();
