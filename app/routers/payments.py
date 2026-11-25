"""aamarPay checkout endpoints. The flow is explained in app/services/aamarpay.py."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.cache import Cache
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.routers.events import get_cache, notify_change
from app.services import aamarpay
from app.services import booking_service as svc
from app.services.aamarpay import PaymentProviderError
from app.services.booking_service import BookingError

router = APIRouter(tags=["payments"])

# Where the browser lands after aamarPay, per outcome (index.html shows a matching message)
RETURN_PAGE = {"confirmed": "success", "duplicate": "success", "refund_pending": "refund",
               "failed": "failed", "unpaid": "pending"}


@router.get("/payments/config")
def payment_config():
    """Tells the frontend which payment flow to show."""
    return {"provider": "aamarpay" if aamarpay.enabled() else "demo", "currency": "BDT"}


@router.post("/bookings/{booking_id}/checkout", responses={409: {"description": "Not held, or aamarPay not set up"}})
def start_checkout(
    booking_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Starts an aamarPay payment for a held seat. Send the browser to `payment_url`, where the
    user pays with bKash, Nagad, Rocket or a card."""
    if not aamarpay.enabled():
        raise HTTPException(409, "aamarPay isn't set up on this server; pay with POST /bookings/{id}/confirm")
    try:
        url = svc.start_aamarpay_checkout(db, user, booking_id, str(request.base_url))
    except BookingError as e:
        raise HTTPException(e.status_code, e.detail)
    return {"payment_url": url}


@router.api_route("/payments/aamarpay/return", methods=["GET", "POST"], include_in_schema=False)
def aamarpay_return(tran_id: str, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    """aamarPay sends the browser here after a payment (success, fail or cancel). No login needed
    and nothing from the request is trusted except our own transaction ID: the server asks
    aamarPay's API what really happened, so a faked redirect can't confirm a booking."""
    try:
        result = aamarpay.verify(tran_id)
    except PaymentProviderError:
        return RedirectResponse("/?payment=pending", status_code=303)  # the sweeper will finish it
    booking, outcome = svc.fulfill_aamarpay_payment(db, result)
    if outcome == "confirmed":
        notify_change(cache, booking.seat.event_id, booking.seat_id, "booked")
    return RedirectResponse(f"/?payment={RETURN_PAGE[outcome]}", status_code=303)
