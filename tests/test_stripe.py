"""Stripe Checkout flow. Stripe's API is replaced by FakeStripe, so these run offline (and in CI);
webhook signatures are still checked by the real Stripe SDK, with a test secret."""
import dataclasses
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
import stripe

from app.config import get_settings
from app.db import SessionLocal
from app.models import Booking, BookingStatus, Payment, PaymentMethod, PaymentStatus, Seat
from app.services import stripe_payments
from app.services.booking_service import release_expired_holds
from app.services.stripe_payments import CheckoutResult
from tests.conftest import PAY, key

WEBHOOK_SECRET = "whsec_test_secret"


class FakeStripe:
    """Stands in for the three Stripe API calls the app makes."""

    def __init__(self):
        self.sessions: dict[str, CheckoutResult] = {}
        self.created: list[dict] = []
        self.refunds: list[str] = []

    def create_checkout(self, *, booking_id, user_id, title, amount_cents, base_url):
        sid = f"cs_test_{len(self.created)}"
        self.created.append({"booking_id": booking_id, "amount_cents": amount_cents, "title": title, "base_url": base_url})
        self.sessions[sid] = CheckoutResult(booking_id, user_id, paid=False, amount_cents=amount_cents,
                                            currency="bdt", payment_intent=None, card_last4="")
        return f"https://checkout.stripe.test/{sid}"

    def pay(self, sid, amount_cents=None):
        s = self.sessions[sid]
        self.sessions[sid] = dataclasses.replace(s, paid=True, payment_intent=f"pi_{sid}", card_last4="4242",
                                                 amount_cents=amount_cents or s.amount_cents)
        return f"pi_{sid}"

    def retrieve_checkout(self, sid):
        return self.sessions[sid]

    def refund(self, payment_intent_id):
        self.refunds.append(payment_intent_id)


@pytest.fixture
def fake_stripe(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    fake = FakeStripe()
    for name in ("create_checkout", "retrieve_checkout", "refund"):
        monkeypatch.setattr(stripe_payments, name, getattr(fake, name))
    return fake


def _checkout(client, event, headers, i=0):
    """Holds seat i and starts a Stripe checkout for it. Returns (booking, session_id)."""
    ev, seats = event
    b = client.post(f"/events/{ev['id']}/seats/{seats[i]['id']}/hold", headers={**headers, "Idempotency-Key": key()}).json()
    r = client.post(f"/bookings/{b['id']}/checkout", headers=headers)
    assert r.status_code == 200, r.text
    return b, r.json()["checkout_url"].rsplit("/", 1)[1]


def _booking(client, headers, booking_id):
    return {b["id"]: b for b in client.get("/me/bookings", headers=headers).json()}[booking_id]


def _payments(booking_id):
    with SessionLocal() as db:
        return db.query(Payment).filter_by(booking_id=booking_id).order_by(Payment.id).all()


def _signed_webhook(client, session_id, secret=WEBHOOK_SECRET):
    payload = json.dumps({"id": "evt_test", "object": "event", "type": "checkout.session.completed",
                          "data": {"object": {"id": session_id, "object": "checkout.session"}}})
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/payments/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})


def test_demo_gateway_when_stripe_not_configured(client, event, attendee):
    assert client.get("/payments/config").json()["provider"] == "demo"
    ev, seats = event
    b = client.post(f"/events/{ev['id']}/seats/{seats[0]['id']}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    assert client.post(f"/bookings/{b['id']}/checkout", headers=attendee).status_code == 409
    assert client.post("/payments/stripe/webhook", content=b"{}").status_code == 503


def test_checkout_uses_seat_price_and_turns_off_demo_payments(client, event, attendee, fake_stripe):
    assert client.get("/payments/config").json()["provider"] == "stripe"
    b, _ = _checkout(client, event, attendee)
    [created] = fake_stripe.created
    assert created["booking_id"] == b["id"] and created["amount_cents"] == 1000
    assert "Test Fest" in created["title"] and created["base_url"].startswith("http")
    # the made-up bKash payment can't bypass Stripe any more
    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY).status_code == 409


