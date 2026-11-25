"""aamarPay hosted checkout (bKash, Nagad, Rocket, cards), used in its free sandbox.

Flow:
  1. The user holds a seat (5 minutes), then clicks Pay.
  2. POST /bookings/{id}/checkout records a *pending* payment with our own transaction ID
     (tran_id), asks aamarPay for a payment page and sends the browser there.
  3. After paying, aamarPay sends the browser back to /payments/aamarpay/return?tran_id=...
     We never trust what the browser brings back: the server asks aamarPay's
     "search transaction" API what really happened (verify() below), then
     booking_service.fulfill_aamarpay_payment() confirms the booking. It's idempotent, so a
     double redirect or a page refresh can't record the payment twice.
  4. Safety net for users who close the tab before the redirect: the expiry sweeper checks
     pending payments with aamarPay too (booking_service.reconcile_pending_payments).

aamarPay has no refund API, so a payment that can't be honoured (the hold ran out, or a paid
booking is cancelled) is marked refund_pending, and the admin refunds it from the aamarPay
merchant panel.

Only active when AAMARPAY_STORE_ID and AAMARPAY_SIGNATURE_KEY are set. Without them the app
uses the demo gateway (payment_gateway.py), so tests and CI need no aamarPay account.
This module is the only place that talks to aamarPay. Docs: https://aamarpay.readme.io
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.config import get_settings

SANDBOX_URL = "https://sandbox.aamarpay.com"
LIVE_URL = "https://secure.aamarpay.com"
TIMEOUT_SECONDS = 15


class PaymentProviderError(Exception):
    """aamarPay was unreachable or rejected the request."""


@dataclass(frozen=True)
class Verification:
    """What aamarPay's search-transaction API says about one of our transactions."""
    tran_id: str
    status: str               # "paid" | "failed" | "not_found" | "pending"
    amount_cents: int         # what was actually paid, in poisha (0 if unknown)
    currency: str
    gateway_txn_id: str       # aamarPay's own ID (pg_txnid), shown on receipts
    card_last4: str


def enabled() -> bool:
    s = get_settings()
    return bool(s.aamarpay_store_id and s.aamarpay_signature_key)


def _base_url() -> str:
    return SANDBOX_URL if get_settings().aamarpay_sandbox else LIVE_URL


def _credentials() -> dict[str, str]:
    s = get_settings()
    return {"store_id": s.aamarpay_store_id or "", "signature_key": s.aamarpay_signature_key or ""}


def _call(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode() or "{}")
    except (OSError, ValueError) as e:  # network errors, timeouts, bad JSON
        raise PaymentProviderError(f"aamarPay unreachable: {e}") from e


def taka(amount_cents: int) -> str:
    """aamarPay wants taka with 2 decimals ("1200.00"), not poisha."""
    return f"{Decimal(amount_cents) / 100:.2f}"


def _to_cents(value) -> int:
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, TypeError, ValueError):
        return 0


def create_payment(*, tran_id: str, amount_cents: int, description: str, customer_name: str,
                   customer_email: str, return_url: str) -> str:
    """Starts a payment and returns the hosted payment page URL to send the browser to."""
    body = {
        **_credentials(),
        "tran_id": tran_id,  # ours, max 32 chars, unique per attempt
        "amount": taka(amount_cents),
        "currency": "BDT",
        "desc": description,
        "cus_name": customer_name,
        "cus_email": customer_email,
        "cus_phone": "01700000000",  # required by aamarPay; we don't collect phone numbers
        "success_url": f"{return_url}&result=success",
        "fail_url": f"{return_url}&result=fail",
        "cancel_url": f"{return_url}&result=cancel",
        "type": "json",
    }
    req = urllib.request.Request(
        f"{_base_url()}/jsonpost.php", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    data = _call(req)
    if str(data.get("result")).lower() != "true" or not data.get("payment_url"):
        raise PaymentProviderError(f"aamarPay refused the payment: {data}")
    return data["payment_url"]


def verify(tran_id: str) -> Verification:
    """Asks aamarPay what happened to one of our transactions. This is the source of truth."""
    query = urllib.parse.urlencode({"request_id": tran_id, **_credentials(), "type": "json"})
    data = _call(urllib.request.Request(f"{_base_url()}/api/v1/trxcheck/request.php?{query}"))
    pay_status = str(data.get("pay_status", "")).lower()
    if pay_status == "successful":
        status = "paid"
    elif pay_status in ("failed", "expired", "cancelled", "canceled"):
        status = "failed"
    elif data.get("status") == "Invalid-Data" or not data.get("mer_txnid"):
        status = "not_found"  # the payment page was never used
    else:
        status = "pending"
    card = str(data.get("cardnumber") or "")
    return Verification(
        tran_id=tran_id, status=status,
        amount_cents=_to_cents(data.get("amount_bdt") or data.get("amount")),
        currency=str(data.get("currency_merchant") or data.get("currency") or "").upper(),
        gateway_txn_id=str(data.get("pg_txnid") or ""),
        card_last4=card[-4:] if card[-4:].isdigit() else "",
    )
