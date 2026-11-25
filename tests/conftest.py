import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/ticketdhaka_test")
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ.pop("REDIS_URL", None)
os.environ["ADMIN_EMAIL"] = ""  # a local .env admin must not leak into test databases

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def register(client, role="attendee", email=None):
    """Signs up through the API (always as an attendee), then promotes in the DB if needed."""
    email = email or f"{uuid.uuid4().hex[:8]}@test.dev"
    r = client.post("/auth/register", json={"email": email, "name": "T", "password": "password123"})
    assert r.status_code == 201, r.text
    if role != "attendee":
        from app.db import SessionLocal
        from app.models import Role, User

        with SessionLocal() as db:
            db.query(User).filter(User.email == email).update({User.role: Role(role)})
            db.commit()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def organizer(client):
    return register(client, "organizer")


@pytest.fixture
def attendee(client):
    return register(client)


@pytest.fixture
def event(client, organizer):
    r = client.post("/events", headers=organizer, json={
        "title": "Test Fest", "venue": "Dhaka", "starts_at": "2027-01-10T18:00:00Z",
        "rows": 2, "seats_per_row": 3, "price_cents": 1000,
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    seats = client.get(f"/events/{ev['id']}/seats").json()
    return ev, seats


def key() -> str:
    return uuid.uuid4().hex
