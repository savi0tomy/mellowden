import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resend_mailer import verify_resend_webhook


class TrackingBase(DeclarativeBase):
    pass


class EmailDelivery(TrackingBase):
    __tablename__ = "email_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    resend_email_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    recipient: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(64), default="sent", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_email_tracking(engine):
    TrackingBase.metadata.create_all(bind=engine)


def latest_customer_delivery(db, order_id: str):
    return db.scalar(
        select(EmailDelivery)
        .where(
            EmailDelivery.shopify_order_id == order_id,
            EmailDelivery.kind.in_(["customer_review", "customer_review_reminder"]),
        )
        .order_by(EmailDelivery.created_at.desc())
    )


def _tags_dict(raw_tags) -> dict:
    if isinstance(raw_tags, dict):
        return {str(k): str(v) for k, v in raw_tags.items()}
    if isinstance(raw_tags, list):
        return {
            str(item.get("name")): str(item.get("value") or "")
            for item in raw_tags
            if isinstance(item, dict) and item.get("name")
        }
    return {}


def build_resend_webhook_router(SessionLocal, settings=None, ApprovalOrder=None, send_owner_alert_fn=None):
    router = APIRouter()
    init_email_tracking(SessionLocal.kw["bind"])

    @router.post("/webhooks/resend")
    async def resend_webhook(request: Request):
        raw = await request.body()
        secret = os.getenv("RESEND_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "RESEND_WEBHOOK_SECRET not configured")
        try:
            event = verify_resend_webhook(raw, request.headers, secret)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(401, "Invalid Resend webhook signature") from exc

        event_type = str(event.get("type") or "")
        data = event.get("data") or {}
        email_id = str(data.get("email_id") or "")
        if not email_id or not event_type.startswith("email."):
            return {"ok": True, "ignored": True}

        tags = _tags_dict(data.get("tags"))
        order_id = tags.get("order_id", "")
        kind = tags.get("kind", "")
        to = data.get("to") or []
        recipient = str(to[0] if isinstance(to, list) and to else to or "")
        status = event_type.removeprefix("email.")
        detail = ""
        if status == "bounced":
            bounce = data.get("bounce") or {}
            detail = str(bounce.get("message") or bounce.get("type") or "")
        elif status == "failed":
            detail = str(data.get("error") or data.get("message") or "")
        elif status == "delivery_delayed":
            detail = str(data.get("reason") or "")

        should_alert = False
        with SessionLocal() as db:
            row = db.scalar(select(EmailDelivery).where(EmailDelivery.resend_email_id == email_id))
            previous_status = row.status if row else ""
            if not row:
                row = EmailDelivery(
                    shopify_order_id=order_id,
                    kind=kind,
                    resend_email_id=email_id,
                    recipient=recipient,
                    status=status,
                    detail=detail,
                )
                db.add(row)
            else:
                row.shopify_order_id = order_id or row.shopify_order_id
                row.kind = kind or row.kind
                row.recipient = recipient or row.recipient
                row.status = status
                row.detail = detail
            db.commit()
            should_alert = (
                status in {"bounced", "failed", "complained", "suppressed"}
                and previous_status != status
                and kind in {"customer_review", "customer_review_reminder", "abandoned_checkout"}
                and bool(order_id)
            )

            if should_alert and settings and ApprovalOrder and send_owner_alert_fn and order_id and not order_id.startswith("checkout-"):
                portrait_order = db.scalar(
                    select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id)
                )
                if portrait_order:
                    try:
                        send_owner_alert_fn(
                            settings,
                            portrait_order,
                            "Customer email needs attention",
                            f"Resend reported {status} for {recipient or 'the customer'}. {detail or 'Open the dashboard and use the manual email fallback if needed.'}",
                            f"delivery-{email_id}-{status}",
                        )
                    except Exception:
                        pass

        return {"ok": True}

    return router
