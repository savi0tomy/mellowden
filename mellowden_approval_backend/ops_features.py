import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import DateTime, Integer, String, Text, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resend_mailer import (
    send_abandoned_checkout_email,
    send_customer_review_reminder,
    send_daily_digest,
    send_owner_alert,
)

logger = logging.getLogger("mellowden.ops")

STATUS_TAGS = {
    "WAITING_FOR_ARTWORK": "mellowden:waiting_artwork",
    "WAITING_FOR_CUSTOMER_APPROVAL": "mellowden:waiting_customer",
    "REVISION_REQUESTED": "mellowden:revision_requested",
    "APPROVED": "mellowden:approved",
    "SENT_TO_PRINT": "mellowden:sent_to_print",
}
ALL_STATUS_TAGS = list(STATUS_TAGS.values())


class OpsBase(DeclarativeBase):
    pass


class ArtworkVersion(OpsBase):
    __tablename__ = "artwork_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    artwork_url: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApprovalEvent(OpsBase):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ReminderState(OpsBase):
    __tablename__ = "reminder_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), index=True)
    reminder_kind: Mapped[str] = mapped_column(String(80), index=True)
    token_marker: Mapped[str] = mapped_column(String(128), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ShopifyOrderSnapshot(OpsBase):
    __tablename__ = "shopify_order_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopify_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    financial_status: Mapped[str] = mapped_column(String(64), default="")
    fulfillment_status: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AbandonedCheckoutState(OpsBase):
    __tablename__ = "abandoned_checkout_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checkout_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    recovery_url: Mapped[str] = mapped_column(Text, default="")
    product_summary: Mapped[str] = mapped_column(Text, default="")
    checkout_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recovered: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_ops(engine):
    OpsBase.metadata.create_all(bind=engine)


def record_event(db, order_id: str, event_type: str, detail: str = ""):
    db.add(ApprovalEvent(shopify_order_id=order_id, event_type=event_type, detail=detail[:4000]))
    db.commit()


def latest_event(db, order_id: str, event_type: str):
    return db.scalar(
        select(ApprovalEvent)
        .where(
            ApprovalEvent.shopify_order_id == order_id,
            ApprovalEvent.event_type == event_type,
        )
        .order_by(ApprovalEvent.created_at.desc())
    )


def list_events(db, order_id: str, limit: int = 25):
    return list(
        db.scalars(
            select(ApprovalEvent)
            .where(ApprovalEvent.shopify_order_id == order_id)
            .order_by(ApprovalEvent.created_at.desc())
            .limit(limit)
        ).all()
    )


def save_artwork_version(db, row, artwork_url: str, note: str = ""):
    latest_number = db.scalar(
        select(func.max(ArtworkVersion.version_number)).where(
            ArtworkVersion.shopify_order_id == row.shopify_order_id
        )
    ) or 0
    version = ArtworkVersion(
        shopify_order_id=row.shopify_order_id,
        version_number=int(latest_number) + 1,
        artwork_url=artwork_url,
        note=note[:4000],
    )
    db.add(version)
    db.commit()
    return version


def list_artwork_versions(db, order_id: str):
    return list(
        db.scalars(
            select(ArtworkVersion)
            .where(ArtworkVersion.shopify_order_id == order_id)
            .order_by(ArtworkVersion.version_number.desc())
        ).all()
    )


def shopify_admin_order_url(settings, order_id: str) -> str:
    store_handle = (settings.shopify_store_domain or "").split(".", 1)[0]
    if not store_handle:
        store_handle = "hjkvek-1w"
    return f"https://admin.shopify.com/store/{store_handle}/orders/{order_id}"


def _shopify_graphql(settings, query: str, variables: dict):
    domain = (settings.shopify_store_domain or "").strip().replace("https://", "").rstrip("/")
    token = (settings.shopify_admin_access_token or "").strip()
    if not domain or not token:
        return None
    url = f"https://{domain}/admin/api/2026-07/graphql.json"
    request = Request(
        url,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
            "User-Agent": "mellowden-approval-backend/1.2",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Shopify Admin API rejected request ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Shopify Admin API: {exc.reason}") from exc
    if result.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {result['errors'][:3]}")
    return result.get("data") or {}


def sync_shopify_order_metadata(settings, row, review_url: str):
    if not settings.shopify_admin_access_token:
        return False
    order_gid = f"gid://shopify/Order/{row.shopify_order_id}"
    tag = STATUS_TAGS.get(row.status, "mellowden:portrait")
    query = """
mutation SyncMellowdenOrder($id: ID!, $remove: [String!]!, $add: [String!]!, $metafields: [MetafieldsSetInput!]!) {
  tagsRemove(id: $id, tags: $remove) { userErrors { field message } }
  tagsAdd(id: $id, tags: $add) { userErrors { field message } }
  metafieldsSet(metafields: $metafields) { userErrors { field message } }
}
"""
    variables = {
        "id": order_gid,
        "remove": ALL_STATUS_TAGS,
        "add": ["mellowden:portrait", tag],
        "metafields": [
            {
                "ownerId": order_gid,
                "namespace": "mellowden",
                "key": "approval_status",
                "type": "single_line_text_field",
                "value": row.status,
            },
            {
                "ownerId": order_gid,
                "namespace": "mellowden",
                "key": "review_url",
                "type": "url",
                "value": review_url,
            },
            {
                "ownerId": order_gid,
                "namespace": "mellowden",
                "key": "revision_count",
                "type": "number_integer",
                "value": str(row.revision_count or 0),
            },
        ],
    }
    data = _shopify_graphql(settings, query, variables)
    if data is None:
        return False
    errors = []
    for key in ("tagsRemove", "tagsAdd", "metafieldsSet"):
        errors.extend((data.get(key) or {}).get("userErrors") or [])
    if errors:
        raise RuntimeError(f"Shopify metadata sync errors: {errors[:3]}")
    return True


def fetch_shopify_order_snapshot(settings, db, order_id: str):
    if not settings.shopify_admin_access_token:
        return None
    query = """
query MellowdenOrderLive($id: ID!) {
  order(id: $id) {
    id
    displayFinancialStatus
    displayFulfillmentStatus
  }
}
"""
    data = _shopify_graphql(settings, query, {"id": f"gid://shopify/Order/{order_id}"})
    order = (data or {}).get("order")
    if not order:
        return None
    snapshot = db.scalar(
        select(ShopifyOrderSnapshot).where(ShopifyOrderSnapshot.shopify_order_id == order_id)
    )
    if not snapshot:
        snapshot = ShopifyOrderSnapshot(shopify_order_id=order_id)
        db.add(snapshot)
    snapshot.financial_status = str(order.get("displayFinancialStatus") or "")
    snapshot.fulfillment_status = str(order.get("displayFulfillmentStatus") or "")
    snapshot.updated_at = datetime.utcnow()
    db.commit()
    return snapshot


def get_shopify_snapshot(db, order_id: str):
    return db.scalar(
        select(ShopifyOrderSnapshot).where(ShopifyOrderSnapshot.shopify_order_id == order_id)
    )


def _parse_shopify_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def fetch_abandoned_checkouts(settings):
    if not settings.shopify_admin_access_token:
        return []
    query = """
query MellowdenAbandonedCheckouts($first: Int!) {
  abandonedCheckouts(first: $first, reverse: true, sortKey: UPDATED_AT, query: "status:open recovery_state:not_recovered") {
    nodes {
      id
      createdAt
      updatedAt
      abandonedCheckoutUrl
      customer { email }
      lineItems(first: 10) { nodes { title } }
    }
  }
}
"""
    data = _shopify_graphql(settings, query, {"first": 50})
    return ((data or {}).get("abandonedCheckouts") or {}).get("nodes") or []


def _reminder_state(db, order_id: str, kind: str, marker: str):
    state = db.scalar(
        select(ReminderState).where(
            ReminderState.shopify_order_id == order_id,
            ReminderState.reminder_kind == kind,
        )
    )
    if not state:
        state = ReminderState(
            shopify_order_id=order_id,
            reminder_kind=kind,
            token_marker=marker,
            count=0,
        )
        db.add(state)
        db.commit()
        db.refresh(state)
    elif state.token_marker != marker:
        state.token_marker = marker
        state.count = 0
        state.last_sent_at = None
        db.commit()
    return state


def process_review_reminders(settings, SessionLocal, ApprovalOrder, customer_review_url):
    thresholds = [int(settings.review_reminder_hours), int(settings.review_reminder_hours) * 2]
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = db.scalars(
            select(ApprovalOrder).where(ApprovalOrder.status == "WAITING_FOR_CUSTOMER_APPROVAL")
        ).all()
        for row in rows:
            state = _reminder_state(db, row.shopify_order_id, "customer_review", row.approval_token)
            sequence = state.count + 1
            if sequence > len(thresholds):
                continue
            base_event = latest_event(db, row.shopify_order_id, "ARTWORK_UPLOADED")
            base_time = base_event.created_at if base_event else row.updated_at
            if not base_time or now - base_time < timedelta(hours=thresholds[sequence - 1]):
                continue
            try:
                send_customer_review_reminder(
                    settings,
                    row,
                    customer_review_url(row.approval_token),
                    sequence,
                )
                state.count = sequence
                state.last_sent_at = now
                db.commit()
                record_event(db, row.shopify_order_id, "REVIEW_REMINDER_SENT", f"Reminder #{sequence}")
            except Exception:
                logger.exception("Review reminder failed for %s", row.order_name)


def process_revision_owner_reminders(settings, SessionLocal, ApprovalOrder):
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = db.scalars(
            select(ApprovalOrder).where(ApprovalOrder.status == "REVISION_REQUESTED")
        ).all()
        for row in rows:
            marker = f"{row.approval_token}:{row.revision_count}"
            state = _reminder_state(db, row.shopify_order_id, "owner_revision_overdue", marker)
            if state.count:
                continue
            event = latest_event(db, row.shopify_order_id, "REVISION_REQUESTED")
            base_time = event.created_at if event else row.updated_at
            if not base_time or now - base_time < timedelta(hours=int(settings.revision_owner_reminder_hours)):
                continue
            try:
                send_owner_alert(
                    settings,
                    row,
                    "Revision still needs artwork",
                    f"The customer requested changes {int(settings.revision_owner_reminder_hours)}+ hours ago and no revised artwork has been uploaded yet. Their latest request is: {row.revision_request or '—'}",
                    f"revision-overdue-{row.revision_count}",
                )
                state.count = 1
                state.last_sent_at = now
                db.commit()
                record_event(db, row.shopify_order_id, "OWNER_REVISION_REMINDER_SENT")
            except Exception:
                logger.exception("Revision owner reminder failed for %s", row.order_name)


def process_daily_digest(settings, SessionLocal, ApprovalOrder):
    now = datetime.now(timezone.utc)
    if now.hour != int(settings.daily_digest_hour_utc):
        return
    date_label = now.date().isoformat()
    with SessionLocal() as db:
        marker = db.scalar(
            select(ApprovalEvent).where(
                ApprovalEvent.shopify_order_id == "store",
                ApprovalEvent.event_type == "DAILY_DIGEST_SENT",
                ApprovalEvent.detail == date_label,
            )
        )
        if marker:
            return
        rows = db.scalars(select(ApprovalOrder)).all()
        counts = {key: 0 for key in STATUS_TAGS}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        overdue = []
        cutoff = datetime.utcnow() - timedelta(hours=int(settings.revision_owner_reminder_hours))
        for row in rows:
            if row.status == "REVISION_REQUESTED" and row.updated_at and row.updated_at < cutoff:
                overdue.append(row.order_name or row.shopify_order_id)
        summary = "No overdue actions." if not overdue else "Revision artwork overdue: " + ", ".join(overdue[:10])
        try:
            send_daily_digest(settings, date_label=date_label, counts=counts, overdue_summary=summary)
            record_event(db, "store", "DAILY_DIGEST_SENT", date_label)
        except Exception:
            logger.exception("Daily digest failed")


def process_abandoned_checkouts(settings, SessionLocal):
    try:
        nodes = fetch_abandoned_checkouts(settings)
    except Exception:
        logger.exception("Could not fetch abandoned Shopify checkouts")
        return
    now = datetime.utcnow()
    with SessionLocal() as db:
        for node in nodes:
            checkout_id = str(node.get("id") or "").rsplit("/", 1)[-1]
            customer = node.get("customer") or {}
            email = str(customer.get("email") or "")
            recovery_url = str(node.get("abandonedCheckoutUrl") or "")
            titles = [str(n.get("title") or "") for n in ((node.get("lineItems") or {}).get("nodes") or [])]
            product_summary = ", ".join([t for t in titles if t][:3]) or "your personalized Mellowden portrait"
            created_at = _parse_shopify_datetime(node.get("createdAt"))
            if not checkout_id or not email or not recovery_url:
                continue
            state = db.scalar(select(AbandonedCheckoutState).where(AbandonedCheckoutState.checkout_id == checkout_id))
            if not state:
                state = AbandonedCheckoutState(
                    checkout_id=checkout_id,
                    email=email,
                    recovery_url=recovery_url,
                    product_summary=product_summary,
                    checkout_created_at=created_at,
                )
                db.add(state)
                db.commit()
            else:
                state.email = email
                state.recovery_url = recovery_url
                state.product_summary = product_summary
                state.checkout_created_at = created_at or state.checkout_created_at
                db.commit()
            base_time = state.checkout_created_at or now
            if state.reminder_sent_at or now - base_time < timedelta(hours=int(settings.abandoned_checkout_reminder_hours)):
                continue
            try:
                send_abandoned_checkout_email(
                    settings,
                    checkout_id=checkout_id,
                    email=email,
                    recovery_url=recovery_url,
                    product_summary=product_summary,
                )
                state.reminder_sent_at = now
                db.commit()
            except Exception:
                logger.exception("Abandoned checkout email failed for %s", checkout_id)


def refresh_shopify_snapshots(settings, SessionLocal, ApprovalOrder):
    if not settings.shopify_admin_access_token:
        return
    with SessionLocal() as db:
        rows = db.scalars(
            select(ApprovalOrder).where(ApprovalOrder.status != "SENT_TO_PRINT")
        ).all()
        for row in rows[:50]:
            try:
                fetch_shopify_order_snapshot(settings, db, row.shopify_order_id)
            except Exception:
                logger.exception("Could not refresh Shopify order %s", row.shopify_order_id)


async def operations_loop(settings, SessionLocal, ApprovalOrder, customer_review_url):
    await asyncio.sleep(20)
    while True:
        try:
            process_review_reminders(settings, SessionLocal, ApprovalOrder, customer_review_url)
            process_revision_owner_reminders(settings, SessionLocal, ApprovalOrder)
            process_daily_digest(settings, SessionLocal, ApprovalOrder)
            process_abandoned_checkouts(settings, SessionLocal)
            refresh_shopify_snapshots(settings, SessionLocal, ApprovalOrder)
        except Exception:
            logger.exception("Operations loop iteration failed")
        await asyncio.sleep(max(5, int(settings.ops_poll_minutes)) * 60)
