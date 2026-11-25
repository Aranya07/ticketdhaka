"""Database schema (3NF). See DESIGN.md for the ER diagram."""
import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    attendee = "attendee"
    organizer = "organizer"
    admin = "admin"


class SeatStatus(str, enum.Enum):
    available = "available"
    held = "held"
    booked = "booked"


class BookingStatus(str, enum.Enum):
    held = "held"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.attendee)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


CATEGORIES = ("music", "tech", "sports", "food", "theatre", "comedy", "workshop", "festival", "other")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_category_starts", "category", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    venue: Mapped[str] = mapped_column(String(200))
    # "in and around Dhaka": neighbourhood or nearby town, e.g. Gulshan, Dhanmondi, Purbachal, Savar
    area: Mapped[str] = mapped_column(String(60), default="Dhaka", server_default="Dhaka")
    category: Mapped[str] = mapped_column(String(30), default="other", server_default="other")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    organizer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    seats: Mapped[list["Seat"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("event_id", "label", name="uq_seat_event_label"),
        # Hot query: "available seats for event X" -> composite index
        Index("ix_seats_event_status", "event_id", "status"),
        # Expiry sweeper scans held seats by expiry time
        Index("ix_seats_status_expires", "status", "hold_expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(10))  # e.g. "B7"
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[SeatStatus] = mapped_column(
        Enum(SeatStatus, name="seat_status"), default=SeatStatus.available
    )
    held_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped[Event] = relationship(back_populates="seats")


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # Idempotency: the same client retrying with the same key never creates two bookings
        UniqueConstraint("user_id", "idempotency_key", name="uq_booking_idempotency"),
        Index("ix_bookings_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"), default=BookingStatus.held
    )
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    seat: Mapped[Seat] = relationship()


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_waitlist_event_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[int] = mapped_column(Integer, default=1)  # 0 = VIP, 1 = regular
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
