import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESEND_ENDPOINT = "https://api.resend.com/emails"
REVIEW_TEMPLATE = "mellowden-portrait-ready"
APPROVED_TEMPLATE = "mellowden-portrait-approved"
REVISION_TEMPLATE = "mellowden-revision-requested"
WEBHOOK_TOLERANCE_SECONDS = 300


def _send_template(settings, *, to_email: str, template_id: str, variables: dict, idempotency_key: str, tags: dict):
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    if not settings.resend_from_email:
        raise RuntimeError("RESEND_FROM_EMAIL is not configured")
    if not to_email:
        raise RuntimeError("Recipient email is missing")

    payload = {
        "from": f"Mellowden <{settings.resend_from_email}>",
        "to": [to_email],
        "reply_to": [settings.owner_notification_email or settings.resend_from_email],
        "template": {
            "id": template_id,
            "variables": variables,
        },
        "tags": [{"name": key, "value": str(value)[:256]} for key, value in tags.items() if value is not None],
    }

    request = Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mellowden-approval-backend/1.1",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            result = json.loads(body) if body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend rejected the email ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Resend: {exc.reason}") from exc

    email_id = str(result.get("id") or "")
    if not email_id:
        raise RuntimeError(f"Resend did not return an email id: {result}")
    return email_id


def send_customer_review_email(settings, row, review_url: str):
    return _send_template(
        settings,
        to_email=row.customer_email,
        template_id=REVIEW_TEMPLATE,
        variables={
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "PET_NAME": row.pet_name or "your pet",
            "PRODUCT_TITLE": row.product_title or "your Mellowden portrait",
            "REVIEW_URL": review_url,
        },
        idempotency_key=f"mellowden-review/{row.shopify_order_id}/{row.approval_token}",
        tags={"order_id": row.shopify_order_id, "kind": "customer_review"},
    )


def send_owner_approved_email(settings, row):
    if not settings.owner_notification_email:
        raise RuntimeError("OWNER_NOTIFICATION_EMAIL is not configured")
    return _send_template(
        settings,
        to_email=settings.owner_notification_email,
        template_id=APPROVED_TEMPLATE,
        variables={
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "CUSTOMER_EMAIL": row.customer_email or "—",
            "PET_NAME": row.pet_name or "—",
            "PRODUCT_TITLE": row.product_title or "Mellowden portrait",
            "ADMIN_URL": "https://mellowden-approval-backend-production.up.railway.app/admin",
        },
        idempotency_key=f"mellowden-approved/{row.shopify_order_id}/{row.approval_token}",
        tags={"order_id": row.shopify_order_id, "kind": "owner_approved"},
    )


def send_owner_revision_email(settings, row, message: str):
    if not settings.owner_notification_email:
        raise RuntimeError("OWNER_NOTIFICATION_EMAIL is not configured")
    return _send_template(
        settings,
        to_email=settings.owner_notification_email,
        template_id=REVISION_TEMPLATE,
        variables={
            "ORDER_NAME": row.order_name or row.shopify_order_id,
            "CUSTOMER_EMAIL": row.customer_email or "—",
            "PET_NAME": row.pet_name or "—",
            "PRODUCT_TITLE": row.product_title or "Mellowden portrait",
            "REVISION_MESSAGE": message,
            "ADMIN_URL": "https://mellowden-approval-backend-production.up.railway.app/admin",
        },
        idempotency_key=f"mellowden-revision/{row.shopify_order_id}/{row.approval_token}/{row.revision_count}",
        tags={"order_id": row.shopify_order_id, "kind": "owner_revision"},
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

    secret = webhook_secret[len("whsec_"):] if webhook_secret.startswith("whsec_") else webhook_secret
    try:
        key = base64.b64decode(secret)
    except Exception as exc:
        raise ValueError("Invalid Resend webhook secret") from exc

    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    signatures = [part.split(",", 1)[1] for part in signature_header.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(candidate, expected) for candidate in signatures):
        raise ValueError("Invalid Resend webhook signature")

    return json.loads(raw_body.decode("utf-8"))
