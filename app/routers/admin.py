"""Admin API: the owner's control panel. Every route requires the admin role.

GET    /admin/stats                    platform overview (users, seats, bookings, revenue, cache)
GET    /admin/users                    list users
PATCH  /admin/users/{id}/role          promote or demote (e.g. make someone an organizer)
GET    /admin/bookings                 all bookings, filterable by status/event
POST   /admin/seats/{seat_id}/release  force-free a stuck seat (cancels its active booking)
DELETE /admin/events/{event_id}        delete an event and its seats/bookings
POST   /admin/demo-events              load the Dhaka demo catalog (skips titles that already exist)
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.cache import Cache
from app.db import get_db
from app.deps import require_admin
from app.models import Booking, BookingStatus, Event, Role, Seat, SeatStatus, User, WaitlistEntry
from app.routers.events import get_cache, notify_change
from app.schemas import AdminBookingOut, AdminUserOut, RoleUpdate
from app.services.event_service import invalidate_event

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _counts(db: Session, column) -> dict[str, int]:
    return {k.value: v for k, v in db.execute(select(column, func.count()).group_by(column)).all()}


@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    revenue = db.scalar(
        select(func.coalesce(func.sum(Seat.price_cents), 0))
        .join(Booking, Booking.seat_id == Seat.id)
        .where(Booking.status == BookingStatus.confirmed)
    )
    return {
        "users": _counts(db, User.role),
        "events": db.scalar(select(func.count(Event.id))),
        "seats": _counts(db, Seat.status),
        "bookings": _counts(db, Booking.status),
        "waitlist": db.scalar(select(func.count(WaitlistEntry.id))),
        "revenue_bdt": (revenue or 0) / 100,
        "cache": cache.stats(),
    }


@router.get("/users", response_model=list[AdminUserOut])
def users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    return db.execute(select(User).order_by(User.id).limit(limit).offset(offset)).scalars().all()


@router.patch("/users/{user_id}/role", response_model=AdminUserOut)
def set_role(user_id: int, body: RoleUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id and body.role != Role.admin:
        raise HTTPException(400, "You can't remove your own admin role")
    user.role = body.role
    db.commit()
    return user


@router.get("/bookings", response_model=list[AdminBookingOut])
def bookings(
    status: BookingStatus | None = None,
    event_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = (
        select(Booking, User.email, Event.title, Seat.label, Seat.price_cents)
        .join(User, User.id == Booking.user_id)
        .join(Seat, Seat.id == Booking.seat_id)
        .join(Event, Event.id == Seat.event_id)
        .order_by(Booking.created_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(Booking.status == status)
    if event_id:
        q = q.where(Event.id == event_id)
    return [
        AdminBookingOut(id=b.id, status=b.status, user_email=email, event_title=title, seat_id=b.seat_id, seat_label=label,
                        price_cents=price, created_at=b.created_at, confirmed_at=b.confirmed_at)
        for b, email, title, label, price in db.execute(q).all()
    ]


@router.post("/seats/{seat_id}/release")
def release_seat(seat_id: int, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    """Takes the same row lock as a normal booking, so it can never race with a user."""
    seat = db.execute(select(Seat).where(Seat.id == seat_id).with_for_update()).scalar_one_or_none()
    if seat is None:
        raise HTTPException(404, "Seat not found")
    cancelled = db.query(Booking).filter(
        Booking.seat_id == seat.id, Booking.status.in_([BookingStatus.held, BookingStatus.confirmed])
    ).update({Booking.status: BookingStatus.cancelled}, synchronize_session=False)
    seat.status, seat.held_by, seat.hold_expires_at = SeatStatus.available, None, None
    db.commit()
    notify_change(cache, seat.event_id, seat.id, "available")
    return {"seat_id": seat.id, "status": "available", "bookings_cancelled": cancelled}


@router.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: int, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    seat_ids = select(Seat.id).where(Seat.event_id == event_id)
    db.query(Booking).filter(Booking.seat_id.in_(seat_ids)).delete(synchronize_session=False)
    db.delete(event)  # seats and waitlist cascade
    db.commit()
    invalidate_event(cache, event_id)


@router.post("/demo-events")
def load_demo_events(db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    from app.seed import load_catalog

    created = load_catalog(db)
    cache.invalidate_prefix("events:list:")
    return {"created": created}
