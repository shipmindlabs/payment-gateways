"""Payment provider abstraction with auditable operations."""

from payment_gateways.audit import (
    REDACTED,
    AuditedProvider,
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
    Origin,
    record_callback,
    redact,
)
from payment_gateways.errors import (
    Declined,
    ErrorKind,
    IdempotencyConflict,
    InvalidState,
    NetworkError,
    PaymentFailed,
    ProviderError,
)
from payment_gateways.fake import FakeProvider
from payment_gateways.idempotency import IdempotencyStore, request_fingerprint
from payment_gateways.models import (
    Operation,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
)
from payment_gateways.money import Money
from payment_gateways.protocol import PaymentProvider
from payment_gateways.retry import RetryingProvider

__all__ = [
    "REDACTED",
    "AuditRecord",
    "AuditSink",
    "AuditedProvider",
    "Declined",
    "ErrorKind",
    "FakeProvider",
    "IdempotencyConflict",
    "IdempotencyStore",
    "InMemoryAuditSink",
    "InvalidState",
    "Money",
    "NetworkError",
    "Operation",
    "Origin",
    "PaymentFailed",
    "PaymentProvider",
    "PaymentResult",
    "PaymentStatus",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "RetryingProvider",
    "__version__",
    "record_callback",
    "redact",
    "request_fingerprint",
]

__version__ = "0.1.0"
