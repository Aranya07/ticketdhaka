"""A held seat is only booked after a successful payment (see confirm_booking)."""
from app.db import SessionLocal
from app.models import Payment, PaymentStatus
from tests.conftest import PAY, key


def _hold(client, event, headers, i=0):
    ev, seats = event
    r = client.post(f"/events/{ev['id']}/seats/{seats[i]['id']}/hold", headers={**headers, "Idempotency-Key": key()})
    assert r.status_code == 201
    return r.json()


def _payments(booking_id):
    with SessionLocal() as db:
        return db.query(Payment).filter_by(booking_id=booking_id).order_by(Payment.id).all()


def test_confirm_requires_payment_details(client, event, attendee):
    b = _hold(client, event, attendee)
    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee).status_code == 422
    bad = {"method": "bkash", "account": "12345"}
    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=bad).status_code == 422
    assert _payments(b["id"]) == []


def test_successful_payment_confirms_and_records_transaction(client, event, attendee):
    b = _hold(client, event, attendee)
    assert b["amount_cents"] == 1000 and b["transaction_id"] is None
    r = client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed" and body["transaction_id"].startswith("TXN-")

    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.succeeded and p.amount_cents == 1000
    assert p.account_last4 == "5678" and p.provider_ref == body["transaction_id"]  # full number never stored


def test_declined_payment_keeps_hold_and_allows_retry(client, event, attendee):
    b = _hold(client, event, attendee)
    declined = {"method": "card", "account": "4111 1111 1111 0000"}
    r = client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=declined)
    assert r.status_code == 402 and "declined" in r.json()["detail"].lower()

    mine = {x["id"]: x for x in client.get("/me/bookings", headers=attendee).json()}
    assert mine[b["id"]]["status"] == "held"  # the seat is still yours, try again

    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY).status_code == 200
    assert [p.status for p in _payments(b["id"])] == [PaymentStatus.failed, PaymentStatus.succeeded]


def test_confirming_twice_charges_once(client, event, attendee):
    b = _hold(client, event, attendee)
    first = client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY).json()
    second = client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY).json()
    assert first["transaction_id"] == second["transaction_id"]
    assert len(_payments(b["id"])) == 1


def test_cancelling_paid_booking_refunds(client, event, attendee):
    b = _hold(client, event, attendee)
    client.post(f"/bookings/{b['id']}/confirm", headers=attendee, json=PAY)
    assert client.post(f"/bookings/{b['id']}/cancel", headers=attendee).json()["status"] == "cancelled"
    [p] = _payments(b["id"])
    assert p.status == PaymentStatus.refunded and p.refunded_at is not None
