"""The headline test: N users race for ONE seat at the same time.

Each thread uses its own DB session (its own connection and transaction),
exactly like separate API requests in production. Without the row lock this
test fails: several threads read "available" before any of them commits.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Booking, BookingStatus, Role, Seat, SeatStatus, User
from app.schemas import EventCreate
from app.services.booking_service import BookingError, hold_seat
from app.services.event_service import create_event

N = 200


def _setup() -> tuple[int, int, list[int]]:
    with SessionLocal() as db:
        org = User(email="org@x.dev", name="o", hashed_password="x", role=Role.organizer)
        users = [User(email=f"u{i}@x.dev", name="u", hashed_password="x") for i in range(N)]
        db.add_all([org, *users])
        db.commit()
        ev = create_event(db, org, EventCreate(
            title="Hot", venue="V", starts_at="2027-01-01T00:00:00Z", rows=1, seats_per_row=1, price_cents=0,
        ))
        seat_id = db.scalar(select(Seat.id).where(Seat.event_id == ev.id))
        return ev.id, seat_id, [u.id for u in users]


def test_200_concurrent_holds_on_one_seat_yield_exactly_one_booking():
    event_id, seat_id, user_ids = _setup()
    barrier = Barrier(25)  # release threads in waves so they genuinely collide

    def attempt(uid: int) -> str:
        with SessionLocal() as db:
            user = db.get(User, uid)
            try:
                barrier.wait(timeout=5)
            except Exception:
                pass
            try:
                hold_seat(db, user, event_id, seat_id, idempotency_key=f"k-{uid}")
                return "won"
            except BookingError as e:
                return str(e.status_code)

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(attempt, user_ids))

    assert results.count("won") == 1
    assert results.count("409") == N - 1
    with SessionLocal() as db:
        assert db.get(Seat, seat_id).status == SeatStatus.held
        held = db.scalar(select(func.count(Booking.id)).where(Booking.status == BookingStatus.held))
        assert held == 1


def test_concurrent_retries_with_same_key_create_one_booking():
    event_id, seat_id, user_ids = _setup()
    uid = user_ids[0]

    def attempt(_: int) -> int:
        with SessionLocal() as db:
            return hold_seat(db, db.get(User, uid), event_id, seat_id, idempotency_key="same-key").booking.id

    with ThreadPoolExecutor(max_workers=20) as pool:
        ids = set(pool.map(attempt, range(40)))
    assert len(ids) == 1
