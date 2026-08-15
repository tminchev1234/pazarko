// Service worker: тегли ценовата история от Pazarko.
// host_permissions за pazarko-1.onrender.com → fetch-ът тук не е обект на CORS.
const API = "https://pazarko-1.onrender.com/api/alex/price-history";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "PAZARKO_PRICE" && msg.url) {
    fetch(API + "?url=" + encodeURIComponent(msg.url), { credentials: "omit" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => sendResponse({ ok: true, data }))
      .catch(() => sendResponse({ ok: false }));
    return true; // async response
  }
});
