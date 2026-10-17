"""The reference implementations answer the contract they document.

`AuditedProvider` and `RetryingProvider` run it too: a wrapper that changes an
answer on its way through is a wrapper that lies to the audit trail.
"""

from payment_gateways import (
    AuditedProvider,
    FakeProvider,
    InMemoryAuditSink,
    RetryingProvider,
)
from payment_gateways.contract import PaymentProviderContract
from payment_gateways.protocol import PaymentProvider


class TestFakeProviderContract(PaymentProviderContract):
    def make_provider(self) -> PaymentProvider:
        return FakeProvider()


class TestAuditedProviderContract(PaymentProviderContract):
    def make_provider(self) -> PaymentProvider:
        return AuditedProvider(FakeProvider(), InMemoryAuditSink())


class TestRetryingProviderContract(PaymentProviderContract):
    def make_provider(self) -> PaymentProvider:
        return RetryingProvider(FakeProvider(), sleep=lambda _: None)
