from app import app, settings, SessionLocal, ApprovalOrder, new_token, customer_review_url
from admin_dashboard import build_admin_router

app.include_router(
    build_admin_router(
        settings,
        SessionLocal,
        ApprovalOrder,
        new_token,
        customer_review_url,
    )
)
