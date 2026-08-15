"""Types returned by payment providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from payment_gateways.money import Money

__all__ = ["PaymentResult", "PaymentStatus"]


class PaymentStatus(str, Enum):
    """Provider-independent lifecycle state of a payment."""

    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Outcome of a single provider operation."""

    transaction_id: str
    status: PaymentStatus
    amount: Money
    provider: str
    created_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
