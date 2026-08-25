import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

RESEND_API_URL = "https://api.resend.com/emails"
WEBHOOK_TOLERANCE_SECONDS = 300


def _sender(settings) -> str:
    email = (settings.resend_from_email or "").strip()
    if not email:
        raise RuntimeError("RESEND_FROM_EMAIL is not configured")
    return f"Mellowden <{email}>"


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Resend connection error: {exc.reason}") from exc


def send_template(settings, to_email: str, template_id: str, variables: dict, idempotency_key: str | None = None) -> str:
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not to_email:
        raise RuntimeError("Recipient email is missing")

    headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    payload = {
        "from": _sender(settings),
        "to": [to_email],
        "reply_to": [settings.resend_reply_to_email or settings.resend_from_email],
        "template": {"id": template_id, "variables": variables},
    }
    result = _post_json(RESEND_API_URL, payload, headers)
    email_id = str(result.get("id") or "")
    if not email_id:
        raise RuntimeError(f"Resend did not return an email id: {result}")
    return email_id


def send_customer_review_email(settings, row, review_url: str) -> str:
    return send_template(
        settings,
        row.customer_email,
        settings.resend_review_template,
        {
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "PET_NAME": row.pet_name or "your pet",
            "PRODUCT_TITLE": row.product_title or "your Mellowden portrait",
            "REVIEW_URL": review_url,
        },
        idempotency_key=f"mellowden-review/{row.shopify_order_id}/{row.approval_token}",
    )


def send_owner_approval_notification(settings, row) -> str:
    return send_template(
        settings,
        settings.owner_notification_email,
        settings.resend_approved_template,
        {
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "CUSTOMER_EMAIL": row.customer_email or "—",
            "PET_NAME": row.pet_name or "—",
            "PRODUCT_TITLE": row.product_title or "Mellowden portrait",
            "ADMIN_URL": settings.admin_dashboard_url,
        },
        idempotency_key=f"mellowden-approved/{row.shopify_order_id}/{row.approval_token}",
    )


def send_owner_revision_notification(settings, row) -> str:
    return send_template(
        settings,
        settings.owner_notification_email,
        settings.resend_revision_template,
        {
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "CUSTOMER_EMAIL": row.customer_email or "—",
            "PET_NAME": row.pet_name or "—",
            "PRODUCT_TITLE": row.product_title or "Mellowden portrait",
            "REVISION_MESSAGE": row.revision_request or "No revision details provided.",
            "ADMIN_URL": settings.admin_dashboard_url,
        },
        idempotency_key=f"mellowden-revision/{row.shopify_order_id}/{row.approval_token}/{row.revision_count}",
    )


def verify_resend_webhook(raw_body: bytes, headers, webhook_secret: str) -> dict:
    if not webhook_secret:
        raise RuntimeError("RESEND_WEBHOOK_SECRET is not configured")

    msg_id = headers.get("svix-id")
    timestamp = headers.get("svix-timestamp")
    signature_header = headers.get("svix-signature")
    if not msg_id or not timestamp or not signature_header:
        raise ValueError("Missing Resend webhook signature headers")

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise ValueError("Invalid webhook timestamp") from exc

    if abs(int(time.time()) - timestamp_int) > WEBHOOK_TOLERANCE_SECONDS:
        raise ValueError("Webhook timestamp is outside the allowed tolerance")

    secret = webhook_secret
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_"):]
    try:
        key = base64.b64decode(secret)
    except Exception as exc:
        raise ValueError("Invalid webhook secret") from exc

    signed = msg_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    valid = False
    for token in signature_header.split():
        if token.startswith("v1,"):
            candidate = token.split(",", 1)[1]
            if hmac.compare_digest(candidate, expected):
                valid = True
                break
    if not valid:
        raise ValueError("Invalid webhook signature")

    return json.loads(raw_body.decode("utf-8"))
