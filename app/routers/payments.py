"""Stripe Checkout endpoints. The flow is explained in app/services/stripe_payments.py."""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.cache import Cache
from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import Booking, BookingStatus, Seat, User
from app.routers.events import get_cache, notify_change
from app.services import booking_service as svc
from app.services import stripe_payments
from app.services.stripe_payments import PaymentProviderError

router = APIRouter(tags=["payments"])

# What the browser lands on after Stripe, per fulfil outcome (index.html shows a matching message)
RETURN_PAGE = {"confirmed": "success", "duplicate": "success", "refunded": "refunded", "unpaid": "pending"}


@router.get("/payments/config")
def payment_config():
    """Tells the frontend which payment flow to show."""
    return {"provider": "stripe" if stripe_payments.enabled() else "demo", "currency": "BDT"}


@router.post("/bookings/{booking_id}/checkout", responses={409: {"description": "Not held, or Stripe not set up"}})
def start_checkout(
    booking_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Creates a Stripe Checkout page for a held seat. Send the browser to `checkout_url`."""
    if not stripe_payments.enabled():
        raise HTTPException(409, "Stripe isn't set up on this server; pay with POST /bookings/{id}/confirm")
    booking = db.execute(
        select(Booking).options(joinedload(Booking.seat).joinedload(Seat.event)).where(Booking.id == booking_id)
    ).scalar_one_or_none()
    if booking is None or booking.user_id != user.id:
        raise HTTPException(404, "Booking not found")
    if booking.status != BookingStatus.held:
        raise HTTPException(409, f"Booking is {booking.status.value}")
    seat = booking.seat
    if seat.hold_expires_at and seat.hold_expires_at <= datetime.now(UTC):
        raise HTTPException(410, "Hold expired, please pick the seat again")
    try:
        url = stripe_payments.create_checkout(
            booking_id=booking.id, user_id=user.id, title=f"{seat.event.title}: seat {seat.label}",
            amount_cents=seat.price_cents, base_url=str(request.base_url),
        )
    except PaymentProviderError as e:
        raise HTTPException(502, f"Stripe error: {e}")
    return {"checkout_url": url}


def _fulfill(session_id: str, cache: Cache) -> str:
    """Shared by the redirect and the webhook. Returns the fulfil outcome."""
    paid = stripe_payments.retrieve_checkout(session_id)
    with SessionLocal() as db:
        booking, outcome = svc.fulfill_stripe_payment(db, paid)
        if outcome == "confirmed":
            notify_change(cache, booking.seat.event_id, booking.seat_id, "booked")
    return outcome


@router.get("/payments/stripe/return", include_in_schema=False)
def stripe_return(session_id: str, cache: Cache = Depends(get_cache)):
    """Stripe sends the browser here after a payment. No login needed: the session ID is checked
    against Stripe's API, so a made-up or someone else's ID can't confirm anything it shouldn't."""
    try:
        outcome = _fulfill(session_id, cache)
    except PaymentProviderError:
        return RedirectResponse("/?payment=pending", status_code=303)  # the webhook will finish it
    return RedirectResponse(f"/?payment={RETURN_PAGE[outcome]}", status_code=303)


@router.post("/payments/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str = Header("", alias="Stripe-Signature")):
    """Stripe calls this after every payment, even if the user closed the tab. Any non-2xx answer
    makes Stripe retry later, which is safe because fulfilling is idempotent."""
    if not stripe_payments.webhook_enabled():
        raise HTTPException(503, "Stripe webhook secret isn't configured")
    payload = await request.body()  # the raw bytes: the signature is computed over them
    try:
        session_id = stripe_payments.checkout_session_from_webhook(payload, stripe_signature)
    except ValueError:
        raise HTTPException(400, "Invalid signature")
    if session_id:
        try:
            await run_in_threadpool(_fulfill, session_id, request.app.state.cache)
        except PaymentProviderError as e:
            raise HTTPException(502, f"Stripe error: {e}")
    return {"received": True}
