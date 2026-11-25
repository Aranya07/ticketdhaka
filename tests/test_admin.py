from app.db import SessionLocal
from app.models import Role, User
from app.seed import ensure_admin
from tests.conftest import key, register


def test_cannot_self_register_as_organizer_or_admin(client):
    r = client.post("/auth/register", json={"email": "sneaky@x.dev", "name": "S", "password": "password123", "role": "admin"})
    assert r.status_code == 201
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["role"] == "attendee"  # the extra field is ignored


def test_admin_routes_are_admin_only(client, attendee, organizer):
    for headers in (attendee, organizer):
        assert client.get("/admin/stats", headers=headers).status_code == 403
    assert client.get("/admin/stats").status_code == 401


def test_ensure_admin_creates_and_promotes(client):
    with SessionLocal() as db:
        ensure_admin(db, "Owner@Example.com", "strong-password-1")
        assert db.query(User).filter_by(email="owner@example.com").one().role == Role.admin
    r = client.post("/auth/login", json={"email": "owner@example.com", "password": "strong-password-1"})
    assert r.status_code == 200


def test_admin_stats_bookings_release_and_roles(client, event, attendee):
    ev, seats = event
    admin = register(client, "admin")
    hold = client.post(f"/events/{ev['id']}/seats/{seats[0]['id']}/hold", headers={**attendee, "Idempotency-Key": key()}).json()
    client.post(f"/bookings/{hold['id']}/confirm", headers=attendee)

    stats = client.get("/admin/stats", headers=admin).json()
    assert stats["bookings"]["confirmed"] == 1 and stats["seats"]["booked"] == 1
    assert stats["revenue_bdt"] == 10.0

    rows = client.get("/admin/bookings?status=confirmed", headers=admin).json()
    assert rows[0]["seat_id"] == seats[0]["id"] and rows[0]["event_title"] == "Test Fest"

    r = client.post(f"/admin/seats/{seats[0]['id']}/release", headers=admin)
    assert r.json()["bookings_cancelled"] == 1
    seat_status = {s["id"]: s["status"] for s in client.get(f"/events/{ev['id']}/seats").json()}
    assert seat_status[seats[0]["id"]] == "available"

    users = client.get("/admin/users", headers=admin).json()
    target = next(u for u in users if u["role"] == "attendee")
    promoted = client.patch(f"/admin/users/{target['id']}/role", headers=admin, json={"role": "organizer"})
    assert promoted.json()["role"] == "organizer"
    me = client.get("/auth/me", headers=admin).json()
    assert client.patch(f"/admin/users/{me['id']}/role", headers=admin, json={"role": "attendee"}).status_code == 400


def test_admin_can_delete_event(client, event, attendee):
    ev, seats = event
    admin = register(client, "admin")
    client.post(f"/events/{ev['id']}/seats/{seats[0]['id']}/hold", headers={**attendee, "Idempotency-Key": key()})
    assert client.delete(f"/admin/events/{ev['id']}", headers=admin).status_code == 204
    assert client.get("/events").json()["total"] == 0


def test_admin_can_create_events(client):
    admin = register(client, "admin")
    r = client.post("/events", headers=admin, json={
        "title": "Admin Event", "venue": "V", "starts_at": "2027-01-01T00:00:00Z",
        "rows": 1, "seats_per_row": 2, "price_cents": 0,
    })
    assert r.status_code == 201
