"""The single protocol every payment provider implements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from payment_gateways.models import PaymentResult
from payment_gateways.money import Money

__all__ = ["PaymentProvider"]


@runtime_checkable
class PaymentProvider(Protocol):
    """Hold, settle and inspect payments behind a provider-neutral surface."""

    name: str

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> PaymentResult:
        """Authorize `amount` for `order_id` without moving funds yet."""
        ...

    def capture(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> PaymentResult:
        """Settle a hold; `amount` of None captures the full held amount."""
        ...

    def refund(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> PaymentResult:
        """Return the whole captured amount to the payer."""
        ...

    def partial_refund(
        self,
        *,
        transaction_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> PaymentResult:
        """Return `amount` of a captured payment to the payer."""
        ...

    def cancel(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> PaymentResult:
        """Release a hold that has not been captured."""
        ...

    def status(self, *, transaction_id: str) -> PaymentResult:
        """Read the current state of a transaction from the provider."""
        ...
