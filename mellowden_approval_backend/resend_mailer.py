import json
from html import escape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _send(settings, *, to_email: str, subject: str, html: str, text: str | None = None):
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not settings.resend_from_email:
        raise RuntimeError("RESEND_FROM_EMAIL is not configured")
    if not to_email:
        raise RuntimeError("Recipient email is missing")

    payload = {
        "from": f"Mellowden <{settings.resend_from_email}>",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if settings.owner_notification_email:
        payload["reply_to"] = settings.owner_notification_email

    request = Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mellowden-approval-backend/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend rejected the email ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Resend: {exc.reason}") from exc


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<body style="margin:0;background:#f5efe7;font-family:Arial,sans-serif;color:#2f2925">
  <div style="max-width:640px;margin:0 auto;padding:38px 18px">
    <div style="font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:#75675d;font-weight:700;margin-bottom:14px">MELLOWDEN</div>
    <div style="background:#fffaf4;border:1px solid #e6d9cc;border-radius:20px;padding:30px">
      <h1 style="font-family:Georgia,serif;font-size:36px;line-height:1.1;margin:0 0 18px">{escape(title)}</h1>
      {body}
    </div>
    <p style="font-size:12px;color:#8a7d73;text-align:center;margin:18px 0 0">Made for the ones who make home warmer.</p>
  </div>
</body>
</html>"""


def send_customer_review_email(settings, row, review_url: str):
    order_name = row.order_name or "your order"
    pet_name = row.pet_name or "your pet"
    safe_url = escape(review_url, quote=True)
    body = f"""
      <p style="font-size:16px;line-height:1.65;color:#75675d">Your personalized portrait of <strong style="color:#2f2925">{escape(pet_name)}</strong> is ready for you to review.</p>
      <p style="font-size:16px;line-height:1.65;color:#75675d">Please check the artwork carefully. You can approve it for printing or request a revision from the private review page.</p>
      <p style="margin:26px 0"><a href="{safe_url}" style="display:inline-block;background:#2f2925;color:#fff;text-decoration:none;padding:14px 22px;border-radius:999px;font-weight:700">Review your portrait</a></p>
      <p style="font-size:14px;line-height:1.6;color:#75675d">Nothing is sent to print until you approve the artwork.</p>
      <p style="font-size:13px;color:#8a7d73;margin-top:22px">Order {escape(order_name)}</p>
    """
    text = (
        f"Your Mellowden portrait is ready to review.\n\n"
        f"Review your artwork: {review_url}\n\n"
        "You can approve it for printing or request changes there. "
        "Nothing is sent to print until you approve the artwork."
    )
    return _send(
        settings,
        to_email=row.customer_email,
        subject=f"Your Mellowden portrait is ready to review — {order_name}",
        html=_shell("Your portrait is ready", body),
        text=text,
    )


def send_owner_approved_email(settings, row):
    if not settings.owner_notification_email:
        raise RuntimeError("OWNER_NOTIFICATION_EMAIL is not configured")
    order_name = row.order_name or row.shopify_order_id
    body = f"""
      <p style="font-size:16px;line-height:1.65;color:#75675d">The customer approved their portrait for printing.</p>
      <p style="font-size:16px;line-height:1.65"><strong>Order:</strong> {escape(order_name)}<br><strong>Customer:</strong> {escape(row.customer_email or '—')}<br><strong>Pet:</strong> {escape(row.pet_name or '—')}</p>
      <p style="font-size:14px;color:#75675d">The dashboard status is now APPROVED.</p>
    """
    return _send(
        settings,
        to_email=settings.owner_notification_email,
        subject=f"Approved: Mellowden order {order_name}",
        html=_shell("Portrait approved", body),
        text=f"Mellowden order {order_name} was approved by {row.customer_email or 'the customer'}. The dashboard status is now APPROVED.",
    )


def send_owner_revision_email(settings, row, message: str):
    if not settings.owner_notification_email:
        raise RuntimeError("OWNER_NOTIFICATION_EMAIL is not configured")
    order_name = row.order_name or row.shopify_order_id
    body = f"""
      <p style="font-size:16px;line-height:1.65;color:#75675d">The customer requested changes to their portrait.</p>
      <p style="font-size:16px;line-height:1.65"><strong>Order:</strong> {escape(order_name)}<br><strong>Customer:</strong> {escape(row.customer_email or '—')}<br><strong>Pet:</strong> {escape(row.pet_name or '—')}</p>
      <div style="margin-top:20px;background:#f3e9e0;border-radius:14px;padding:16px;line-height:1.6"><strong>Requested changes</strong><br>{escape(message)}</div>
      <p style="font-size:14px;color:#75675d">Open the private dashboard to upload the revised artwork. Uploading a revision will create a fresh review link and send it to the customer again.</p>
    """
    return _send(
        settings,
        to_email=settings.owner_notification_email,
        subject=f"Revision requested: Mellowden order {order_name}",
        html=_shell("Revision requested", body),
        text=f"Mellowden order {order_name} requested changes:\n\n{message}\n\nOpen the dashboard to upload a revised artwork.",
    )
