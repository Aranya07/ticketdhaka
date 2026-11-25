import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import BookingStatus, PaymentMethod, Role, SeatStatus


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    # No role field on purpose: everyone signs up as an attendee. Only an admin can promote users.


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    name: str
    role: Role


Category = Literal["music", "tech", "sports", "food", "theatre", "comedy", "workshop", "festival", "other"]


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    venue: str = Field(min_length=1, max_length=200)
    area: str = Field(default="Dhaka", min_length=1, max_length=60)
    category: Category = "other"
    starts_at: datetime
    rows: int = Field(ge=1, le=26)
    seats_per_row: int = Field(ge=1, le=50)
    price_cents: int = Field(ge=0)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    venue: str
    area: str = "Dhaka"
    category: str = "other"
    starts_at: datetime
    total_seats: int = 0
    available_seats: int = 0
    price_from_cents: int | None = None


class SeatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    price_cents: int
    status: SeatStatus
    hold_expires_at: datetime | None = None


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    seat_id: int
    seat_label: str
    event_id: int
    status: BookingStatus
    created_at: datetime
    confirmed_at: datetime | None
    hold_expires_at: datetime | None
    amount_cents: int
    transaction_id: str | None = None  # gateway ref of the successful payment, once paid


BD_MOBILE = re.compile(r"^01[3-9]\d{8}$")  # e.g. 01712345678
CARD_NUMBER = re.compile(r"^\d{16}$")


class PaymentIn(BaseModel):
    method: PaymentMethod
    account: str = Field(description="bKash/Nagad: 11-digit mobile number. Card: 16-digit card number. "
                                     "Mock gateway: anything ending in 0000 is declined.")

    @model_validator(mode="after")
    def check_account(self):
        self.account = self.account.replace(" ", "").replace("-", "")
        pattern = CARD_NUMBER if self.method == PaymentMethod.card else BD_MOBILE
        if not pattern.match(self.account):
            kind = "a 16-digit card number" if self.method == PaymentMethod.card else "an 11-digit mobile number (01XXXXXXXXX)"
            raise ValueError(f"account must be {kind}")
        return self


class Page(BaseModel):
    items: list
    total: int
    limit: int
    offset: int


class RoleUpdate(BaseModel):
    role: Role


class AdminUserOut(UserOut):
    created_at: datetime


class AdminBookingOut(BaseModel):
    id: int
    status: BookingStatus
    user_email: str
    event_title: str
    seat_id: int
    seat_label: str
    price_cents: int
    created_at: datetime
    confirmed_at: datetime | None
