"""Error values returned by provider operations.

Failures here are values, not only exceptions. A declined card, an unreachable
acquirer and a capture on an already cancelled hold are ordinary outcomes of a
payment run: each one has to be written to the audit trail before the caller
reacts to it, and an exception unwinding the stack makes that recording easy to
skip. A returned error is also visible to the type checker, so a call site that
forgets to handle a decline fails review instead of production. Callers who
prefer the raising style still get it from ``ProviderResponse.unwrap()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

__all__ = [
    "Declined",
    "ErrorKind",
    "InvalidState",
    "NetworkError",
    "PaymentFailed",
    "ProviderError",
]


class ErrorKind(str, Enum):
    """Provider-independent classification of a failed operation."""

    DECLINED = "declined"
    NETWORK = "network"
    INVALID_STATE = "invalid_state"


@dataclass(frozen=True, slots=True)
class ProviderError:
    """Common shape of the error union; providers return one of the subclasses."""

    message: str
    code: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    kind: ClassVar[ErrorKind]
    retryable: ClassVar[bool] = False

    def __str__(self) -> str:
        if self.code is None:
            return f"{self.kind.value}: {self.message}"
        return f"{self.kind.value} [{self.code}]: {self.message}"


@dataclass(frozen=True, slots=True)
class Declined(ProviderError):
    """The provider processed the request and refused to move the money."""

    kind: ClassVar[ErrorKind] = ErrorKind.DECLINED


@dataclass(frozen=True, slots=True)
class NetworkError(ProviderError):
    """The provider was unreachable, or answered too late to be trusted."""

    kind: ClassVar[ErrorKind] = ErrorKind.NETWORK
    retryable: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class InvalidState(ProviderError):
    """The operation does not apply to the current state of the transaction."""

    kind: ClassVar[ErrorKind] = ErrorKind.INVALID_STATE


class PaymentFailed(Exception):
    """Raised by ``ProviderResponse.unwrap()`` when the operation failed."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(str(error))
        self.error = error
