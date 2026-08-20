"""The single protocol every payment provider implements.

Every operation answers with a `ProviderResponse`: the request as it was sent,
paired with either a `PaymentResult` or a `ProviderError`, so the whole call can
go to the audit trail before the caller reacts to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from payment_gateways.models import ProviderResponse
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
    ) -> ProviderResponse:
        """Authorize `amount` for `order_id` without moving funds yet."""
        ...

    def capture(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> ProviderResponse:
        """Settle a hold; `amount` of None captures the full held amount."""
        ...

    def refund(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        """Return the whole captured amount to the payer."""
        ...

    def partial_refund(
        self,
        *,
        transaction_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResponse:
        """Return `amount` of a captured payment to the payer."""
        ...

    def cancel(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        """Release a hold that has not been captured."""
        ...

    def status(self, *, transaction_id: str) -> ProviderResponse:
        """Read the current state of a transaction from the provider."""
        ...
