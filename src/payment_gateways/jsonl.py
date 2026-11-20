"""A trail that outlives the process: audit records as JSON lines.

`InMemoryAuditSink` answers questions for the length of a test run. The
questions that matter — what was sent, what came back, when, and how long it
took — are asked weeks later, by support, by finance or by a chargeback, and by
then the process that held the records is gone.

`JsonlAuditSink` writes each record as one JSON object on its own line and
flushes it, so an exchange is on disk before the caller reacts to it. The file
is opened for appending, so a restart continues the trail instead of replacing
it. Records arrive already redacted when they come from `AuditedProvider` or
`record_callback`: this module only writes what it is handed.

`record_to_dict` is the shape, separate from the file, so a sink that writes to
a table or a queue can record the same object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from payment_gateways.audit import AuditRecord
from payment_gateways.errors import ProviderError
from payment_gateways.models import PaymentResult, ProviderRequest, ProviderResponse
from payment_gateways.money import Money

__all__ = ["JsonlAuditSink", "record_to_dict"]


def _plain(value: Any) -> Any:
    if isinstance(value, Money):
        # The decimal text, never a float: a cent lost in binary rounding is a
        # cent the trail can no longer account for.
        return {"amount": str(value.amount), "currency": value.currency}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _request_to_dict(request: ProviderRequest) -> dict[str, Any]:
    return {
        "operation": request.operation.value,
        "provider": request.provider,
        "idempotency_key": request.idempotency_key,
        "order_id": request.order_id,
        "transaction_id": request.transaction_id,
        "amount": _plain(request.amount),
        "metadata": _plain(request.metadata),
        "created_at": _plain(request.created_at),
    }


def _result_to_dict(result: PaymentResult) -> dict[str, Any]:
    return {
        "transaction_id": result.transaction_id,
        "status": result.status.value,
        "amount": _plain(result.amount),
        "provider": result.provider,
        "created_at": _plain(result.created_at),
        "raw": _plain(result.raw),
    }


def _error_to_dict(error: ProviderError) -> dict[str, Any]:
    return {
        "kind": error.kind.value,
        "message": error.message,
        "code": error.code,
        "raw": _plain(error.raw),
    }


def _response_to_dict(response: ProviderResponse) -> dict[str, Any]:
    result = response.result
    error = response.error
    return {
        "result": None if result is None else _result_to_dict(result),
        "error": None if error is None else _error_to_dict(error),
        "replayed": response.replayed,
        "received_at": _plain(response.received_at),
        "raw": _plain(response.raw),
    }


def record_to_dict(record: AuditRecord) -> dict[str, Any]:
    """Render one audit record as plain JSON-ready types.

    A record carries one request — the one that was sent — so the copy the
    response echoes back is left out of the object.
    """
    response = record.response
    return {
        "recorded_at": _plain(record.recorded_at),
        "origin": record.origin.value,
        "ok": record.ok,
        "duration_ms": record.duration_ms,
        "exception": record.exception,
        "request": _request_to_dict(record.request),
        "response": None if response is None else _response_to_dict(response),
    }


class JsonlAuditSink:
    """An `AuditSink` that appends one JSON object per record to a file.

    The file is opened for appending on construction and closed by `close()` or
    by leaving the `with` block. Every record is flushed as it is written.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._file: TextIO | None = open(
            self._path, "a", encoding="utf-8", newline="\n"
        )

    @property
    def path(self) -> Path:
        return self._path

    def record(self, record: AuditRecord) -> None:
        """Append `record` to the file and flush it."""
        if self._file is None:
            raise ValueError(f"audit sink for {self._path} is closed")
        line = json.dumps(
            record_to_dict(record), default=str, ensure_ascii=False
        )
        self._file.write(f"{line}\n")
        # A trail still sitting in a buffer when the process dies is a trail
        # that cannot answer the question it was written for.
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> JsonlAuditSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
