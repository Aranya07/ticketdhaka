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

Payment (the transaction requirement):
  A hold only becomes "confirmed" after a successful payment. The payment row,
  the booking status and the seat status are written in ONE database transaction
  while the seat row is locked, so you can never end up with a confirmed booking
  that has no payment (or the other way round). Double-clicking "Pay" can't charge
  twice: the second request waits on the booking lock, then sees "confirmed" and
  returns without calling the gateway.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.waitlist import PriorityWaitlist, WaitlistItem
from app.models import (
    Booking,
    BookingStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Seat,
    SeatStatus,
    User,
    WaitlistEntry,
)
from app.services import aamarpay, payment_gateway
from app.services.aamarpay import PaymentProviderError, Verification

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


def confirm_booking(db: Session, user: User, booking_id: int, method: PaymentMethod, account: str) -> Booking:
    """Charges the user for the held seat. On success, the hold becomes a confirmed booking."""
    try:
        booking = _lock_booking(db, booking_id, user)
        if booking.status == BookingStatus.confirmed:
            db.rollback()
            return booking  # already paid: confirming twice never charges twice
        if booking.status != BookingStatus.held:
            raise BookingError(409, f"Booking is {booking.status.value}")

        seat = _lock_seat(db, booking.seat_id)
        if seat.hold_expires_at is None or seat.hold_expires_at <= _now():
            _expire_current_hold(db, seat)
            db.commit()
            raise BookingError(410, "Hold expired, please pick the seat again")

        result = payment_gateway.charge(method, account, seat.price_cents, reference=f"booking-{booking.id}")
        db.add(Payment(
            booking_id=booking.id, user_id=user.id, amount_cents=seat.price_cents, method=method,
            account_last4=account[-4:], provider_ref=result.provider_ref, failure_reason=result.failure_reason,
            status=PaymentStatus.succeeded if result.ok else PaymentStatus.failed,
        ))
        if not result.ok:
            db.commit()  # keep the failed attempt on record; the hold stays so the user can retry
            raise BookingError(402, f"Payment failed: {result.failure_reason}")

        seat.status = SeatStatus.booked
        seat.hold_expires_at = None
        booking.status = BookingStatus.confirmed
        booking.confirmed_at = _now()
        db.commit()  # payment + booking + seat: all or nothing
        return booking
    except BookingError:
        if db.in_transaction():
            db.rollback()
        raise


def start_aamarpay_checkout(db: Session, user: User, booking_id: int, return_base_url: str) -> str:
    """Records a pending payment for a held seat and returns aamarPay's payment page URL.
    The pending row is saved BEFORE calling aamarPay, so even if the user pays and closes the
    tab, the sweeper knows the transaction ID and can still check it (reconcile_pending_payments)."""
    booking = db.execute(select(Booking).where(Booking.id == booking_id)).scalar_one_or_none()
    if booking is None or booking.user_id != user.id:
        raise BookingError(404, "Booking not found")
    if booking.status != BookingStatus.held:
        raise BookingError(409, f"Booking is {booking.status.value}")
    seat = db.get(Seat, booking.seat_id)
    if seat.hold_expires_at and seat.hold_expires_at <= _now():
        raise BookingError(410, "Hold expired, please pick the seat again")

    tran_id = f"TD{booking.id}-{uuid.uuid4().hex[:12]}"  # ours; aamarPay allows max 32 chars
    payment = Payment(
        booking_id=booking.id, user_id=user.id, amount_cents=seat.price_cents, method=PaymentMethod.aamarpay,
        account_last4="", status=PaymentStatus.pending, provider_ref=tran_id,
    )
    db.add(payment)
    db.commit()
    try:
        return aamarpay.create_payment(
            tran_id=tran_id, amount_cents=seat.price_cents, description=f"{seat.event.title}: seat {seat.label}",
            customer_name=user.name or user.email, customer_email=user.email,
            return_url=f"{return_base_url}payments/aamarpay/return?tran_id={tran_id}",
        )
    except PaymentProviderError as e:
        payment.status, payment.failure_reason = PaymentStatus.failed, str(e)[:200]
        db.commit()
        raise BookingError(502, "Payment gateway is unavailable, please try again") from e