def test_paying_confirms_booking_once(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    pi = fake_stripe.pay(sid)
    r = client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?payment=success"

    mine = _booking(client, attendee, b["id"])
    assert mine["status"] == "confirmed" and mine["transaction_id"] == pi
    [p] = _payments(b["id"])
    assert p.method == PaymentMethod.stripe and p.status == PaymentStatus.succeeded
    assert p.amount_cents == 1000 and p.account_last4 == "4242"

    # the webhook for the same payment arrives afterwards: no second payment row
    assert _signed_webhook(client, sid).status_code == 200
    assert len(_payments(b["id"])) == 1


def test_webhook_alone_confirms_and_checks_signature(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    fake_stripe.pay(sid)
    assert _signed_webhook(client, sid, secret="whsec_wrong").status_code == 400
    assert _booking(client, attendee, b["id"])["status"] == "held"  # forged webhook changed nothing

    assert _signed_webhook(client, sid).status_code == 200  # user closed the tab: the webhook finishes it
    assert _booking(client, attendee, b["id"])["status"] == "confirmed"


def test_unpaid_session_does_not_confirm(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    r = client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert r.headers["location"] == "/?payment=pending"
    assert _booking(client, attendee, b["id"])["status"] == "held" and _payments(b["id"]) == []


def test_payment_after_hold_was_lost_is_refunded(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    with SessionLocal() as db:  # the 5-minute hold runs out and the sweeper frees the seat
        db.get(Seat, b["seat_id"]).hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        release_expired_holds(db)
    pi = fake_stripe.pay(sid)

    r = client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert r.headers["location"] == "/?payment=refunded"
    assert fake_stripe.refunds == [pi]
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.refunded and "no longer held" in p.failure_reason


def test_paid_just_after_hold_time_still_gets_the_seat(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    with SessionLocal() as db:  # time is up, but the sweeper hasn't run and nobody took the seat
        db.get(Seat, b["seat_id"]).hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    fake_stripe.pay(sid)
    client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert _booking(client, attendee, b["id"])["status"] == "confirmed" and fake_stripe.refunds == []


def test_wrong_amount_is_refunded(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    pi = fake_stripe.pay(sid, amount_cents=1)
    r = client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert r.headers["location"] == "/?payment=refunded" and fake_stripe.refunds == [pi]
    with SessionLocal() as db:
        assert db.get(Booking, b["id"]).status == BookingStatus.held


def test_cancelling_stripe_booking_refunds_through_stripe(client, event, attendee, fake_stripe):
    b, sid = _checkout(client, event, attendee)
    pi = fake_stripe.pay(sid)
    client.get(f"/payments/stripe/return?session_id={sid}", follow_redirects=False)
    assert client.post(f"/bookings/{b['id']}/cancel", headers=attendee).json()["status"] == "cancelled"
    assert fake_stripe.refunds == [pi]
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.refunded


def test_retrieve_checkout_reads_the_stripe_session(monkeypatch):
    """The real parser, fed a Session shaped like Stripe's API response."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_fake")
    session = stripe.checkout.Session.construct_from({
        "id": "cs_test_1", "object": "checkout.session", "payment_status": "paid",
        "amount_total": 50000, "currency": "bdt", "metadata": {"booking_id": "7", "user_id": "3"},
        "payment_intent": {"id": "pi_123", "object": "payment_intent", "latest_charge": {
            "id": "ch_1", "object": "charge", "payment_method_details": {"card": {"last4": "4242"}}}},
    }, "sk_test_fake")
    calls = {}

    def fake_retrieve(sid, **kwargs):
        calls.update(kwargs, sid=sid)
        return session

    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake_retrieve)
    r = stripe_payments.retrieve_checkout("cs_test_1")
    assert (r.booking_id, r.user_id, r.paid, r.amount_cents) == (7, 3, True, 50000)
    assert (r.payment_intent, r.card_last4, r.currency) == ("pi_123", "4242", "bdt")
    assert calls["sid"] == "cs_test_1" and "payment_intent.latest_charge" in calls["expand"]
