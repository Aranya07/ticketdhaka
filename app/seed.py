"""Demo data, so the live site isn't empty.

- Demo accounts are made sure of on every start, so the "Use the demo account" button always works.
- The event catalog is only loaded into an EMPTY database. After that, an admin loads it on purpose
  (Admin → "Load Dhaka demo events"). That way events an admin deletes don't come back on every restart.
"""
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Event, Role, User
from app.schemas import EventCreate
from app.security import hash_password
from app.services.event_service import create_event

DEMO_USER = ("demo@ticketdhaka.dev", "Demo User", "demo12345")
SYSTEM_ORGANIZER = "team@ticketdhaka.dev"

# (title, venue, area, category, days from now, hour, rows, seats per row, price in BDT)
DHAKA_CATALOG = [
    ("Dhaka Tech Fest 2026", "ICCB Hall 4", "Purbachal Road", "tech", 10, 10, 8, 12, 500),
    ("Indie Night Live", "Shilpakala Academy", "Segunbagicha", "music", 14, 19, 6, 10, 800),
    ("AI Builders Meetup", "BUET Auditorium", "Palashi", "tech", 21, 15, 4, 8, 0),
    ("Rooftop Acoustic Sessions", "Gulshan Avenue Rooftop", "Gulshan", "music", 5, 20, 4, 10, 1200),
    ("Dhanmondi Street Food Carnival", "Rabindra Sarobar", "Dhanmondi", "food", 7, 16, 5, 12, 200),
    ("Stand-up Comedy Night", "Banani Club Hall", "Banani", "comedy", 9, 20, 5, 10, 700),
    ("Mirpur Friday Football Cup", "Mirpur Indoor Stadium", "Mirpur", "sports", 12, 15, 6, 12, 300),
    ("Bangla Theatre: Rokto Korobi", "National Theatre Hall", "Segunbagicha", "theatre", 16, 18, 7, 12, 600),
    ("Startup Pitch Workshop", "BRAC University Hall", "Merul Badda", "workshop", 18, 10, 3, 10, 400),
    ("Pohela Falgun Spring Festival", "Savar Riverside Park", "Savar", "festival", 25, 14, 6, 12, 350),
    ("Uttara Jazz & Blues Evening", "Uttara Club", "Uttara", "music", 28, 19, 5, 10, 1000),
    ("Narayanganj Food & Heritage Fair", "Panam City Grounds", "Narayanganj", "food", 30, 11, 5, 12, 150),
    ("Gazipur Weekend Cycling Rally", "Bhawal National Park Gate", "Gazipur", "sports", 33, 7, 4, 10, 250),
]


def _system_organizer(db: Session) -> User:
    org = db.execute(select(User).where(User.email == SYSTEM_ORGANIZER)).scalar_one_or_none()
    if org is None:
        # Random password nobody knows: this account only owns the catalog events, nobody can log in as it.
        org = User(email=SYSTEM_ORGANIZER, name="TicketDhaka Team",
                   hashed_password=hash_password(secrets.token_urlsafe(24)), role=Role.organizer)
        db.add(org)
        db.commit()
    return org


def ensure_demo_users(db: Session) -> None:
    email, name, password = DEMO_USER
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is None:
        db.add(User(email=email, name=name, hashed_password=hash_password(password)))
        db.commit()


def load_catalog(db: Session) -> int:
    """Adds every catalog event that isn't there yet (matched by title). Returns how many it created."""
    org = _system_organizer(db)
    existing = set(db.execute(select(Event.title)).scalars())
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    created = 0
    for title, venue, area, cat, days, hour, rows, per_row, price in DHAKA_CATALOG:
        if title in existing:
            continue
        # hour is Dhaka local time (UTC+6)
        starts = (base + timedelta(days=days)).replace(hour=(hour - 6) % 24)
        create_event(db, org, EventCreate(
            title=title, venue=venue, area=area, category=cat, starts_at=starts,
            rows=rows, seats_per_row=per_row, price_cents=price * 100,
        ))
        created += 1
    return created


def seed(db: Session) -> None:
    ensure_demo_users(db)
    if (db.scalar(select(func.count(Event.id))) or 0) == 0:
        load_catalog(db)


def ensure_admin(db: Session, email: str | None, password: str | None) -> None:
    """Creates the owner's admin account, or promotes it if it already exists.
    Credentials come from env vars (ADMIN_EMAIL / ADMIN_PASSWORD), so they never live in git.
    Changing ADMIN_PASSWORD and redeploying resets the password."""
    if not email or not password:
        return
    email = email.lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        db.add(User(email=email, name="Admin", hashed_password=hash_password(password), role=Role.admin))
    else:
        user.role = Role.admin
        user.hashed_password = hash_password(password)
    db.commit()
