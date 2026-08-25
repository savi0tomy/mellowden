import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from html import escape
from typing import Optional

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from admin_dashboard import build_admin_router
from ops_features import (
    init_ops,
    operations_loop,
    record_event,
    save_artwork_version,
    sync_shopify_order_metadata,
)
from resend_mailer import (
    send_customer_review_email,
    send_owner_alert,
    send_owner_approved_email,
    send_owner_revision_email,
)

logger = logging.getLogger("mellowden")


class Settings(BaseSettings):
    app_base_url: str = "https://mellowden-approval-backend-production.up.railway.app"
    storefront_review_url: str = "https://mellowden.store/pages/review"
    database_url: str = "sqlite:///./mellowden.db"
    shopify_webhook_secret: str = ""
    admin_api_key: str = "change-this"

    # Optional Admin API access enables live Shopify status, order tags/metafields,
    # and abandoned-checkout recovery.
    shopify_store_domain: str = "hjkvek-1w.myshopify.com"
    shopify_admin_access_token: str = ""

    # Resend transactional email
    resend_api_key: str = ""
    resend_from_email: str = ""
    owner_notification_email: str = ""

    # Operations automation defaults.
    review_reminder_hours: int = 24
    revision_owner_reminder_hours: int = 24
    abandoned_checkout_reminder_hours: int = 4
    daily_digest_hour_utc: int = 3
    ops_poll_minutes: int = 15

    # Legacy SMTP variables remain harmless if they still exist in Railway.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Mellowden"
    smtp_use_tls: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
