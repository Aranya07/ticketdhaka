"""aamarPay checkout flow. aamarPay's API is replaced by FakeAamarPay, so these run offline (and in CI).
The last two tests run the real request-building / response-parsing code on aamarPay-shaped data."""
import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import Booking, BookingStatus, Payment, PaymentMethod, PaymentStatus, Seat
from app.services import aamarpay
from app.services.aamarpay import PaymentProviderError, Verification
from app.services.booking_service import reconcile_pending_payments, release_expired_holds
from tests.conftest import PAY, key


class FakeAamarPay:
    """Stands in for aamarPay's two API calls: start a payment, and check one."""

    def __init__(self):
        self.created: list[dict] = []
        self.results: dict[str, Verification] = {}
        self.down = False

    def create_payment(self, **kwargs):
        if self.down:
            raise PaymentProviderError("aamarPay unreachable")
        self.created.append(kwargs)
        return f"https://sandbox.aamarpay.test/paynow.php?track={kwargs['tran_id']}"

    def set(self, tran_id, status, amount_cents=None):
        amount = amount_cents if amount_cents is not None else self.created[-1]["amount_cents"]
        self.results[tran_id] = Verification(tran_id, status, amount, "BDT", "AAM123", "1111")

    def verify(self, tran_id):
        return self.results.get(tran_id, Verification(tran_id, "not_found", 0, "", "", ""))


@pytest.fixture
def fake_aamarpay(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "aamarpay_store_id", "aamarpaytest")
    monkeypatch.setattr(settings, "aamarpay_signature_key", "test-key")
    fake = FakeAamarPay()
    monkeypatch.setattr(aamarpay, "create_payment", fake.create_payment)
    monkeypatch.setattr(aamarpay, "verify", fake.verify)
    return fake


def _checkout(client, event, headers, i=0):
    """Holds seat i and starts an aamarPay checkout. Returns (booking, tran_id)."""
    ev, seats = event
    b = client.post(f"/events/{ev['id']}/seats/{seats[i]['id']}/hold", headers={**headers, "Idempotency-Key": key()}).json()
    r = client.post(f"/bookings/{b['id']}/checkout", headers=headers)
    assert r.status_code == 200, r.text
    return b, r.json()["payment_url"].rsplit("track=", 1)[1]


