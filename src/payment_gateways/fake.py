"""In-memory provider for tests, examples and documentation.

`FakeProvider` is the reference implementation of `PaymentProvider`: it runs the
transaction state machine every real provider is expected to expose, records
every call it answers, and lets a test script the next outcome of any operation
so that decline and network handling can be exercised without a network.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count
from typing import Any

from payment_gateways.errors import InvalidState, ProviderError
from payment_gateways.models import (
    Operation,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
)
from payment_gateways.money import Money

__all__ = ["FakeProvider"]

_ZERO = Decimal("0")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Transaction:
    """Mutable bookkeeping for one payment; never leaves the provider."""

    transaction_id: str
    order_id: str
    authorized: Money
    status: PaymentStatus
    captured: Decimal = _ZERO
    refunded: Decimal = _ZERO


def _check_amount(amount: Money, available: Money) -> InvalidState | None:
    if amount.currency != available.currency:
        return InvalidState(
            f"amount is in {amount.currency}, transaction is in {available.currency}"
        )
    if amount.amount <= _ZERO:
        return InvalidState("amount must be positive")
    if amount.amount > available.amount:
        return InvalidState(f"amount {amount} exceeds the available {available}")
    return None


class FakeProvider:
    """A payment provider that keeps every transaction in memory.

    Successful responses are stored under their idempotency key and replayed for
    a repeated call; failed ones are not, so a retryable error can be retried
    with the same key. `fail_next` queues an error to return from the next call
    to an operation, which leaves the transaction untouched.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self.calls: list[ProviderResponse] = []
        self._clock = clock or _utcnow
        self._transactions: dict[str, _Transaction] = {}
        self._replays: dict[tuple[Operation, str], ProviderResponse] = {}
        self._scripted: dict[Operation, deque[ProviderError]] = {}
        self._ids = count(1)

    def fail_next(self, operation: Operation, error: ProviderError) -> None:
        """Answer the next call to `operation` with `error`, changing no state."""
        self._scripted.setdefault(operation, deque()).append(error)

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        request = self._request(
            Operation.HOLD,
            idempotency_key=idempotency_key,
            order_id=order_id,
            amount=amount,
            metadata=metadata,
        )
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = _Transaction(
            transaction_id=f"{self.name}-{next(self._ids)}",
            order_id=order_id,
            authorized=amount,
            status=PaymentStatus.AUTHORIZED,
        )
        self._transactions[transaction.transaction_id] = transaction
        return self._succeed(request, transaction)

    def capture(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> ProviderResponse:
        request = self._request(
            Operation.CAPTURE,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            amount=amount,
        )
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return self._fail(request, InvalidState(f"unknown transaction {transaction_id}"))
        if transaction.status is not PaymentStatus.AUTHORIZED:
            return self._fail(
                request,
                InvalidState(f"cannot capture a {transaction.status.value} transaction"),
            )
        captured = amount if amount is not None else transaction.authorized
        invalid = _check_amount(captured, transaction.authorized)
        if invalid is not None:
            return self._fail(request, invalid)
        transaction.captured = captured.amount
        transaction.status = PaymentStatus.CAPTURED
        return self._succeed(request, transaction)

    def refund(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = self._request(
            Operation.REFUND,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
        )
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return self._fail(request, InvalidState(f"unknown transaction {transaction_id}"))
        if transaction.status not in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
        ):
            return self._fail(
                request,
                InvalidState(f"cannot refund a {transaction.status.value} transaction"),
            )
        transaction.refunded = transaction.captured
        transaction.status = PaymentStatus.REFUNDED
        return self._succeed(request, transaction)

    def partial_refund(
        self,
        *,
        transaction_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = self._request(
            Operation.PARTIAL_REFUND,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            amount=amount,
        )
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return self._fail(request, InvalidState(f"unknown transaction {transaction_id}"))
        if transaction.status not in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
        ):
            return self._fail(
                request,
                InvalidState(f"cannot refund a {transaction.status.value} transaction"),
            )
        remaining = Money(
            transaction.captured - transaction.refunded, transaction.authorized.currency
        )
        invalid = _check_amount(amount, remaining)
        if invalid is not None:
            return self._fail(request, invalid)
        transaction.refunded += amount.amount
        transaction.status = (
            PaymentStatus.REFUNDED
            if transaction.refunded == transaction.captured
            else PaymentStatus.PARTIALLY_REFUNDED
        )
        return self._succeed(request, transaction)

    def cancel(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = self._request(
            Operation.CANCEL,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
        )
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return self._fail(request, InvalidState(f"unknown transaction {transaction_id}"))
        if transaction.status is not PaymentStatus.AUTHORIZED:
            return self._fail(
                request,
                InvalidState(f"cannot cancel a {transaction.status.value} transaction"),
            )
        transaction.status = PaymentStatus.CANCELLED
        return self._succeed(request, transaction)

    def status(self, *, transaction_id: str) -> ProviderResponse:
        request = self._request(Operation.STATUS, transaction_id=transaction_id)
        early = self._short_circuit(request)
        if early is not None:
            return early
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return self._fail(request, InvalidState(f"unknown transaction {transaction_id}"))
        return self._succeed(request, transaction)

    def _request(
        self,
        operation: Operation,
        *,
        idempotency_key: str | None = None,
        order_id: str | None = None,
        transaction_id: str | None = None,
        amount: Money | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderRequest:
        return ProviderRequest(
            operation=operation,
            provider=self.name,
            idempotency_key=idempotency_key,
            order_id=order_id,
            transaction_id=transaction_id,
            amount=amount,
            metadata=dict(metadata or {}),
            created_at=self._clock(),
        )

    def _short_circuit(self, request: ProviderRequest) -> ProviderResponse | None:
        if request.idempotency_key is not None:
            replay = self._replays.get((request.operation, request.idempotency_key))
            if replay is not None:
                return self._record(
                    ProviderResponse(
                        request=request,
                        result=replay.result,
                        received_at=self._clock(),
                        raw={"replayed": True},
                    )
                )
        queued = self._scripted.get(request.operation)
        if queued:
            return self._fail(request, queued.popleft())
        return None

    def _succeed(
        self, request: ProviderRequest, transaction: _Transaction
    ) -> ProviderResponse:
        result = PaymentResult(
            transaction_id=transaction.transaction_id,
            status=transaction.status,
            amount=self._outstanding(transaction),
            provider=self.name,
            created_at=self._clock(),
            raw={"order_id": transaction.order_id},
        )
        return self._record(
            ProviderResponse(request=request, result=result, received_at=self._clock())
        )

    def _fail(self, request: ProviderRequest, error: ProviderError) -> ProviderResponse:
        return self._record(
            ProviderResponse(request=request, error=error, received_at=self._clock())
        )

    def _record(self, response: ProviderResponse) -> ProviderResponse:
        self.calls.append(response)
        key = response.request.idempotency_key
        if response.ok and key is not None:
            self._replays.setdefault((response.request.operation, key), response)
        return response

    @staticmethod
    def _outstanding(transaction: _Transaction) -> Money:
        """What the transaction currently stands at against the payer."""
        currency = transaction.authorized.currency
        if transaction.status is PaymentStatus.CANCELLED:
            return Money(_ZERO, currency)
        if transaction.captured > _ZERO:
            return Money(transaction.captured - transaction.refunded, currency)
        return transaction.authorized
