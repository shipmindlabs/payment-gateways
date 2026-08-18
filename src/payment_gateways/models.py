"""Types exchanged with payment providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, cast

from payment_gateways.errors import PaymentFailed, ProviderError
from payment_gateways.money import Money

__all__ = [
    "Operation",
    "PaymentResult",
    "PaymentStatus",
    "ProviderRequest",
    "ProviderResponse",
]


class PaymentStatus(str, Enum):
    """Provider-independent lifecycle state of a payment."""

    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Operation(str, Enum):
    """The provider calls that appear in the audit trail."""

    HOLD = "hold"
    CAPTURE = "capture"
    REFUND = "refund"
    PARTIAL_REFUND = "partial_refund"
    CANCEL = "cancel"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """State of a transaction as the provider reports it."""

    transaction_id: str
    status: PaymentStatus
    amount: Money
    provider: str
    created_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """What the caller asked a provider to do, as recorded for audit."""

    operation: Operation
    provider: str
    idempotency_key: str | None = None
    order_id: str | None = None
    transaction_id: str | None = None
    amount: Money | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A request paired with its outcome: exactly one of `result` or `error`."""

    request: ProviderRequest
    result: PaymentResult | None = None
    error: ProviderError | None = None
    received_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("a response carries either a result or an error")

    @property
    def ok(self) -> bool:
        return self.error is None

    def unwrap(self) -> PaymentResult:
        """Return the result, raising `PaymentFailed` for a failed operation."""
        if self.error is not None:
            raise PaymentFailed(self.error)
        return cast(PaymentResult, self.result)
