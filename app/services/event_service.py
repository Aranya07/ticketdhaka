from datetime import UTC, datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core.cache import Cache
from app.models import CATEGORIES, Event, Seat, SeatStatus, User
from app.schemas import EventCreate, EventOut, SeatOut


def _row_label(i: int) -> str:
    return chr(ord("A") + i)


def create_event(db: Session, organizer: User, data: EventCreate) -> Event:
    event = Event(
        title=data.title, venue=data.venue, area=data.area.strip(), category=data.category,
        starts_at=data.starts_at, organizer_id=organizer.id,
    )
    event.seats = [
        Seat(label=f"{_row_label(r)}{n + 1}", price_cents=data.price_cents)
        for r in range(data.rows)
        for n in range(data.seats_per_row)
    ]
    db.add(event)
    db.commit()
    return event


def _seat_counts():
    return (
        select(
            Seat.event_id,
            func.count(Seat.id).label("total"),
            func.sum(case((Seat.status == SeatStatus.available, 1), else_=0)).label("available"),
            func.min(Seat.price_cents).label("price_from"),
        )
        .group_by(Seat.event_id)
        .subquery()
    )


def list_events(
    db: Session, cache: Cache, limit: int, offset: int,
    category: str | None = None, area: str | None = None, q: str | None = None, include_past: bool = False,
) -> dict:
    q = (q or "").strip()[:80] or None
    key = f"events:list:{limit}:{offset}:{category}:{area}:{q}:{include_past}"
    if (hit := cache.get(key)) is not None:
        return hit

    filters = []
    if not include_past:
        filters.append(Event.starts_at >= datetime.now(UTC))
    if category:
        filters.append(Event.category == category)
    if area:
        filters.append(func.lower(Event.area) == area.lower())
    if q:
        like = f"%{q}%"
        filters.append(or_(Event.title.ilike(like), Event.venue.ilike(like), Event.area.ilike(like)))

    counts = _seat_counts()
    rows = db.execute(
        select(Event, counts.c.total, counts.c.available, counts.c.price_from)
        .outerjoin(counts, counts.c.event_id == Event.id)
        .where(*filters)
        .order_by(Event.starts_at)
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count(Event.id)).where(*filters)) or 0
    items = [
        EventOut(
            id=e.id, title=e.title, venue=e.venue, area=e.area, category=e.category, starts_at=e.starts_at,
            total_seats=t or 0, available_seats=int(a or 0), price_from_cents=p,
        ).model_dump(mode="json")
        for e, t, a, p in rows
    ]
    page = {"items": items, "total": total, "limit": limit, "offset": offset}
    cache.set(key, page)
    return page


def event_meta(db: Session, cache: Cache) -> dict:
    """Filter options for the UI: every category, plus the areas that have upcoming events."""
    if (hit := cache.get("events:list:meta")) is not None:
        return hit
    areas = db.execute(
        select(Event.area, func.count(Event.id))
        .where(Event.starts_at >= datetime.now(UTC))
        .group_by(Event.area)
        .order_by(func.count(Event.id).desc(), Event.area)
    ).all()
    meta = {"categories": list(CATEGORIES), "areas": [a for a, _ in areas]}
    cache.set("events:list:meta", meta)
    return meta


def get_seats(db: Session, cache: Cache, event_id: int) -> list[dict] | None:
    key = f"event:{event_id}:seats"
    if (hit := cache.get(key)) is not None:
        return hit
    if db.get(Event, event_id) is None:
        return None
    seats = db.execute(select(Seat).where(Seat.event_id == event_id).order_by(Seat.id)).scalars().all()
    data = [SeatOut.model_validate(s).model_dump(mode="json") for s in seats]
    cache.set(key, data)
    return data


def invalidate_event(cache: Cache, event_id: int) -> None:
    cache.invalidate_prefix(f"event:{event_id}:")
    cache.invalidate_prefix("events:list:")
