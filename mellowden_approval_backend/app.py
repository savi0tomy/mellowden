import base64, hashlib, hmac, secrets, smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Header, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, String, Text, Integer, DateTime, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, Mapped, sessionmaker, Session

class Settings(BaseSettings):
    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./mellowden.db"
    shopify_webhook_secret: str = ""
    admin_api_key: str = "change-this"
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
    database_url = database_url.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1,
    )

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
        p = props_to_dict(line)
        if p.get("Pet photo"):
            return line, p
    return None, None

def send_review_email(to_email, order_name, review_url):
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP not configured")
    msg = EmailMessage()
    msg["Subject"] = f"Your Mellowden portrait is ready to review — {order_name}"
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to_email
    msg.set_content(
        f"""Your personalized Mellowden portrait is ready to review.

Review your artwork:
{review_url}

You can approve it for printing or request changes.

Nothing is sent to print until you approve the artwork.
"""
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)

class ArtworkInput(BaseModel):
    artwork_url: HttpUrl
    send_email: bool = True

app = FastAPI(title="Mellowden Artwork Approval")

@app.get("/health")
def health():
    return {"ok": True}

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

    line, p = extract_personalized_line(payload)
    if not line:
        return {"ok": True, "ignored": True}

    customer = payload.get("customer") or {}
    row = ApprovalOrder(
        shopify_order_id=order_id,
        order_name=str(payload.get("name") or ""),
        customer_email=str(payload.get("email") or customer.get("email") or ""),
        product_title=str(line.get("title") or ""),
        variant_title=str(line.get("variant_title") or ""),
        pet_photo_url=p.get("Pet photo", ""),
        background_preference=p.get("Background preference", ""),
        custom_background=p.get("Custom background", ""),
        pet_name=p.get("Pet name", ""),
        special_instructions=p.get("Special instructions", ""),
        approval_token=new_token(),
    )
    db.add(row)
    db.commit()
    return {"ok": True}

@app.get("/admin/orders", dependencies=[Depends(require_admin)])
def admin_orders(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(ApprovalOrder).order_by(ApprovalOrder.created_at.desc())
    if status:
        stmt = stmt.where(ApprovalOrder.status == status)
    rows = db.scalars(stmt).all()
    return [{
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
    } for r in rows]

@app.post("/admin/orders/{order_id}/artwork", dependencies=[Depends(require_admin)])
def set_artwork(order_id: str, body: ArtworkInput, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id))
    if not row:
        raise HTTPException(404, "Order not found")
    row.artwork_url = str(body.artwork_url)
    row.approval_token = new_token()
    row.revision_request = ""
    row.status = "WAITING_FOR_CUSTOMER_APPROVAL"
    db.commit()
    review_url = f"{settings.app_base_url.rstrip('/')}/review/{row.approval_token}"
    email_sent, email_error = False, None
    if body.send_email:
        try:
            send_review_email(row.customer_email, row.order_name, review_url)
            email_sent = True
        except Exception as e:
            email_error = str(e)
    return {"ok": True, "review_url": review_url, "email_sent": email_sent, "email_error": email_error}

def page(title, body):
    return f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{background:#f7f2ea;color:#302923;font-family:Arial;margin:0}}main{{max-width:760px;margin:50px auto;padding:24px}}.card{{background:#fffaf4;border:1px solid #e3d8cd;border-radius:18px;padding:28px}}h1{{font-family:Georgia,serif}}img{{max-width:100%;max-height:650px;display:block;margin:auto;border-radius:10px}}button{{border:0;border-radius:999px;padding:14px 22px;margin:8px 8px 8px 0;font-size:16px;cursor:pointer}}.primary{{background:#8c6a56;color:white}}.secondary{{background:#eee4da}}textarea{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #d7ccc1}}</style></head>
<body><main><div style="letter-spacing:.18em;text-transform:uppercase;font-size:13px;margin-bottom:20px">Mellowden</div><div class=card><h1>{title}</h1>{body}</div></main></body></html>"""

@app.get("/review/{token}", response_class=HTMLResponse)
def review(token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        raise HTTPException(404, "Invalid or expired review link")
    if row.status == "APPROVED":
        return page("Approved for printing", "<p>Thank you. Your artwork is approved.</p>")
    if row.status == "SENT_TO_PRINT":
        return page("Sent to print", "<p>Your portrait has been sent to production.</p>")
    if not row.artwork_url:
        raise HTTPException(409, "Artwork not ready")
    body = f"""
<p>{row.order_name}{' · ' + row.pet_name if row.pet_name else ''}</p>
<img src="{row.artwork_url}" alt="Artwork preview">
<form method=post action="/review/{token}/approve"><button class=primary>Approve for Printing</button></form>
<button class=secondary onclick="document.getElementById('changes').hidden=false">Request Changes</button>
<form id=changes hidden method=post action="/review/{token}/request-changes">
<textarea name=message rows=5 maxlength=1500 required placeholder="Tell us what you'd like changed..."></textarea>
<button class=secondary>Send change request</button>
</form>
<p><small>Nothing is sent to print until you approve this artwork.</small></p>
"""
    return page("Review your portrait", body)

@app.post("/review/{token}/approve", response_class=HTMLResponse)
def approve(token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        raise HTTPException(404, "Invalid or expired review link")
    if row.status != "WAITING_FOR_CUSTOMER_APPROVAL":
        return page("No action needed", "<p>This artwork is no longer awaiting approval.</p>")
    row.status = "APPROVED"
    row.approved_at = datetime.utcnow()
    db.commit()
    return page("Approved for printing", "<p>Thank you. We’ll now prepare your portrait for production.</p>")

@app.post("/review/{token}/request-changes", response_class=HTMLResponse)
def request_changes(token: str, message: str = Form(...), db: Session = Depends(get_db)):
    row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.approval_token == token))
    if not row:
        raise HTTPException(404, "Invalid or expired review link")
    if row.status != "WAITING_FOR_CUSTOMER_APPROVAL":
        return page("No action needed", "<p>This artwork is no longer awaiting feedback.</p>")
    message = message.strip()
    if not message or len(message) > 1500:
        raise HTTPException(400, "Invalid change request")
    row.revision_request = message
    row.revision_count += 1
    row.status = "REVISION_REQUESTED"
    db.commit()
    return page("Change request received", "<p>Thanks. We’ll prepare a revised preview and send you a new review link.</p>")

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
    return {"ok": True, "status": row.status}