def _return(client, tran_id):
    r = client.get(f"/payments/aamarpay/return?tran_id={tran_id}&result=success", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def _booking(client, headers, booking_id):
    return {b["id"]: b for b in client.get("/me/bookings", headers=headers).json()}[booking_id]


def _payments(booking_id):
    with SessionLocal() as db:
        return db.query(Payment).filter_by(booking_id=booking_id).order_by(Payment.id).all()


def _run_out_hold(seat_id, sweep):
    with SessionLocal() as db:
        db.get(Seat, seat_id).hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        if sweep:
            release_expired_holds(db)


def test_demo_gateway_when_aamarpay_not_configured(client, event, attendee):
    assert client.get("/payments/config").json()["provider"] == "demo"
    ev, seats = event
    b = client.post(f"/events/{ev['id']}/seats/{seats[0]['id']}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    assert client.post(f"/bookings/{b['id']}/checkout", headers=attendee).status_code == 409


def test_checkout_records_pending_payment_and_turns_off_demo(client, event, attendee, fake_aamarpay):
    assert client.get("/payments/config").json()["provider"] == "aamarpay"
    b, tran_id = _checkout(client, event, attendee)
    [created] = fake_aamarpay.created
    assert created["tran_id"] == tran_id and len(tran_id) <= 32 and created["amount_cents"] == 1000
    assert "Test Fest" in created["description"] and f"tran_id={tran_id}" in created["return_url"]
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.pending and p.method == PaymentMethod.aamarpay and p.provider_ref == tran_id
    # a made-up bKash number can't skip the real gateway any more
    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY).status_code == 409


def test_paying_confirms_booking_once(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)
    fake_aamarpay.set(tran_id, "paid")
    assert _return(client, tran_id) == "/?payment=success"
    mine = _booking(client, attendee, b["id"])
    assert mine["status"] == "confirmed" and mine["transaction_id"] == tran_id
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.succeeded and p.account_last4 == "1111"

    assert _return(client, tran_id) == "/?payment=success"  # refresh / second redirect: no-op
    assert [p.status for p in _payments(b["id"])] == [PaymentStatus.succeeded]


def test_failed_payment_keeps_hold_and_allows_retry(client, event, attendee, fake_aamarpay):
    b, first = _checkout(client, event, attendee)
    fake_aamarpay.set(first, "failed")
    assert _return(client, first) == "/?payment=failed"
    assert _booking(client, attendee, b["id"])["status"] == "held"

    second = client.post(f"/bookings/{b['id']}/checkout", headers=attendee).json()["payment_url"].rsplit("=", 1)[1]
    fake_aamarpay.set(second, "paid")
    assert _return(client, second) == "/?payment=success"
    assert [p.status for p in _payments(b["id"])] == [PaymentStatus.failed, PaymentStatus.succeeded]


def test_forged_return_confirms_nothing(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)  # aamarPay says: nothing paid
    assert _return(client, tran_id) == "/?payment=pending"
    assert _return(client, "TD999-madeup") == "/?payment=pending"
    assert _booking(client, attendee, b["id"])["status"] == "held"


def test_payment_after_hold_was_lost_needs_refund(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)
    _run_out_hold(b["seat_id"], sweep=True)
    fake_aamarpay.set(tran_id, "paid")
    assert _return(client, tran_id) == "/?payment=refund"
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.refund_pending and "no longer held" in p.failure_reason


def test_paid_just_after_hold_time_still_gets_the_seat(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)
    _run_out_hold(b["seat_id"], sweep=False)  # time is up, but nobody took the seat yet
    fake_aamarpay.set(tran_id, "paid")
    assert _return(client, tran_id) == "/?payment=success"


def test_wrong_amount_needs_refund(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)
    fake_aamarpay.set(tran_id, "paid", amount_cents=100)
    assert _return(client, tran_id) == "/?payment=refund"
    with SessionLocal() as db:
        assert db.get(Booking, b["id"]).status == BookingStatus.held


def test_cancelling_paid_booking_queues_refund(client, event, attendee, fake_aamarpay):
    b, tran_id = _checkout(client, event, attendee)
    fake_aamarpay.set(tran_id, "paid")
    _return(client, tran_id)
    assert client.post(f"/bookings/{b['id']}/cancel", headers=attendee).json()["status"] == "cancelled"
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.refund_pending  # aamarPay has no refund API: the admin refunds it


def test_sweeper_confirms_payment_when_user_closed_the_tab(client, event, attendee, fake_aamarpay):
    paid, paid_tran = _checkout(client, event, attendee, 0)
    fake_aamarpay.set(paid_tran, "paid")
    abandoned, _ = _checkout(client, event, attendee, 1)  # opened the payment page, never paid
    _run_out_hold(abandoned["seat_id"], sweep=True)

    with SessionLocal() as db:
        outcomes = {b.id: o for b, o in reconcile_pending_payments(db, fake_aamarpay.verify, older_than_seconds=0)}
    assert outcomes == {paid["id"]: "confirmed", abandoned["id"]: "failed"}
    assert _booking(client, attendee, paid["id"])["status"] == "confirmed"
    assert [p.status for p in _payments(abandoned["id"])] == [PaymentStatus.failed]


def test_gateway_down_gives_502_and_no_stuck_payment(client, event, attendee, fake_aamarpay):
    fake_aamarpay.down = True
    ev, seats = event
    b = client.post(f"/events/{ev['id']}/seats/{seats[0]['id']}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    assert client.post(f"/bookings/{b['id']}/checkout", headers=attendee).status_code == 502
    assert [p.status for p in _payments(b["id"])] == [PaymentStatus.failed]


def test_create_payment_sends_what_aamarpay_expects(monkeypatch):
    monkeypatch.setattr(get_settings(), "aamarpay_store_id", "aamarpaytest")
    monkeypatch.setattr(get_settings(), "aamarpay_signature_key", "sig")
    sent = {}

    def fake_call(req):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return {"result": "true", "payment_url": "https://sandbox.aamarpay.com/paynow.php?track=AAM1"}

    monkeypatch.setattr(aamarpay, "_call", fake_call)
    url = aamarpay.create_payment(tran_id="TD1-abc", amount_cents=120000, description="Gig: seat A1",
                                  customer_name="Rafi", customer_email="rafi@test.dev",
                                  return_url="http://localhost:8000/payments/aamarpay/return?tran_id=TD1-abc")
    assert url.endswith("track=AAM1") and sent["url"] == "https://sandbox.aamarpay.com/jsonpost.php"
    body = sent["body"]
    assert body["amount"] == "1200.00" and body["currency"] == "BDT" and body["type"] == "json"  # taka, not poisha
    assert body["store_id"] == "aamarpaytest" and body["tran_id"] == "TD1-abc"
    assert body["success_url"].endswith("tran_id=TD1-abc&result=success")
    assert body["fail_url"].endswith("&result=fail") and body["cancel_url"].endswith("&result=cancel")

    monkeypatch.setattr(aamarpay, "_call", lambda req: {"result": "false", "message": "Invalid Store ID"})
    with pytest.raises(PaymentProviderError):
        aamarpay.create_payment(tran_id="x", amount_cents=1, description="d", customer_name="n",
                                customer_email="e@x.dev", return_url="http://x/?tran_id=x")


def test_verify_reads_aamarpays_answer(monkeypatch):
    monkeypatch.setattr(get_settings(), "aamarpay_store_id", "aamarpaytest")
    monkeypatch.setattr(get_settings(), "aamarpay_signature_key", "sig")
    answers = {
        "TD1-paid": {"mer_txnid": "TD1-paid", "pay_status": "Successful", "status_code": "2", "amount": "1200.00",
                     "amount_bdt": "1200.00", "currency_merchant": "BDT", "pg_txnid": "AAM1", "cardnumber": "411111******1111"},
        "TD2-failed": {"mer_txnid": "TD2-failed", "pay_status": "Failed", "status_code": "7", "amount": "1200.00"},
        "TD3-none": {"request_id": "TD3-none", "store_id": "aamarpaytest", "status": "Invalid-Data"},
    }
    asked = []

    def fake_call(req):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        asked.append(q)
        return answers[q["request_id"][0]]

    monkeypatch.setattr(aamarpay, "_call", fake_call)
    paid = aamarpay.verify("TD1-paid")
    assert (paid.status, paid.amount_cents, paid.currency, paid.gateway_txn_id, paid.card_last4) == \
        ("paid", 120000, "BDT", "AAM1", "1111")
    assert aamarpay.verify("TD2-failed").status == "failed"
    assert aamarpay.verify("TD3-none").status == "not_found"
    assert asked[0]["type"] == ["json"] and asked[0]["store_id"] == ["aamarpaytest"]
