"""Stripe Checkout (test mode) for paying for a held seat.

Flow:
  1. The user holds a seat (5 minutes), then clicks Pay.
  2. POST /bookings/{id}/checkout creates a Stripe Checkout Session, and the browser goes to
     Stripe's hosted payment page. Card details only ever touch Stripe, never this server.
  3. After paying, Stripe does two things: it redirects the browser to /payments/stripe/return,
     and it POSTs a `checkout.session.completed` webhook. Both end up in
     booking_service.fulfill_stripe_payment(), which is idempotent: whichever arrives first
     confirms the booking, the other is a no-op. The webhook is the safety net for users who
     close the tab before the redirect.

Only active when STRIPE_SECRET_KEY is set. Without it the app uses the demo gateway
(payment_gateway.py), so tests, CI and local runs don't need a Stripe account.
This module is the only place that talks to the Stripe SDK.
"""
from __future__ import annotations

from dataclasses import dataclass

import stripe

from app.config import get_settings

CURRENCY = "bdt"  # Stripe amounts are in the smallest unit (poisha), same as our price_cents


class PaymentProviderError(Exception):
    """Stripe was unreachable or rejected the request."""


@dataclass(frozen=True)
class CheckoutResult:
    """What we trust about a Checkout Session, always read fresh from Stripe's API."""
    booking_id: int
    user_id: int
    paid: bool
    amount_cents: int
    currency: str
    payment_intent: str | None  # Stripe's payment ID (pi_...), our transaction ID
    card_last4: str


def enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


def webhook_enabled() -> bool:
    return enabled() and bool(get_settings().stripe_webhook_secret)


def _api_key() -> str:
    return get_settings().stripe_secret_key or ""


def create_checkout(*, booking_id: int, user_id: int, title: str, amount_cents: int, base_url: str) -> str:
    """Creates a hosted Checkout page for one seat and returns its URL."""
    try:
        session = stripe.checkout.Session.create(
            api_key=_api_key(),
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {"currency": CURRENCY, "unit_amount": amount_cents, "product_data": {"name": title}},
            }],
            client_reference_id=str(booking_id),
            metadata={"booking_id": str(booking_id), "user_id": str(user_id)},
            payment_intent_data={"metadata": {"booking_id": str(booking_id)}},
            success_url=f"{base_url}payments/stripe/return?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}?payment=cancelled",
        )
    except stripe.StripeError as e:
        raise PaymentProviderError(e.user_message or str(e)) from e
    return session.url


def retrieve_checkout(session_id: str) -> CheckoutResult:
    """Reads a Checkout Session back from Stripe. We never trust what the browser or a webhook
    body says about a payment: this API call is the source of truth."""
    try:
        s = stripe.checkout.Session.retrieve(session_id, api_key=_api_key(), expand=["payment_intent.latest_charge"])
    except stripe.StripeError as e:
        raise PaymentProviderError(e.user_message or str(e)) from e
    metadata = s.get("metadata") or {}
    intent = s.get("payment_intent")
    intent_id, last4 = None, ""
    if isinstance(intent, str):
        intent_id = intent
    elif intent:
        intent_id = intent.get("id")
        charge = intent.get("latest_charge")
        if charge and not isinstance(charge, str):
            last4 = ((charge.get("payment_method_details") or {}).get("card") or {}).get("last4") or ""
    return CheckoutResult(
        booking_id=int(metadata.get("booking_id", 0)),
        user_id=int(metadata.get("user_id", 0)),
        paid=s.get("payment_status") == "paid",
        amount_cents=s.get("amount_total") or 0,
        currency=(s.get("currency") or "").lower(),
        payment_intent=intent_id,
        card_last4=last4,
    )


def checkout_session_from_webhook(payload: bytes, signature: str) -> str | None:
    """Checks the webhook really came from Stripe (signed with our webhook secret) and returns the
    Checkout Session ID for a completed payment, or None for event types we don't handle.
    Raises ValueError if the signature is wrong."""
    try:
        event = stripe.Webhook.construct_event(payload, signature, get_settings().stripe_webhook_secret or "")
    except (ValueError, stripe.SignatureVerificationError) as e:
        raise ValueError("invalid Stripe webhook") from e
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return event["data"]["object"]["id"]
    return None


def refund(payment_intent_id: str) -> None:
    """Refunds a payment in full. The idempotency key makes a retry (e.g. Stripe re-sending a
    webhook) safe: Stripe returns the first refund instead of refunding twice."""
    try:
        stripe.Refund.create(api_key=_api_key(), payment_intent=payment_intent_id,
                             idempotency_key=f"refund-{payment_intent_id}")
    except stripe.StripeError as e:
        raise PaymentProviderError(e.user_message or str(e)) from e
