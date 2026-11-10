"""Gateways de pagamento: SyncPay (Pix em R$) e Stripe (cartão em US$/€, opcional)."""

import asyncio
import io
import os
import time

import httpx
import qrcode
import stripe

SYNCPAY_CLIENT_ID = os.environ.get("SYNCPAY_CLIENT_ID", "")
SYNCPAY_CLIENT_SECRET = os.environ.get("SYNCPAY_CLIENT_SECRET", "")
SYNCPAY_BASE = "https://api.syncpayments.com.br/api/partner/v1"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # só se o painel tiver endereço público

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_ON = bool(stripe.api_key)

http = httpx.AsyncClient(timeout=20)
_token = {"value": None, "exp": 0.0}


# ───────────────────────── SyncPay ─────────────────────────
async def _syncpay_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = await http.post(
        f"{SYNCPAY_BASE}/auth-token",
        json={"client_id": SYNCPAY_CLIENT_ID, "client_secret": SYNCPAY_CLIENT_SECRET},
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _token["value"]


async def _syncpay(method: str, path: str, **kw) -> dict:
    headers = {"Authorization": f"Bearer {await _syncpay_token()}", "Accept": "application/json"}
    r = await http.request(method, f"{SYNCPAY_BASE}{path}", headers=headers, **kw)
    if r.status_code == 401:  # token venceu antes da hora
        _token["value"] = None
        headers["Authorization"] = f"Bearer {await _syncpay_token()}"
        r = await http.request(method, f"{SYNCPAY_BASE}{path}", headers=headers, **kw)
    r.raise_for_status()
    return r.json()


async def syncpay_balance() -> dict:
    return await _syncpay("GET", "/balance")


# ───────────────────────── Interface única ─────────────────────────
async def create_charge(currency: str, cents: int, description: str, user_id: int, return_url: str) -> tuple[str, str, str]:
    """
    Cria uma cobrança. Retorna (provider, ext_id, payload):
      - syncpay: payload = código Pix copia e cola
      - stripe:  payload = URL da página de pagamento
    """
    if currency == "brl":
        body = {"amount": round(cents / 100, 2), "description": description}
        if PUBLIC_URL:
            body["webhook_url"] = f"{PUBLIC_URL}/webhook/syncpay"
        data = await _syncpay("POST", "/cash-in", json=body)
        return "syncpay", data["identifier"], data["pix_code"]

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="payment",
        line_items=[{
            "price_data": {"currency": currency, "unit_amount": cents, "product_data": {"name": description}},
            "quantity": 1,
        }],
        client_reference_id=str(user_id),
        success_url=return_url,
        cancel_url=return_url,
    )
    return "stripe", session.id, session.url


async def charge_status(provider: str, ext_id: str) -> str:
    """Retorna 'paid', 'pending' ou 'failed'."""
    if provider == "syncpay":
        data = await _syncpay("GET", f"/transaction/{ext_id}")
        st = (data.get("data") or data).get("status", "pending")
        if st == "completed":
            return "paid"
        if st in ("failed", "refunded", "med"):
            return "failed"
        return "pending"

    if provider == "stripe":
        s = await asyncio.to_thread(stripe.checkout.Session.retrieve, ext_id)
        if s.payment_status in ("paid", "no_payment_required"):
            return "paid"
        if s.status == "expired":
            return "failed"
        return "pending"

    return "pending"


def qr_png(text: str) -> io.BytesIO:
    buf = io.BytesIO()
    qrcode.make(text, box_size=8, border=2).save(buf, format="PNG")
    buf.seek(0)
    return buf
