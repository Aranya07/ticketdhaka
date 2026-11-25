from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.cache import Cache
from app.db import get_db
from app.deps import require_organizer
from app.models import User
from app.realtime import hub
from app.schemas import Category, EventCreate, EventOut, SeatOut
from app.services import event_service

router = APIRouter(prefix="/events", tags=["events"])


def get_cache(request: Request) -> Cache:
    return request.app.state.cache


@router.get("")
def list_events(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    category: Category | None = None,
    area: str | None = Query(None, max_length=60),
    q: str | None = Query(None, max_length=80, description="search title, venue or area"),
    include_past: bool = False,
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
):
    """Upcoming events, soonest first. Filter by category and area, or search with q."""
    return event_service.list_events(db, cache, limit, offset, category, area, q, include_past)


@router.get("/meta")
def meta(db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    """Filter options: all categories, plus the areas that currently have upcoming events."""
    return event_service.event_meta(db, cache)


@router.post("", response_model=EventOut, status_code=201)
def create_event(
    data: EventCreate,
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
    organizer: User = Depends(require_organizer),
):
    event = event_service.create_event(db, organizer, data)
    cache.invalidate_prefix("events:list:")
    n = data.rows * data.seats_per_row
    return EventOut(
        id=event.id, title=event.title, venue=event.venue, area=event.area, category=event.category,
        starts_at=event.starts_at, total_seats=n, available_seats=n, price_from_cents=data.price_cents,
    )


@router.get("/{event_id}/seats", response_model=list[SeatOut])
def seats(event_id: int, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    data = event_service.get_seats(db, cache, event_id)
    if data is None:
        raise HTTPException(404, "Event not found")
    return data


def notify_change(cache: Cache, event_id: int, seat_id: int, status: str) -> None:
    event_service.invalidate_event(cache, event_id)
    hub.publish(event_id, {"type": "seat", "seat_id": seat_id, "status": status})
