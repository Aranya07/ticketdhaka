from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.cache import Cache
from app.db import get_db
from app.deps import get_current_user
from app.models import Booking, PaymentStatus, User
from app.routers.events import get_cache, notify_change
from app.schemas import BookingOut, PaymentIn
from app.services import booking_service as svc
from app.services import stripe_payments
from app.services.booking_service import BookingError

router = APIRouter(tags=["bookings"])


def _out(b: Booking) -> BookingOut:
    paid = next((p for p in b.payments if p.status != PaymentStatus.failed), None)
    return BookingOut(
        id=b.id, seat_id=b.seat_id, seat_label=b.seat.label, event_id=b.seat.event_id,
        status=b.status, created_at=b.created_at, confirmed_at=b.confirmed_at,
        hold_expires_at=b.seat.hold_expires_at if b.status.value == "held" else None,
        amount_cents=b.seat.price_cents, transaction_id=paid.provider_ref if paid else None,
    )


def _reload(db: Session, booking_id: int) -> Booking:
    return db.execute(
        select(Booking).options(joinedload(Booking.seat), selectinload(Booking.payments))
        .where(Booking.id == booking_id).execution_options(populate_existing=True)
    ).scalar_one()


@router.post("/events/{event_id}/seats/{seat_id}/hold", response_model=BookingOut)
def hold(
    event_id: int,
    seat_id: int,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64),
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
    user: User = Depends(get_current_user),
):
    try:
        result = svc.hold_seat(db, user, event_id, seat_id, idempotency_key)
    except BookingError as e:
        raise HTTPException(e.status_code, e.detail)
    response.status_code = 201 if result.created else 200
    if result.created:
        notify_change(cache, event_id, seat_id, "held")
    return _out(_reload(db, result.booking.id))


@router.post("/bookings/{booking_id}/confirm", response_model=BookingOut,
             responses={402: {"description": "Payment declined; the hold is kept so you can retry"}})
def confirm(
    booking_id: int,
    payment: PaymentIn,
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
    user: User = Depends(get_current_user),
):
    """Pays with the built-in demo gateway. Switched off when Stripe is set up, so nobody can
    skip the real checkout with a made-up bKash number."""
    if stripe_payments.enabled():
        raise HTTPException(409, "This site takes payments through Stripe: use POST /bookings/{id}/checkout")
    try:
        booking = svc.confirm_booking(db, user, booking_id, payment.method, payment.account)
    except BookingError as e:
        if e.status_code == 410:  # the hold expired and the seat was freed
            b = _reload(db, booking_id)
            notify_change(cache, b.seat.event_id, b.seat_id, "available")
        raise HTTPException(e.status_code, e.detail)
    b = _reload(db, booking.id)
    notify_change(cache, b.seat.event_id, b.seat_id, "booked")
    return _out(b)


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
def cancel(
    booking_id: int,
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
    user: User = Depends(get_current_user),
):
    try:
        booking, promoted = svc.cancel_booking(db, user, booking_id)
    except BookingError as e:
        raise HTTPException(e.status_code, e.detail)
    b = _reload(db, booking.id)
    notify_change(cache, b.seat.event_id, b.seat_id, "held" if promoted else "available")
    return _out(b)


@router.get("/me/bookings", response_model=list[BookingOut])
def my_bookings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.execute(
        select(Booking).options(joinedload(Booking.seat), selectinload(Booking.payments))
        .where(Booking.user_id == user.id).order_by(Booking.created_at.desc())
    ).scalars().all()
    return [_out(b) for b in rows]


@router.post("/events/{event_id}/waitlist", status_code=201)
def join_waitlist(event_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        entry = svc.join_waitlist(db, user, event_id)
    except BookingError as e:
        raise HTTPException(e.status_code, e.detail)
    return {"id": entry.id, "event_id": event_id, "joined_at": entry.joined_at}
