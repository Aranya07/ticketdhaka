"""Mock payment gateway (stands in for bKash / Nagad / SSLCommerz).

No money really moves. Every charge "succeeds" except for test accounts that end in
"0000", which get declined (the same idea as Stripe's test cards), so the failure
path can be tried from the UI and from tests.

To use a real provider, swap `charge()` for an HTTP call that passes `reference` as
the provider's idempotency key, so a network retry can never charge twice.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models import PaymentMethod

DECLINE_SUFFIX = "0000"


@dataclass(frozen=True)
class ChargeResult:
    ok: bool
    provider_ref: str
    failure_reason: str | None = None


def charge(method: PaymentMethod, account: str, amount_cents: int, reference: str) -> ChargeResult:
    provider_ref = f"TXN-{uuid.uuid4().hex[:16].upper()}"
    if account.endswith(DECLINE_SUFFIX):
        reason = "Card declined" if method == PaymentMethod.card else "Insufficient balance"
        return ChargeResult(ok=False, provider_ref=provider_ref, failure_reason=reason)
    return ChargeResult(ok=True, provider_ref=provider_ref)
