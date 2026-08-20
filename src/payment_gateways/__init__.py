"""Payment provider abstraction with auditable operations."""

from payment_gateways.errors import (
    Declined,
    ErrorKind,
    InvalidState,
    NetworkError,
    PaymentFailed,
    ProviderError,
)
from payment_gateways.fake import FakeProvider
from payment_gateways.models import (
    Operation,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
)
from payment_gateways.money import Money
from payment_gateways.protocol import PaymentProvider

__all__ = [
    "Declined",
    "ErrorKind",
    "FakeProvider",
    "InvalidState",
    "Money",
    "NetworkError",
    "Operation",
    "PaymentFailed",
    "PaymentProvider",
    "PaymentResult",
    "PaymentStatus",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "__version__",
]

__version__ = "0.1.0"
