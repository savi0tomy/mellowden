# Mellowden Artwork Approval Backend

This implements the post-purchase approval flow:

Shopify order -> waiting for artwork -> upload finished artwork -> email secure review link ->
Approve for Printing OR Request Changes -> revision loop -> APPROVED -> manually update/approve Gelato -> SENT_TO_PRINT.

## Run
1. `python -m venv .venv`
2. Activate it.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`
5. `uvicorn app:app --reload`

Docs: `http://localhost:8000/docs`

## Shopify webhook
After deployment to HTTPS, create an `orders/create` webhook pointing to:

`https://YOUR-BACKEND/webhooks/shopify/orders-create`

Set the same Shopify webhook signing secret in `SHOPIFY_WEBHOOK_SECRET`.

## Internal workflow
List orders:
`GET /admin/orders`
Header: `X-Admin-Key: ...`

Set finished artwork + send review email:
`POST /admin/orders/{shopify_order_id}/artwork`

JSON:
`{"artwork_url":"https://...","send_email":true}`

Revision requests appear with status `REVISION_REQUESTED`.
Upload revised art through the same artwork endpoint; this generates a fresh token and sends another review email.

After customer approves, status becomes `APPROVED`.

After you replace the demo artwork in Gelato and manually approve production:
`POST /admin/orders/{shopify_order_id}/mark-sent-to-print`

## Statuses
- WAITING_FOR_ARTWORK
- WAITING_FOR_CUSTOMER_APPROVAL
- REVISION_REQUESTED
- APPROVED
- SENT_TO_PRINT

The service intentionally does not auto-submit to Gelato. That final production action remains manual for safety.
