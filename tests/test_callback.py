"""A pending hold moves only when the provider's callback decides it."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_gateways import (
    ErrorKind,
    FakeProvider,
    InMemoryAuditSink,
    Money,
    Operation,
    Origin,
    PaymentStatus,
    ProviderError,
    ProviderResponse,
    record_callback,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


def hold(
    provider: FakeProvider, *, order_id: str = "order-1", key: str = "hold-order-1"
) -> ProviderResponse:
    return provider.hold(
        order_id=order_id, amount=money("40.00"), idempotency_key=key
    )


def pending(provider: FakeProvider) -> str:
    provider.pending_next()
    result = hold(provider).unwrap()
    assert result.status is PaymentStatus.PENDING
    return result.transaction_id


def refused(response: ProviderResponse) -> ProviderError:
    assert not response.ok
    assert response.result is None
    assert response.error is not None
    assert response.error.kind is ErrorKind.INVALID_STATE
    return response.error


def test_a_pending_hold_reports_the_amount_it_was_asked_for(
    provider: FakeProvider,
) -> None:
    transaction_id = pending(provider)

    current = provider.status(transaction_id=transaction_id).unwrap()

    assert current.status is PaymentStatus.PENDING
    assert current.amount == money("40.00")


def test_only_the_next_hold_is_left_pending(provider: FakeProvider) -> None:
    provider.pending_next()

    first = hold(provider, key="hold-1").unwrap()
    second = hold(provider, order_id="order-2", key="hold-2").unwrap()

    assert first.status is PaymentStatus.PENDING
    assert second.status is PaymentStatus.AUTHORIZED


def test_a_replayed_hold_is_not_decided_a_second_time(
    provider: FakeProvider,
) -> None:
    provider.pending_next()
    first = hold(provider, key="hold-1").unwrap()

    again = hold(provider, key="hold-1")
    later = hold(provider, order_id="order-2", key="hold-2").unwrap()

    assert again.replayed
    assert again.unwrap().status is PaymentStatus.PENDING
    assert again.unwrap().transaction_id == first.transaction_id
    assert later.status is PaymentStatus.AUTHORIZED


def test_a_callback_authorizes_a_pending_hold(provider: FakeProvider) -> None:
    transaction_id = pending(provider)

    settled = provider.callback(
        transaction_id=transaction_id, outcome=PaymentStatus.AUTHORIZED
    )

    assert settled.unwrap().status is PaymentStatus.AUTHORIZED
    assert settled.unwrap().amount == money("40.00")
    captured = provider.capture(
        transaction_id=transaction_id, idempotency_key="capture-1"
    ).unwrap()
    assert captured.status is PaymentStatus.CAPTURED
    assert captured.amount == money("40.00")


def test_a_callback_fails_a_pending_hold_and_leaves_nothing_owed(
    provider: FakeProvider,
) -> None:
    transaction_id = pending(provider)

    settled = provider.callback(
        transaction_id=transaction_id, outcome=PaymentStatus.FAILED
    ).unwrap()

    assert settled.status is PaymentStatus.FAILED
    assert settled.amount == money("0")
    refused(
        provider.capture(transaction_id=transaction_id, idempotency_key="capture-1")
    )


def test_a_pending_hold_refuses_every_operation_but_a_callback(
    provider: FakeProvider,
) -> None:
    transaction_id = pending(provider)

    refused(
        provider.capture(transaction_id=transaction_id, idempotency_key="capture-1")
    )
    refused(provider.cancel(transaction_id=transaction_id, idempotency_key="cancel-1"))
    refused(provider.refund(transaction_id=transaction_id, idempotency_key="refund-1"))
    refused(
        provider.partial_refund(
            transaction_id=transaction_id,
            amount=money("10.00"),
            idempotency_key="partial-1",
        )
    )

    assert (
        provider.status(transaction_id=transaction_id).unwrap().status
        is PaymentStatus.PENDING
    )


def test_a_second_callback_finds_nothing_pending(provider: FakeProvider) -> None:
    transaction_id = pending(provider)
    provider.callback(
        transaction_id=transaction_id, outcome=PaymentStatus.AUTHORIZED
    ).unwrap()

    refused(
        provider.callback(
            transaction_id=transaction_id, outcome=PaymentStatus.FAILED
        )
    )

    assert (
        provider.status(transaction_id=transaction_id).unwrap().status
        is PaymentStatus.AUTHORIZED
    )


def test_a_callback_on_a_decided_hold_is_refused(provider: FakeProvider) -> None:
    held = hold(provider).unwrap()

    refused(
        provider.callback(
            transaction_id=held.transaction_id, outcome=PaymentStatus.FAILED
        )
    )


def test_a_callback_for_an_unknown_transaction_is_refused(
    provider: FakeProvider,
) -> None:
    refused(
        provider.callback(
            transaction_id="no-such-transaction", outcome=PaymentStatus.AUTHORIZED
        )
    )


def test_a_callback_settles_a_hold_as_authorized_or_failed_only(
    provider: FakeProvider,
) -> None:
    transaction_id = pending(provider)

    for outcome in (
        PaymentStatus.CAPTURED,
        PaymentStatus.REFUNDED,
        PaymentStatus.CANCELLED,
        PaymentStatus.PENDING,
    ):
        refused(provider.callback(transaction_id=transaction_id, outcome=outcome))

    assert (
        provider.status(transaction_id=transaction_id).unwrap().status
        is PaymentStatus.PENDING
    )


def test_the_callback_is_an_answer_like_any_other(provider: FakeProvider) -> None:
    transaction_id = pending(provider)

    settled = provider.callback(
        transaction_id=transaction_id, outcome=PaymentStatus.AUTHORIZED
    )

    assert settled.request.operation is Operation.CALLBACK
    assert settled.request.transaction_id == transaction_id
    assert settled.request.idempotency_key is None
    assert provider.calls[-1] is settled


def test_the_callback_reaches_the_audit_trail(provider: FakeProvider) -> None:
    sink = InMemoryAuditSink()
    transaction_id = pending(provider)
    settled = provider.callback(
        transaction_id=transaction_id, outcome=PaymentStatus.AUTHORIZED
    )

    record_callback(sink, settled)

    record = sink.records[0]
    assert record.origin is Origin.CALLBACK
    assert record.operation is Operation.CALLBACK
    assert record.ok
    assert record.response is not None
    assert record.response.unwrap().transaction_id == transaction_id
    assert record.duration_ms is None
