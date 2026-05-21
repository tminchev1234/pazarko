"""
Email alert sender — Gmail SMTP (stdlib only, no extra deps).
Switch to Resend later by replacing send_alert() body.
"""
from __future__ import annotations
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_price_alert(
    *,
    to_email: str,
    product_name: str,
    current_price: float,
    target_price: float,
    store: str,
    product_url: str,
    image_url: str | None,
    smtp_user: str,
    smtp_pass: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    from_name: str = "Alex — AI Съветник",
) -> bool:
    """Send a price-drop alert. Returns True on success."""
    if not smtp_user or not smtp_pass:
        logger.warning("[email] SMTP credentials not configured — skipping alert")
        return False

    subject = f"💰 Alex: {product_name[:50]} вече е €{current_price:.0f}"

    img_html = (
        f'<img src="{image_url}" alt="" style="max-width:160px;border-radius:8px;margin-bottom:12px">'
        if image_url else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="bg">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">
    <div style="background:#1a1a2e;padding:20px 24px;color:#fff">
      <span style="font-size:22px;font-weight:700">Alex</span>
      <span style="font-size:13px;opacity:.7;margin-left:8px">AI Съветник за електроника</span>
    </div>
    <div style="padding:24px">
      {img_html}
      <h2 style="margin:0 0 8px;font-size:16px;color:#1a1a2e">{product_name}</h2>
      <p style="margin:0 0 16px;color:#555;font-size:14px">
        Проследяваният от теб продукт падна под целевата цена!
      </p>
      <div style="display:flex;gap:16px;margin-bottom:20px">
        <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 20px;text-align:center">
          <div style="font-size:11px;color:#16a34a;text-transform:uppercase;font-weight:600">Текуща цена</div>
          <div style="font-size:28px;font-weight:700;color:#16a34a">€{current_price:.0f}</div>
        </div>
        <div style="background:#fafafa;border:1px solid #e5e7eb;border-radius:8px;padding:12px 20px;text-align:center">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;font-weight:600">Твоята цел</div>
          <div style="font-size:28px;font-weight:700;color:#374151">€{target_price:.0f}</div>
        </div>
      </div>
      <p style="margin:0 0 20px;color:#555;font-size:14px">
        Магазин: <strong>{store}</strong>
      </p>
      <a href="{product_url}" style="display:inline-block;background:#6366f1;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600;font-size:15px">
        Виж в магазина →
      </a>
    </div>
    <div style="padding:16px 24px;border-top:1px solid #f0f0f0;font-size:12px;color:#9ca3af">
      Получаваш това съобщение, защото следиш цени в Alex.<br>
      Управлявай известията си на сайта.
    </div>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{smtp_user}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.sendmail(smtp_user, [to_email], msg.as_bytes())
        logger.info("[email] alert sent to %s for %s", to_email, product_name[:40])
        return True
    except Exception as exc:
        logger.error("[email] failed to send alert to %s: %s", to_email, exc)
        return False