def fulfill_aamarpay_payment(db: Session, result: Verification) -> tuple[Booking | None, str]:
    """Applies what aamarPay says about one of our transactions. Called from the browser return
    AND from the sweeper, so it's idempotent. `result` comes from aamarpay.verify(), fetched
    BEFORE taking any lock, so no row is locked while we wait on the network.
    Returns (booking, outcome), outcome being one of:
      confirmed       - this call confirmed the booking
      duplicate       - already handled earlier (refresh, or the other path got here first)
      refund_pending  - paid, but the seat can't be given (hold lost / wrong amount / already
                        paid by another attempt): the admin must refund it in aamarPay's panel
      failed          - the payment failed or expired; the hold stays, so the user can retry
      unpaid          - nothing paid (yet)
    Same lock order as confirm_booking (booking, then seat), so the two can never deadlock."""
    try:
        payment = db.execute(
            select(Payment).where(Payment.provider_ref == result.tran_id)
        ).scalar_one_or_none()
        if payment is None:
            return None, "unpaid"
        booking = db.execute(
            select(Booking).where(Booking.id == payment.booking_id).with_for_update()
        ).scalar_one()
        db.refresh(payment)  # re-read now that we hold the booking lock
        if payment.status != PaymentStatus.pending:
            db.rollback()
            return booking, "duplicate"
        if result.status == "failed":
            payment.status, payment.failure_reason = PaymentStatus.failed, "Payment failed or expired at aamarPay"
            db.commit()
            return booking, "failed"
        if result.status != "paid":
            db.rollback()
            return booking, "unpaid"

        seat = _lock_seat(db, booking.seat_id)
        payment.account_last4 = result.card_last4
        still_ours = (
            booking.status == BookingStatus.held and seat.status == SeatStatus.held and seat.held_by == booking.user_id
        )
        if not still_ours:
            reason = "Seat no longer held when the payment arrived"
        elif result.amount_cents != payment.amount_cents or result.currency != "BDT":
            reason = "Paid amount doesn't match the seat price"
        else:
            # Paid while we still hold the seat. A hold that ran out a moment ago but that nobody
            # else has taken yet still counts: the user paid, so they get the seat.
            payment.status = PaymentStatus.succeeded
            seat.status = SeatStatus.booked
            seat.hold_expires_at = None
            booking.status = BookingStatus.confirmed
            booking.confirmed_at = _now()
            db.commit()  # payment + booking + seat: all or nothing
            return booking, "confirmed"

        payment.status, payment.failure_reason = PaymentStatus.refund_pending, reason
        db.commit()
        return booking, "refund_pending"
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


def reconcile_pending_payments(db: Session, verify, older_than_seconds: int = 30) -> list[tuple[Booking, str]]:
    """Run by the background sweeper: checks aamarPay payments that are still pending (e.g. the
    user paid, then closed the tab before being sent back). `verify` is aamarpay.verify, passed
    in so tests can fake it. Pending attempts nobody paid for are closed once the hold is gone."""
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    tran_ids = db.scalars(
        select(Payment.provider_ref).where(Payment.status == PaymentStatus.pending, Payment.created_at < cutoff)
        .order_by(Payment.id).limit(50)
    ).all()
    db.rollback()  # don't keep a transaction open while calling aamarPay
    outcomes = []
    for tran_id in tran_ids:
        try:
            result = verify(tran_id)
        except PaymentProviderError:
            continue  # aamarPay is down: try again on the next sweep
        booking, outcome = fulfill_aamarpay_payment(db, result)
        if outcome == "unpaid" and booking is not None and booking.status != BookingStatus.held:
            payment = db.execute(select(Payment).where(Payment.provider_ref == tran_id)).scalar_one()
            if payment.status == PaymentStatus.pending:  # never paid, and the hold is gone
                payment.status, payment.failure_reason = PaymentStatus.failed, "Not paid before the hold ended"
                db.commit()
            outcome = "failed"
        outcomes.append((booking, outcome))
    return outcomes


def refund_payments(db: Session, booking_ids: list[int]) -> int:
    """Refunds the successful payments of these bookings (the caller commits). Demo-gateway
    payments are simply marked refunded. aamarPay has no refund API, so its payments become
    refund_pending: the admin sends the money back from the aamarPay merchant panel.
    Returns how many payments were refunded or queued for refund."""
    if not booking_ids:
        return 0
    payments = db.execute(
        select(Payment).where(Payment.booking_id.in_(booking_ids), Payment.status == PaymentStatus.succeeded)
    ).scalars().all()
    for p in payments:
        if p.method == PaymentMethod.aamarpay:
            p.status, p.failure_reason = PaymentStatus.refund_pending, "Booking cancelled"
        else:
            p.status, p.refunded_at = PaymentStatus.refunded, _now()
    return len(payments)


def cancel_booking(db: Session, user: User, booking_id: int) -> tuple[Booking, int | None]:
    """Cancels a booking and frees the seat. If anyone is on the waitlist, the
    seat goes straight to them as a new hold. Returns (booking, promoted_user_id)."""
    try:
        booking = _lock_booking(db, booking_id, user)
        if booking.status not in (BookingStatus.held, BookingStatus.confirmed):
            raise BookingError(409, f"Booking is {booking.status.value}")
        seat = _lock_seat(db, booking.seat_id)
        if booking.status == BookingStatus.confirmed:
            refund_payments(db, [booking.id])
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
