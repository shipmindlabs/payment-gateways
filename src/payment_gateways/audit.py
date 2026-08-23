"""Audit trail for provider calls and callbacks.

Auditing wraps a provider instead of living inside it: `AuditedProvider`
forwards every call, times it and hands the outcome to an `AuditSink`. Values
under secret-looking keys are replaced before anything is handed over, and a
sink that raises is logged and ignored, so recording a payment can never be the
reason a payment fails.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from payment_gateways.errors import ProviderError
from payment_gateways.models import Operation, ProviderRequest, ProviderResponse
from payment_gateways.money import Money
from payment_gateways.protocol import PaymentProvider

__all__ = [
    "REDACTED",
    "AuditRecord",
    "AuditSink",
    "AuditedProvider",
    "InMemoryAuditSink",
    "Origin",
    "record_callback",
    "redact",
]

_log = logging.getLogger(__name__)

REDACTED = "***"

_SENSITIVE_TOKENS = frozenset(
    {
        "authorization",
        "card",
        "cvc",
        "cvn",
        "cvv",
        "iban",
        "pan",
        "passphrase",
        "password",
        "pin",
        "secret",
        "signature",
        "token",
    }
)
_SENSITIVE_MARKERS = (
    "accountnumber",
    "apikey",
    "cardnumber",
    "privatekey",
    "secretkey",
)
_MAX_DEPTH = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_sensitive(key: Any) -> bool:
    tokens = [token for token in re.split(r"[^a-z0-9]+", str(key).lower()) if token]
    if any(token in _SENSITIVE_TOKENS for token in tokens):
        return True
    joined = "".join(tokens)
    return any(marker in joined for marker in _SENSITIVE_MARKERS)


def redact(value: Any) -> Any:
    """Replace values held under secret-looking keys with `REDACTED`."""
    return _redact(value, 0)


def _redact(value: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    return value


class Origin(str, Enum):
    """Whether an audited exchange was started here or by the provider."""

    CALL = "call"
    CALLBACK = "callback"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One exchange with a provider, as written to the trail."""

    request: ProviderRequest
    response: ProviderResponse | None = None
    duration_ms: float | None = None
    origin: Origin = Origin.CALL
    exception: str | None = None
    recorded_at: datetime = field(default_factory=_now)

    @property
    def operation(self) -> Operation:
        return self.request.operation

    @property
    def ok(self) -> bool:
        return self.response is not None and self.response.ok

    @property
    def error(self) -> ProviderError | None:
        return None if self.response is None else self.response.error


@runtime_checkable
class AuditSink(Protocol):
    """Destination for audit records: a log, a table, a queue."""

    def record(self, record: AuditRecord) -> None:
        """Persist one record; may raise, the caller absorbs it."""
        ...


class InMemoryAuditSink:
    """Sink that keeps records in a list, for tests and for local runs."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)

    def record(self, record: AuditRecord) -> None:
        self._records.append(record)

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(tuple(self._records))


def _redact_request(request: ProviderRequest) -> ProviderRequest:
    return replace(request, metadata=redact(request.metadata))


def _redact_response(response: ProviderResponse | None) -> ProviderResponse | None:
    if response is None:
        return None
    result = response.result
    error = response.error
    return replace(
        response,
        request=_redact_request(response.request),
        result=None if result is None else replace(result, raw=redact(result.raw)),
        error=None if error is None else replace(error, raw=redact(error.raw)),
        raw=redact(response.raw),
    )


def _emit(sink: AuditSink, record: AuditRecord) -> None:
    try:
        sink.record(
            replace(
                record,
                request=_redact_request(record.request),
                response=_redact_response(record.response),
            )
        )
    except Exception:
        _log.warning("audit sink failed, record dropped", exc_info=True)


def record_callback(sink: AuditSink, response: ProviderResponse) -> None:
    """Record an inbound provider callback under the request it belongs to."""
    _emit(
        sink,
        AuditRecord(
            request=response.request,
            response=response,
            origin=Origin.CALLBACK,
        ),
    )


class AuditedProvider:
    """A `PaymentProvider` that records every call it forwards."""

    def __init__(self, provider: PaymentProvider, sink: AuditSink) -> None:
        self._provider = provider
        self._sink = sink

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
        request = ProviderRequest(
            operation=Operation.HOLD,
            provider=self.name,
            idempotency_key=idempotency_key,
            order_id=order_id,
            amount=amount,
            metadata=dict(metadata or {}),
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.hold(
                order_id=order_id,
                amount=amount,
                idempotency_key=idempotency_key,
                metadata=metadata,
            ),
        )

    def capture(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> ProviderResponse:
        request = ProviderRequest(
            operation=Operation.CAPTURE,
            provider=self.name,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            amount=amount,
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.capture(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
                amount=amount,
            ),
        )

    def refund(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = ProviderRequest(
            operation=Operation.REFUND,
            provider=self.name,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.refund(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
            ),
        )

    def partial_refund(
        self,
        *,
        transaction_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = ProviderRequest(
            operation=Operation.PARTIAL_REFUND,
            provider=self.name,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            amount=amount,
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.partial_refund(
                transaction_id=transaction_id,
                amount=amount,
                idempotency_key=idempotency_key,
            ),
        )

    def cancel(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
    ) -> ProviderResponse:
        request = ProviderRequest(
            operation=Operation.CANCEL,
            provider=self.name,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.cancel(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
            ),
        )

    def status(self, *, transaction_id: str) -> ProviderResponse:
        request = ProviderRequest(
            operation=Operation.STATUS,
            provider=self.name,
            transaction_id=transaction_id,
            created_at=_now(),
        )
        return self._run(
            request,
            lambda: self._provider.status(transaction_id=transaction_id),
        )

    def _run(
        self,
        request: ProviderRequest,
        call: Callable[[], ProviderResponse],
    ) -> ProviderResponse:
        started = time.perf_counter()
        try:
            response = call()
        except Exception as exc:
            _emit(
                self._sink,
                AuditRecord(
                    request=request,
                    duration_ms=_elapsed_ms(started),
                    exception=f"{type(exc).__name__}: {exc}",
                ),
            )
            raise
        _emit(
            self._sink,
            AuditRecord(
                request=request,
                response=response,
                duration_ms=_elapsed_ms(started),
            ),
        )
        return response


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
