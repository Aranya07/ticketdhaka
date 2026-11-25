"""Booking flow: hold -> confirm (or cancel / expire).

Concurrency strategy (pessimistic locking):
  Every state change to a seat starts with `SELECT ... FOR UPDATE` on that
  seat's row. Postgres lets only one transaction hold the row lock at a time;
  the others wait. Whoever gets the lock first sees the seat "available",
  changes it, and commits. Everyone after that sees "held" and gets a 409.
  The result is that 200 concurrent requests for one seat produce exactly one booking.

Idempotency:
  The client sends an `Idempotency-Key` header. We check for an existing
  booking with that key *after* taking the seat lock, so two retries of the same
  request are serialized and the second just gets back the first's booking.
  A UNIQUE(user_id, idempotency_key) constraint is the final safety net.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.waitlist import PriorityWaitlist, WaitlistItem
from app.models import Booking, BookingStatus, Seat, SeatStatus, User, WaitlistEntry

settings = get_settings()


class BookingError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class HoldResult:
    booking: Booking
    created: bool  # False = idempotent replay of an earlier request


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_seat(db: Session, seat_id: int, event_id: int | None = None) -> Seat:
    q = select(Seat).where(Seat.id == seat_id)
    if event_id is not None:
        q = q.where(Seat.event_id == event_id)
    seat = db.execute(q.with_for_update()).scalar_one_or_none()
    if seat is None:
        raise BookingError(404, "Seat not found")
    return seat


def _expire_current_hold(db: Session, seat: Seat) -> None:
    """The seat's hold has run out: mark its booking expired and free the seat."""
    db.query(Booking).filter(
        Booking.seat_id == seat.id, Booking.status == BookingStatus.held
    ).update({Booking.status: BookingStatus.expired}, synchronize_session=False)
    seat.status = SeatStatus.available
    seat.held_by = None
    seat.hold_expires_at = None


def hold_seat(db: Session, user: User, event_id: int, seat_id: int, idempotency_key: str) -> HoldResult:
    try:
        seat = _lock_seat(db, seat_id, event_id)  # <- other requests for this seat wait here

        existing = db.execute(
            select(Booking).where(Booking.user_id == user.id, Booking.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            db.rollback()
            if existing.seat_id != seat_id:
                raise BookingError(422, "Idempotency-Key already used for a different seat")
            return HoldResult(existing, created=False)

        now = _now()
        if seat.status == SeatStatus.booked:
            raise BookingError(409, "Seat already booked")
        if seat.status == SeatStatus.held:
            if seat.hold_expires_at and seat.hold_expires_at > now:
                raise BookingError(409, "Seat is currently held by someone else")
            _expire_current_hold(db, seat)  # stale hold that the sweeper hasn't reached yet

        seat.status = SeatStatus.held
        seat.held_by = user.id
        seat.hold_expires_at = now + timedelta(minutes=settings.hold_minutes)
        booking = Booking(user_id=user.id, seat_id=seat.id, idempotency_key=idempotency_key)
        db.add(booking)
        db.commit()
        return HoldResult(booking, created=True)
    except BookingError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(Booking).where(Booking.user_id == user.id, Booking.idempotency_key == idempotency_key)
        ).scalar_one()
        return HoldResult(existing, created=False)


def _lock_booking(db: Session, booking_id: int, user: User) -> Booking:
    booking = db.execute(
        select(Booking).where(Booking.id == booking_id).with_for_update()
    ).scalar_one_or_none()
    if booking is None or booking.user_id != user.id:
        raise BookingError(404, "Booking not found")
    return booking


def confirm_booking(db: Session, user: User, booking_id: int) -> Booking:
    """Mock payment, then turn the hold into a confirmed booking."""
    try:
        booking = _lock_booking(db, booking_id, user)
        if booking.status == BookingStatus.confirmed:
            db.rollback()
            return booking  # confirming twice is a no-op
        if booking.status != BookingStatus.held:
            raise BookingError(409, f"Booking is {booking.status.value}")

        seat = _lock_seat(db, booking.seat_id)
        if seat.hold_expires_at is None or seat.hold_expires_at <= _now():
            _expire_current_hold(db, seat)
            db.commit()
            raise BookingError(410, "Hold expired, please pick the seat again")

        # A real system would charge a payment provider here (with its own idempotency key).
        seat.status = SeatStatus.booked
        seat.hold_expires_at = None
        booking.status = BookingStatus.confirmed
        booking.confirmed_at = _now()
        db.commit()
        return booking
    except BookingError:
        if db.in_transaction():
            db.rollback()
        raise


def cancel_booking(db: Session, user: User, booking_id: int) -> tuple[Booking, int | None]:
    """Cancels a booking and frees the seat. If anyone is on the waitlist, the
    seat goes straight to them as a new hold. Returns (booking, promoted_user_id)."""
    try:
        booking = _lock_booking(db, booking_id, user)
        if booking.status not in (BookingStatus.held, BookingStatus.confirmed):
            raise BookingError(409, f"Booking is {booking.status.value}")
        seat = _lock_seat(db, booking.seat_id)
        booking.status = BookingStatus.cancelled
        seat.status = SeatStatus.available
        seat.held_by = None
        seat.hold_expires_at = None
        promoted = _promote_from_waitlist(db, seat)
        db.commit()
        return booking, promoted
    except BookingError:
        db.rollback()
        raise


def _promote_from_waitlist(db: Session, seat: Seat) -> int | None:
    """Pops the next person off the priority waitlist and gives them a hold on the
    freed seat. Must be called while the seat row is locked."""
    entries = db.execute(
        select(WaitlistEntry).where(WaitlistEntry.event_id == seat.event_id).with_for_update(skip_locked=True)
    ).scalars().all()
    if not entries:
        return None
    heap = PriorityWaitlist(
        [WaitlistItem(e.priority, e.joined_at, user_id=e.user_id, entry_id=e.id) for e in entries]
    )
    nxt = heap.pop()
    assert nxt is not None
    seat.status = SeatStatus.held
    seat.held_by = nxt.user_id
    seat.hold_expires_at = _now() + timedelta(minutes=settings.hold_minutes)
    db.add(Booking(user_id=nxt.user_id, seat_id=seat.id, idempotency_key=f"waitlist-{nxt.entry_id}"))
    db.query(WaitlistEntry).filter(WaitlistEntry.id == nxt.entry_id).delete()
    return nxt.user_id


def join_waitlist(db: Session, user: User, event_id: int, priority: int = 1) -> WaitlistEntry:
    available = db.execute(
        select(Seat.id).where(Seat.event_id == event_id, Seat.status == SeatStatus.available).limit(1)
    ).first()
    if available:
        raise BookingError(409, "Seats are still available, book one directly")
    entry = WaitlistEntry(event_id=event_id, user_id=user.id, priority=priority)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise BookingError(409, "Already on the waitlist")
    return entry


def release_expired_holds(db: Session, batch_size: int = 500) -> set[int]:
    """Background sweeper. SKIP LOCKED means it never blocks a user request:
    seats that someone is booking right now get picked up on the next sweep.
    Returns the event ids that changed, so their caches can be invalidated."""
    seats = db.execute(
        select(Seat)
        .where(Seat.status == SeatStatus.held, Seat.hold_expires_at < _now())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    changed: set[int] = set()
    for seat in seats:
        _expire_current_hold(db, seat)
        _promote_from_waitlist(db, seat)
        changed.add(seat.event_id)
    db.commit()
    return changed
