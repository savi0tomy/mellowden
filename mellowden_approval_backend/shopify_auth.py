import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {
    "access_token": "",
    "expires_at": 0.0,
    "cache_key": "",
}


def _domain(settings) -> str:
    return (getattr(settings, "shopify_store_domain", "") or "").strip().replace("https://", "").rstrip("/")


def _client_id() -> str:
    return os.getenv("SHOPIFY_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()


def shopify_admin_configured(settings) -> bool:
    legacy_token = (getattr(settings, "shopify_admin_access_token", "") or "").strip()
    return bool(_domain(settings) and (legacy_token or (_client_id() and _client_secret())))


def invalidate_shopify_admin_token() -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE["access_token"] = ""
        _TOKEN_CACHE["expires_at"] = 0.0
        _TOKEN_CACHE["cache_key"] = ""


def get_shopify_admin_token(settings, force_refresh: bool = False) -> str:
    """Return a usable Admin API token.

    Preferred production path: SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET using
    Shopify's client-credentials grant. The returned 24-hour token is cached in
    memory and refreshed five minutes before expiry.

    SHOPIFY_ADMIN_ACCESS_TOKEN remains supported as a backwards-compatible
    fallback, but is no longer required.
    """
    legacy_token = (getattr(settings, "shopify_admin_access_token", "") or "").strip()
    client_id = _client_id()
    client_secret = _client_secret()
    domain = _domain(settings)

    if not domain:
        raise RuntimeError("SHOPIFY_STORE_DOMAIN is not configured")

    if not client_id or not client_secret:
        if legacy_token:
            return legacy_token
        raise RuntimeError("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are not configured")

    cache_key = f"{domain}:{client_id}"
    now = time.time()
    with _TOKEN_LOCK:
        if (
            not force_refresh
            and _TOKEN_CACHE["access_token"]
            and _TOKEN_CACHE["cache_key"] == cache_key
            and now < float(_TOKEN_CACHE["expires_at"])
        ):
            return str(_TOKEN_CACHE["access_token"])

        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")
        request = Request(
            f"https://{domain}/admin/oauth/access_token",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "mellowden-approval-backend/1.3",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Shopify token request rejected ({exc.code}): {detail[:500]}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach Shopify token endpoint: {exc.reason}") from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Shopify token response did not include access_token")

        try:
            expires_in = int(payload.get("expires_in") or 86399)
        except (TypeError, ValueError):
            expires_in = 86399

        # Refresh before expiry; keep at least a short usable cache window.
        refresh_margin = min(300, max(30, expires_in // 10))
        _TOKEN_CACHE["access_token"] = access_token
        _TOKEN_CACHE["expires_at"] = time.time() + max(30, expires_in - refresh_margin)
        _TOKEN_CACHE["cache_key"] = cache_key
        return access_token
