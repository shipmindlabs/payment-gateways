"""Retry wrapper for provider answers that may still succeed.

A `NetworkError` never says whether the request reached the acquirer, only that
the answer did not come back. Sending it again is the one useful reaction, and
it is safe only under the key the first attempt already carried: that is what
lets the provider recognise the retry and replay instead of charging twice.

The other three failures are final. A `Declined` card, an operation the current
state does not allow and a key that belongs to a different request answer the
same way however often they are sent, so resending them only delays the caller.

Retrying is a wrapper for the same reason auditing is: `RetryingProvider`
implements `PaymentProvider`, so it composes with `AuditedProvider` in either
order, and a call site never learns that attempts were made at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from payment_gateways.models import ProviderResponse
from payment_gateways.money import Money
from payment_gateways.protocol import PaymentProvider

__all__ = ["RetryingProvider"]

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 0.2
DEFAULT_MULTIPLIER = 2.0
DEFAULT_MAX_BACKOFF = 5.0


class RetryingProvider:
    """A `PaymentProvider` that resends the answers which may still succeed.

    An answer whose error is retryable is sent again under the same idempotency
    key, up to `attempts` times, waiting `backoff` seconds and multiplying that
    wait by `multiplier` up to `max_backoff` between attempts. The response of
    the last attempt is returned as it is, failure included. `sleep` is
    injectable so a test can assert the waits without serving them.

    A provider that raises instead of returning an error is not retried: the
    exception is not an answer, and it travels up untouched.
    """

    def __init__(
        self,
        provider: PaymentProvider,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
        multiplier: float = DEFAULT_MULTIPLIER,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if backoff < 0:
            raise ValueError("backoff must not be negative")
        self._provider = provider
        self._attempts = attempts
        self._backoff = backoff
        self._multiplier = multiplier
        self._max_backoff = max_backoff
        self._sleep = sleep

    @property
    def name(self) -> str:
        return self._provider.name

    @property
    def provider(self) -> PaymentProvider:
        return self._provider

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        return self._run(
            lambda: self._provider.hold(
                order_id=order_id,
                amount=amount,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )
        )

    def capture(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> ProviderResponse:
        return self._run(
            lambda: self._provider.capture(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
                amount=amount,
            )
        )

    def refund(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        return self._run(
            lambda: self._provider.refund(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
            )
        )

    def partial_refund(
        self,
        *,
        transaction_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResponse:
        return self._run(
            lambda: self._provider.partial_refund(
                transaction_id=transaction_id,
                amount=amount,
                idempotency_key=idempotency_key,
            )
        )

    def cancel(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        return self._run(
            lambda: self._provider.cancel(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
            )
        )

    def status(self, *, transaction_id: str) -> ProviderResponse:
        return self._run(
            lambda: self._provider.status(transaction_id=transaction_id)
        )

    def _run(self, call: Callable[[], ProviderResponse]) -> ProviderResponse:
        attempt = 1
        while True:
            response = call()
            error = response.error
            if error is None or not error.retryable or attempt == self._attempts:
                return response
            self._sleep(self._delay(attempt))
            attempt += 1

    def _delay(self, attempt: int) -> float:
        return min(
            self._backoff * self._multiplier ** (attempt - 1), self._max_backoff
        )
