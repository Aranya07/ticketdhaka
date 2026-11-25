from datetime import UTC, datetime, timedelta

from app.db import SessionLocal
from app.models import Seat
from app.services.booking_service import release_expired_holds
from tests.conftest import key, register


def test_register_login_me(client):
    r = client.post("/auth/register", json={"email": "a@b.dev", "name": "A", "password": "password123"})
    assert r.status_code == 201
    assert client.post("/auth/register", json={"email": "a@b.dev", "name": "A", "password": "password123"}).status_code == 409
    r = client.post("/auth/login", json={"email": "a@b.dev", "password": "password123"})
    tokens = r.json()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["email"] == "a@b.dev"
    # refresh tokens can't be used as access tokens
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 200
    assert client.post("/auth/login", json={"email": "a@b.dev", "password": "wrong-pass"}).status_code == 401


def test_only_organizers_create_events(client, attendee):
    r = client.post("/events", headers=attendee, json={
        "title": "X", "venue": "Y", "starts_at": "2027-01-01T00:00:00Z", "rows": 1, "seats_per_row": 1, "price_cents": 0,
    })
    assert r.status_code == 403


def test_list_events_counts_and_pagination(client, event):
    page = client.get("/events?limit=10").json()
    assert page["total"] == 1
    assert page["items"][0]["total_seats"] == 6
    assert page["items"][0]["available_seats"] == 6


def test_hold_confirm_flow(client, event, attendee):
    ev, seats = event
    seat = seats[0]
    r = client.post(f"/events/{ev['id']}/seats/{seat['id']}/hold", headers={**attendee, "Idempotency-Key": key()})
    assert r.status_code == 201
    booking = r.json()
    assert booking["status"] == "held" and booking["hold_expires_at"]

    # the cache must be invalidated, so the seat now shows as held
    status = {s["id"]: s["status"] for s in client.get(f"/events/{ev['id']}/seats").json()}
    assert status[seat["id"]] == "held"

    r = client.post(f"/bookings/{booking['id']}/confirm", headers=attendee)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert client.get("/events").json()["items"][0]["available_seats"] == 5


def test_cannot_hold_taken_seat(client, event, attendee):
    ev, seats = event
    other = register(client)
    url = f"/events/{ev['id']}/seats/{seats[0]['id']}/hold"
    assert client.post(url, headers={**attendee, "Idempotency-Key": key()}).status_code == 201
    assert client.post(url, headers={**other, "Idempotency-Key": key()}).status_code == 409


def test_idempotent_retry_returns_same_booking(client, event, attendee):
    ev, seats = event
    k = key()
    url = f"/events/{ev['id']}/seats/{seats[1]['id']}/hold"
    first = client.post(url, headers={**attendee, "Idempotency-Key": k})
    second = client.post(url, headers={**attendee, "Idempotency-Key": k})
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_expired_hold_is_released_and_confirm_fails(client, event, attendee):
    ev, seats = event
    seat_id = seats[2]["id"]
    b = client.post(f"/events/{ev['id']}/seats/{seat_id}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    with SessionLocal() as db:  # fast-forward time: push the hold into the past
        db.get(Seat, seat_id).hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert client.post(f"/bookings/{b['id']}/confirm", headers=attendee).status_code == 410

    b2 = client.post(f"/events/{ev['id']}/seats/{seat_id}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    with SessionLocal() as db:
        db.get(Seat, seat_id).hold_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        changed = release_expired_holds(db)
    assert changed == {ev["id"]}
    mine = {x["id"]: x["status"] for x in client.get("/me/bookings", headers=attendee).json()}
    assert mine[b2["id"]] == "expired"


def test_waitlist_gets_seat_on_cancel(client, organizer):
    ev = client.post("/events", headers=organizer, json={
        "title": "Tiny", "venue": "V", "starts_at": "2027-01-01T00:00:00Z", "rows": 1, "seats_per_row": 1, "price_cents": 0,
    }).json()
    seat = client.get(f"/events/{ev['id']}/seats").json()[0]
    a, b = register(client), register(client)
    booking = client.post(f"/events/{ev['id']}/seats/{seat['id']}/hold", headers={**a, "Idempotency-Key": key()}).json()
    assert client.post(f"/events/{ev['id']}/waitlist", headers=b).status_code == 201
    assert client.post(f"/events/{ev['id']}/waitlist", headers=b).status_code == 409  # no duplicates

    client.post(f"/bookings/{booking['id']}/cancel", headers=a)
    b_bookings = client.get("/me/bookings", headers=b).json()
    assert len(b_bookings) == 1 and b_bookings[0]["status"] == "held"


def test_waitlist_rejected_when_seats_available(client, event, attendee):
    ev, _ = event
    assert client.post(f"/events/{ev['id']}/waitlist", headers=attendee).status_code == 409


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["db"] is True
