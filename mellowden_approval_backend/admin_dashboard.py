import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from email_tracking import build_resend_webhook_router, latest_customer_delivery

COOKIE_NAME = "mellowden_admin"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def build_admin_router(settings, SessionLocal, ApprovalOrder, new_token, customer_review_url, send_customer_review_email):
    router = APIRouter()
    router.include_router(build_resend_webhook_router(SessionLocal))

    def session_value() -> str:
        expires = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
        payload = f"admin:{expires}"
        sig = hmac.new(settings.admin_api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def is_authenticated(request: Request) -> bool:
        raw = request.cookies.get(COOKIE_NAME, "")
        try:
            role, expires, sig = raw.split(":", 2)
            if role != "admin" or int(expires) < int(datetime.now(timezone.utc).timestamp()):
                return False
            payload = f"{role}:{expires}"
            expected = hmac.new(settings.admin_api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, sig)
        except Exception:
            return False

    def login_page(error: str = "") -> str:
        err = f'<div class="error">{escape(error)}</div>' if error else ""
        return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mellowden Admin</title>
<style>
:root{{--cream:#f6f0e8;--paper:#fffaf4;--ink:#2b2622;--muted:#756b63;--line:#e4d8cb;--accent:#2d2925}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--cream);font-family:Arial,sans-serif;color:var(--ink)}}
.wrap{{min-height:100vh;display:grid;place-items:center;padding:24px}} .card{{width:min(430px,100%);background:var(--paper);border:1px solid var(--line);border-radius:24px;padding:34px;box-shadow:0 20px 60px rgba(50,40,30,.08)}}
.k{{letter-spacing:.22em;font-weight:700;font-size:12px;color:var(--muted);text-transform:uppercase}} h1{{font:700 38px/1.05 Georgia,serif;margin:12px 0 10px}} p{{color:var(--muted);line-height:1.5}} input{{width:100%;padding:14px 15px;border:1px solid var(--line);border-radius:12px;font:inherit;background:white;margin:8px 0 14px}} button{{width:100%;padding:14px;border:0;border-radius:999px;background:var(--accent);color:white;font-weight:700;font-size:15px;cursor:pointer}} .error{{background:#fff0ee;color:#8a2f25;border:1px solid #efc7c1;padding:10px 12px;border-radius:10px;margin:12px 0}}
</style></head><body><div class="wrap"><div class="card"><div class="k">Mellowden</div><h1>Order dashboard</h1><p>Private access for managing portrait approvals.</p>{err}<form method="post" action="/admin/login"><input type="password" name="password" placeholder="Admin password" autocomplete="current-password" required><button type="submit">Sign in</button></form></div></div></body></html>"""

    def status_label(status: str) -> str:
        labels = {
            "WAITING_FOR_ARTWORK": "Waiting for artwork",
            "WAITING_FOR_CUSTOMER_APPROVAL": "Waiting for customer",
            "REVISION_REQUESTED": "Revision requested",
            "APPROVED": "Approved",
            "SENT_TO_PRINT": "Sent to print",
        }
        return labels.get(status, status.replace("_", " ").title())

    def dashboard_html(rows, message: str = "", error_message: str = "") -> str:
        cards = []
        for row in rows:
            review_url = customer_review_url(row.approval_token)
            safe_review = escape(review_url, quote=True)
            safe_email = escape(row.customer_email or "")
            with SessionLocal() as delivery_db:
                delivery = latest_customer_delivery(delivery_db, row.shopify_order_id)
            delivery_html = ""
            if delivery:
                detail = f" · {escape(delivery.detail)}" if delivery.detail else ""
                delivery_html = f'<div class="delivery d-{escape(delivery.status)}">Email: {escape(delivery.status.replace("_", " ").title())}{detail}</div>'
            revision = ""
            if row.revision_request:
                revision = f'<div class="revision"><strong>Customer requested:</strong><br>{escape(row.revision_request)}</div>'
            artwork_preview = ""
            if row.artwork_url:
                artwork_preview = f'<div class="art"><img src="{escape(row.artwork_url, quote=True)}" alt="Artwork preview"></div>'
            pet_img = f'<img class="pet" src="{escape(row.pet_photo_url, quote=True)}" alt="Customer pet photo">' if row.pet_photo_url else '<div class="pet empty">No photo</div>'
            mail_subject = quote(f"Your Mellowden portrait is ready to review — {row.order_name}")
            mail_body = quote(f"Hi,\n\nYour Mellowden portrait is ready to review:\n{review_url}\n\nYou can approve it for printing or request changes there.\n\nNothing is sent to print until you approve the artwork.\n\nMellowden")
            mailto = f"mailto:{quote(row.customer_email or '')}?subject={mail_subject}&body={mail_body}"
            cards.append(f"""
<article class="order">
  <div class="topline"><div><div class="ordername">{escape(row.order_name or row.shopify_order_id)}</div><div class="muted">{escape(row.product_title or '')} · {escape(row.variant_title or '')}</div>{delivery_html}</div><span class="status s-{escape(row.status.lower())}">{escape(status_label(row.status))}</span></div>
  <div class="grid">
    <div>{pet_img}</div>
    <div class="details">
      <div><b>Customer</b><br>{safe_email}</div>
      <div><b>Background</b><br>{escape(row.background_preference or '—')}</div>
      <div><b>Pet name</b><br>{escape(row.pet_name or '—')}</div>
      <div><b>Instructions</b><br>{escape(row.special_instructions or '—')}</div>
    </div>
    <div>{artwork_preview}</div>
  </div>
  {revision}
  <form class="upload" method="post" enctype="multipart/form-data" action="/admin/orders/{escape(row.shopify_order_id, quote=True)}/upload-artwork">
    <label><b>{'Upload revised artwork' if row.status == 'REVISION_REQUESTED' else 'Upload artwork'}</b></label>
    <input type="file" name="artwork" accept="image/jpeg,image/png,image/webp" required>
    <button type="submit">{'Upload revision & email customer' if row.status == 'REVISION_REQUESTED' else 'Upload & email customer'}</button>
  </form>
  <div class="linkrow"><input id="url-{escape(row.shopify_order_id, quote=True)}" value="{safe_review}" readonly><button type="button" onclick="copyUrl('url-{escape(row.shopify_order_id, quote=True)}', this)">Copy URL</button><a class="mail" href="{escape(mailto, quote=True)}">Manual email</a><a class="open" href="{safe_review}" target="_blank" rel="noopener">Open review</a></div>
</article>""")
        empty = '<div class="emptylist">No portrait orders yet.</div>' if not cards else ""
        flash = f'<div class="flash">{escape(message)}</div>' if message else ""
        error_flash = f'<div class="flash errorflash">{escape(error_message)}</div>' if error_message else ""
        return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mellowden Admin</title>
<style>
:root{{--cream:#f6f0e8;--paper:#fffaf4;--ink:#2b2622;--muted:#756b63;--line:#e4d8cb;--accent:#2d2925;--green:#e6f1e8;--amber:#f7edd7;--rose:#f7e1dc}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--cream);color:var(--ink);font-family:Arial,sans-serif}} a{{color:inherit}}
header{{position:sticky;top:0;z-index:5;background:rgba(246,240,232,.94);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}} .head{{max-width:1280px;margin:auto;padding:18px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}} .brand{{font-weight:800;letter-spacing:.22em}} .logout{{border:1px solid var(--line);background:var(--paper);padding:10px 14px;border-radius:999px;text-decoration:none}}
main{{max-width:1280px;margin:auto;padding:34px 24px 70px}} .hero{{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:24px}} h1{{font:700 clamp(34px,5vw,58px)/1 Georgia,serif;margin:0}} .sub{{color:var(--muted);margin-top:8px}} .flash{{background:var(--green);border:1px solid #c8dfcc;padding:12px 14px;border-radius:12px;margin-bottom:18px}} .errorflash{{background:#fff0ee;border-color:#efc7c1;color:#8a2f25}}
.order{{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:22px;margin:0 0 18px;box-shadow:0 14px 40px rgba(55,43,31,.04)}} .topline{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}} .ordername{{font:700 26px Georgia,serif}} .muted{{color:var(--muted);font-size:14px;margin-top:4px}} .status{{white-space:nowrap;padding:8px 11px;border-radius:999px;font-size:12px;font-weight:700;background:#eee5dd}} .s-approved,.s-sent_to_print{{background:var(--green)}} .s-revision_requested{{background:var(--rose)}} .s-waiting_for_customer_approval{{background:var(--amber)}} .delivery{{display:inline-block;margin-top:8px;padding:6px 9px;border-radius:999px;background:#eee5dd;font-size:11px;font-weight:700}} .d-delivered{{background:var(--green)}} .d-bounced,.d-failed,.d-complained,.d-suppressed{{background:var(--rose)}} .d-delivery_delayed{{background:var(--amber)}}
.grid{{display:grid;grid-template-columns:170px minmax(240px,1fr) 220px;gap:20px;margin-top:20px;align-items:start}} .pet,.art img{{width:100%;max-height:220px;object-fit:contain;border-radius:14px;background:#f0e9e1;border:1px solid var(--line)}} .pet.empty{{height:170px;display:grid;place-items:center;color:var(--muted)}} .details{{display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:14px;line-height:1.45}} .details b{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}} .revision{{margin-top:16px;background:var(--rose);border:1px solid #edc8c0;border-radius:12px;padding:13px 14px;line-height:1.5}}
.upload{{margin-top:18px;padding-top:18px;border-top:1px solid var(--line);display:flex;align-items:center;gap:10px;flex-wrap:wrap}} input[type=file]{{max-width:360px}} button,.mail,.open{{border:0;border-radius:999px;padding:11px 15px;font:700 13px Arial,sans-serif;cursor:pointer;text-decoration:none;display:inline-block}} button{{background:var(--accent);color:white}} .mail,.open{{background:#eee5dd}} .linkrow{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}} .linkrow input{{flex:1;min-width:280px;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:white;color:var(--muted)}} .emptylist{{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:28px;color:var(--muted)}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr}} .pet,.art img{{max-height:300px}} .topline{{flex-direction:column}} .details{{grid-template-columns:1fr}} .hero{{align-items:flex-start;flex-direction:column}}}}
</style>
<script>function copyUrl(id,btn){{const el=document.getElementById(id);navigator.clipboard.writeText(el.value).then(()=>{{const old=btn.textContent;btn.textContent='Copied';setTimeout(()=>btn.textContent=old,1200)}})}}</script>
</head><body><header><div class="head"><div class="brand">MELLOWDEN</div><a class="logout" href="/admin/logout">Sign out</a></div></header><main><div class="hero"><div><h1>Portrait orders</h1><div class="sub">Upload artwork, automatically email review links, and track approvals, revisions, and delivery.</div></div></div>{flash}{error_flash}{empty}{''.join(cards)}</main></body></html>"""

    @router.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request, saved: str = "", email: str = ""):
        if not is_authenticated(request):
            return HTMLResponse(login_page())
        with SessionLocal() as db:
            rows = db.scalars(select(ApprovalOrder).order_by(ApprovalOrder.created_at.desc())).all()
        message = ""
        error_message = ""
        if saved == "1" and email == "sent":
            message = "Artwork saved and the review email was sent to the customer."
        elif saved == "1" and email == "failed":
            error_message = "Artwork was saved, but Resend could not send the email. You can still copy the review URL and send it manually."
        elif saved == "1":
            message = "Artwork saved. Review link is ready."
        return HTMLResponse(dashboard_html(rows, message, error_message))

    @router.post("/admin/login")
    async def admin_login(password: str = Form(...)):
        if not hmac.compare_digest(password, settings.admin_api_key):
            return HTMLResponse(login_page("Incorrect password."), status_code=401)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            session_value(),
            max_age=7 * 24 * 60 * 60,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/admin/logout")
    def admin_logout():
        response = RedirectResponse("/admin", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @router.post("/admin/orders/{order_id}/upload-artwork")
    async def upload_artwork(request: Request, order_id: str, artwork: UploadFile = File(...)):
        if not is_authenticated(request):
            return RedirectResponse("/admin", status_code=303)
        content_type = (artwork.content_type or "").lower()
        if content_type not in ALLOWED_IMAGE_TYPES:
            return HTMLResponse("Unsupported image type. Use JPG, PNG or WebP.", status_code=400)
        data = await artwork.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            return HTMLResponse("Artwork must be between 1 byte and 10 MB.", status_code=400)
        data_uri = f"data:{content_type};base64,{base64.b64encode(data).decode()}"

        email_sent = False
        with SessionLocal() as db:
            row = db.scalar(select(ApprovalOrder).where(ApprovalOrder.shopify_order_id == order_id))
            if not row:
                return HTMLResponse("Order not found.", status_code=404)
            row.artwork_url = data_uri
            row.approval_token = new_token()
            row.revision_request = ""
            row.approved_at = None
            row.status = "WAITING_FOR_CUSTOMER_APPROVAL"
            db.commit()
            db.refresh(row)
            review_url = customer_review_url(row.approval_token)
            try:
                send_customer_review_email(settings, row, review_url)
                email_sent = True
            except Exception as exc:
                print(f"[Resend] Customer review email failed for {row.order_name}: {exc}")

        return RedirectResponse(f"/admin?saved=1&email={'sent' if email_sent else 'failed'}", status_code=303)

    return router