database_url = settings.database_url
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class ApprovalOrder(Base):
    __tablename__ = "approval_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_name: Mapped[str] = mapped_column(String(64), default="")
    customer_email: Mapped[str] = mapped_column(String(255), default="")
    product_title: Mapped[str] = mapped_column(String(255), default="")
    variant_title: Mapped[str] = mapped_column(String(255), default="")
    pet_photo_url: Mapped[str] = mapped_column(Text, default="")
    background_preference: Mapped[str] = mapped_column(String(255), default="")
    custom_background: Mapped[str] = mapped_column(Text, default="")
    pet_name: Mapped[str] = mapped_column(String(120), default="")
    special_instructions: Mapped[str] = mapped_column(Text, default="")
    artwork_url: Mapped[str] = mapped_column(Text, default="")
    approval_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(64), default="WAITING_FOR_ARTWORK", index=True)
    revision_request: Mapped[str] = mapped_column(Text, default="")
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sent_to_print_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)
init_ops(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_webhook(raw: bytes, received: str | None):
    if not settings.shopify_webhook_secret:
        raise HTTPException(500, "SHOPIFY_WEBHOOK_SECRET not configured")
    digest = base64.b64encode(
        hmac.new(settings.shopify_webhook_secret.encode(), raw, hashlib.sha256).digest()
    ).decode()
    if not received or not hmac.compare_digest(digest, received):
        raise HTTPException(401, "Invalid Shopify webhook signature")


def require_admin(x_admin_key: str | None = Header(default=None)):
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(401, "Invalid admin key")


def new_token():
    return secrets.token_urlsafe(32)


def props_to_dict(line):
    return {
        str(p.get("name") or "").strip(): str(p.get("value") or "")
        for p in (line.get("properties") or [])
        if p.get("name")
    }


def extract_personalized_line(payload):
    for line in payload.get("line_items") or []:
        props = props_to_dict(line)
        if props.get("Pet photo"):
            return line, props
    return None, None


def customer_review_url(token: str) -> str:
    base = settings.storefront_review_url.rstrip("?")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={token}"


def safe_sync_shopify(db: Session, row: ApprovalOrder):
    try:
        if sync_shopify_order_metadata(settings, row, customer_review_url(row.approval_token)):
            record_event(db, row.shopify_order_id, "SHOPIFY_STATUS_SYNCED", row.status)
    except Exception as exc:
        logger.exception("Shopify metadata sync failed for %s", row.order_name)
        record_event(db, row.shopify_order_id, "SHOPIFY_SYNC_FAILED", str(exc)[:500])


class ArtworkInput(BaseModel):
    artwork_url: HttpUrl
    send_email: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(
        operations_loop(settings, SessionLocal, ApprovalOrder, customer_review_url)
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Mellowden Artwork Approval", lifespan=lifespan)
app.include_router(
    build_admin_router(
        settings,
        SessionLocal,
        ApprovalOrder,
        new_token,
        customer_review_url,
        send_customer_review_email,
    )
)


@app.get("/health")
def health():
    return {
        "ok": True,
        "resend_configured": bool(settings.resend_api_key and settings.resend_from_email),
        "shopify_admin_automation_configured": bool(settings.shopify_admin_access_token),
        "operations_loop": True,
    }


@app.post("/webhooks/shopify/orders-create")
async def orders_create(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    verify_webhook(raw, request.headers.get("X-Shopify-Hmac-Sha256"))
    payload = await request.json()
    order_id = str(payload.get("id") or "")
    if not order_id:
        raise HTTPException(400, "Missing order id")
    if db.scalar(select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id)):
        return {"ok": True, "duplicate": True}

    line, props = extract_personalized_line(payload)
    if not line:
        return {"ok": True, "ignored": True}

    customer = payload.get("customer") or {}
    row = ApprovalOrder(
        shopify_order_id=order_id,
        order_name=str(payload.get("name") or ""),
        customer_email=str(payload.get("email") or customer.get("email") or ""),
        product_title=str(line.get("title") or ""),
        variant_title=str(line.get("variant_title") or ""),
        pet_photo_url=props.get("Pet photo", ""),
        background_preference=props.get("Background preference", ""),
        custom_background=props.get("Custom background", ""),
        pet_name=props.get("Pet name", ""),
        special_instructions=props.get("Special instructions", ""),
        approval_token=new_token(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_event(db, order_id, "ORDER_RECEIVED", "Personalized portrait order received from Shopify")
    safe_sync_shopify(db, row)

    try:
        send_owner_alert(
            settings,
            row,
            "New portrait order",
            f"A new personalized portrait order is waiting for artwork. Product: {row.product_title or 'Mellowden portrait'}. Background: {row.background_preference or '—'}. Special instructions: {row.special_instructions or '—'}",
            "new-order",
        )
        record_event(db, order_id, "OWNER_NEW_ORDER_EMAIL_SENT")
    except Exception as exc:
        logger.exception("New-order owner email failed for %s", row.order_name)
        record_event(db, order_id, "OWNER_NEW_ORDER_EMAIL_FAILED", str(exc)[:500])

    return {"ok": True}


@app.get("/admin/orders", dependencies=[Depends(require_admin)])
def admin_orders(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(ApprovalOrder).order_by(ApprovalOrder.created_at.desc())
    if status:
        stmt = stmt.where(ApprovalOrder.status == status)
    rows = db.scalars(stmt).all()
    return [
        {
            "shopify_order_id": r.shopify_order_id,
            "order_name": r.order_name,
            "customer_email": r.customer_email,
            "product_title": r.product_title,
            "variant_title": r.variant_title,
            "pet_photo_url": r.pet_photo_url,
            "background_preference": r.background_preference,
            "custom_background": r.custom_background,
            "pet_name": r.pet_name,
            "special_instructions": r.special_instructions,
            "artwork_url": r.artwork_url,
            "status": r.status,
            "revision_request": r.revision_request,
            "revision_count": r.revision_count,
            "review_url": customer_review_url(r.approval_token),
        }
        for r in rows
    ]


@app.post("/admin/orders/{order_id}/artwork", dependencies=[Depends(require_admin)])
def set_artwork(order_id: str, body: ArtworkInput, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id))
    if not row:
        raise HTTPException(404, "Order not found")

    previous_revision = row.revision_request or ""
    save_artwork_version(db, row, str(body.artwork_url), previous_revision)
    row.artwork_url = str(body.artwork_url)
    row.approval_token = new_token()
    row.revision_request = ""
    row.approved_at = None
    row.status = "WAITING_FOR_CUSTOMER_APPROVAL"
    db.commit()
    db.refresh(row)
    record_event(db, row.shopify_order_id, "ARTWORK_UPLOADED", "Artwork uploaded through admin API")
    safe_sync_shopify(db, row)

    review_url = customer_review_url(row.approval_token)
    email_sent = False
    email_error = None
    if body.send_email:
        try:
            send_customer_review_email(settings, row, review_url)
            email_sent = True
            record_event(db, row.shopify_order_id, "REVIEW_EMAIL_SENT")
        except Exception as exc:
            email_error = str(exc)
            logger.exception("Resend review email failed for %s", row.order_name)
            record_event(db, row.shopify_order_id, "REVIEW_EMAIL_FAILED", email_error[:500])

    return {
        "ok": True,
        "review_url": review_url,
        "email_sent": email_sent,
        "email_error": email_error,
    }


def page(title, body, eyebrow="Mellowden Portrait Review"):
    safe_title = escape(title)
    safe_eyebrow = escape(eyebrow)
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--cream:#f5efe7;--paper:#fffaf4;--ink:#2f2925;--muted:#75675d;--line:#e6d9cc;--accent:#7b5f4d;--soft:#eee4da}}
*{{box-sizing:border-box}} body{{background:var(--cream);color:var(--ink);font-family:Arial,sans-serif;margin:0}}
main{{max-width:860px;margin:0 auto;padding:46px 22px 64px}} .eyebrow{{letter-spacing:.22em;text-transform:uppercase;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:14px}}
.card{{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:34px;box-shadow:0 18px 50px rgba(61,45,35,.06)}} h1{{font-family:Georgia,serif;font-size:clamp(34px,5vw,52px);line-height:1.05;margin:0 0 18px}}
p{{font-size:17px;line-height:1.65;color:var(--muted)}} .meta{{font-size:14px;color:var(--muted);margin:0 0 22px}} .preview{{background:#f1ebe4;border:1px solid var(--line);border-radius:16px;padding:14px;margin:26px 0}}
img{{max-width:100%;max-height:700px;display:block;margin:auto;border-radius:10px}} .actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:24px}} button{{border:0;border-radius:999px;padding:14px 22px;font-size:16px;cursor:pointer}}
.primary{{background:var(--accent);color:white}} .secondary{{background:var(--soft);color:var(--ink)}} textarea{{width:100%;padding:14px;border-radius:12px;border:1px solid #d7ccc1;background:#fff;font:inherit;min-height:130px;margin-top:14px}}
.note{{font-size:14px;margin-top:24px}} .status{{display:inline-block;padding:7px 11px;border-radius:999px;background:var(--soft);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:18px}}
</style></head><body><main><div class="eyebrow">{safe_eyebrow}</div><div class="card"><h1>{safe_title}</h1>{body}</div></main></body></html>"""


def invalid_review_page(message="This review link is invalid, expired, or has been replaced by a newer one."):
    return HTMLResponse(
        page(
            "This review link is no longer valid",
            f"<p>{escape(message)}</p><p class='note'>If you need a fresh link, reply to your Mellowden email and we’ll help you.</p>",
        ),
        status_code=404,
    )


@app.get("/review/{token}", response_class=HTMLResponse)
def review(token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        return invalid_review_page()

    order_name = escape(row.order_name or "Your order")
    pet_name = escape(row.pet_name or "")
    product_title = escape(row.product_title or "Mellowden portrait")
    meta = f"<p class='meta'>{order_name} · {product_title}{' · ' + pet_name if pet_name else ''}</p>"

    if row.status == "WAITING_FOR_ARTWORK":
        return page(
            "We’re creating your portrait",
            meta + "<div class='status'>In progress</div><p>Our artist is preparing your personalized artwork. There’s nothing you need to do yet.</p><p class='note'>We’ll send you a review link as soon as your preview is ready.</p>",
        )
    if row.status == "REVISION_REQUESTED":
        return page(
            "Your changes are with our artist",
            meta + "<div class='status'>Revision requested</div><p>Thanks for your feedback. We’re preparing an updated version of your portrait now.</p><p class='note'>When the revision is ready, you’ll receive a fresh review link.</p>",
        )
    if row.status == "APPROVED":
        return page(
            "Approved for printing",
            meta + "<div class='status'>Approved</div><p>Thank you. Your artwork has been approved and no further action is needed from you.</p><p class='note'>We’ll now prepare your portrait for production.</p>",
        )
    if row.status == "SENT_TO_PRINT":
        return page(
            "Your portrait is in production",
            meta + "<div class='status'>In production</div><p>Your approved portrait has been sent to print.</p>",
        )
    if row.status != "WAITING_FOR_CUSTOMER_APPROVAL":
        return page("Portrait review", meta + "<p>This order isn’t currently awaiting an action.</p>")
    if not row.artwork_url:
        return page("We’re creating your portrait", meta + "<p>Your artwork preview isn’t ready yet. Please check back later.</p>")

    artwork_url = escape(row.artwork_url, quote=True)
    safe_token = escape(token, quote=True)
    body = f"""
{meta}
<div class="status">Ready for review</div>
<p>Please look over your portrait carefully. Approve it when you’re happy, or tell us what you’d like adjusted.</p>
<div class="preview"><img src="{artwork_url}" alt="Your Mellowden artwork preview"></div>
<div class="actions">
<form method="post" action="/review/{safe_token}/approve"><button class="primary">Approve for printing</button></form>
<button class="secondary" type="button" onclick="document.getElementById('changes').hidden=false;this.hidden=true">Request changes</button>
</div>
<form id="changes" hidden method="post" action="/review/{safe_token}/request-changes">
<textarea name="message" maxlength="1500" required placeholder="Tell us what you'd like changed..."></textarea>
<button class="secondary" type="submit">Send change request</button>
</form>
<p class="note">Nothing is sent to print until you approve this artwork.</p>
"""
    return page("Your portrait is ready to review", body)


@app.post("/review/{token}/approve", response_class=HTMLResponse)
def approve(token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        return invalid_review_page()
    if row.status == "APPROVED":
        return page("Approved for printing", "<p>Thank you. Your artwork is already approved.</p>")
    if row.status != "WAITING_FOR_CUSTOMER_APPROVAL":
        return page("No action needed", "<p>This artwork is no longer awaiting approval.</p>")

    row.status = "APPROVED"
    row.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    record_event(db, row.shopify_order_id, "CUSTOMER_APPROVED")
    safe_sync_shopify(db, row)

    try:
        send_owner_approved_email(settings, row)
        record_event(db, row.shopify_order_id, "OWNER_APPROVAL_EMAIL_SENT")
    except Exception as exc:
        logger.exception("Resend owner approval notification failed for %s", row.order_name)
        record_event(db, row.shopify_order_id, "OWNER_APPROVAL_EMAIL_FAILED", str(exc)[:500])

    return page(
        "Approved for printing",
        "<div class='status'>Approved</div><p>Thank you. We’ll now prepare your portrait for production.</p>",
    )


@app.post("/review/{token}/request-changes", response_class=HTMLResponse)
def request_changes(token: str, message: str = Form(...), db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        return invalid_review_page()
    if row.status == "REVISION_REQUESTED":
        return page("Changes received", "<p>Your change request is already with our artist.</p>")
    if row.status != "WAITING_FOR_CUSTOMER_APPROVAL":
        return page("No action needed", "<p>This artwork is no longer awaiting feedback.</p>")

    message = message.strip()
    if not message or len(message) > 1500:
        raise HTTPException(400, "Invalid change request")

    row.revision_request = message
    row.revision_count += 1
    row.status = "REVISION_REQUESTED"
    db.commit()
    db.refresh(row)
    record_event(db, row.shopify_order_id, "REVISION_REQUESTED", message)
    safe_sync_shopify(db, row)

    try:
        send_owner_revision_email(settings, row, message)
        record_event(db, row.shopify_order_id, "OWNER_REVISION_EMAIL_SENT")
    except Exception as exc:
        logger.exception("Resend owner revision notification failed for %s", row.order_name)
        record_event(db, row.shopify_order_id, "OWNER_REVISION_EMAIL_FAILED", str(exc)[:500])

    return page(
        "Your changes are with our artist",
        "<div class='status'>Revision requested</div><p>Thanks. We’ll prepare a revised preview and send you a fresh review link when it’s ready.</p>",
    )


@app.post("/admin/orders/{order_id}/mark-sent-to-print", dependencies=[Depends(require_admin)])
def mark_sent_to_print(order_id: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id))
    if not row:
        raise HTTPException(404, "Order not found")
    if row.status != "APPROVED":
        raise HTTPException(409, "Order must be APPROVED first")
    row.status = "SENT_TO_PRINT"
    row.sent_to_print_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    record_event(db, row.shopify_order_id, "SENT_TO_PRINT")
    safe_sync_shopify(db, row)
    return {"ok": True, "status": row.status}
