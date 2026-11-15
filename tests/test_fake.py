"""The reference provider fails where a test tells it to, and nowhere else.

The scripted outcomes are what lets decline, network and pending handling be
exercised without an acquirer, so the rules around them — order, reach, and
what a stored key does to them — are worth holding down.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from payment_gateways import (
    Declined,
    ErrorKind,
    FakeProvider,
    Money,
    NetworkError,
    Operation,
    PaymentStatus,
    ProviderResponse,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


def hold(
    provider: FakeProvider,
    *,
    order_id: str = "order-1",
    key: str = "hold-order-1",
    amount: str = "40.00",
    metadata: dict[str, object] | None = None,
) -> ProviderResponse:
    return provider.hold(
        order_id=order_id,
        amount=money(amount),
        idempotency_key=key,
        metadata=metadata,
    )


# --- scripted failures ----------------------------------------------------


def test_queued_failures_come_back_in_the_order_they_were_queued() -> None:
    provider = FakeProvider()
    provider.fail_next(Operation.HOLD, NetworkError("gateway timeout", code="N1"))
    provider.fail_next(Operation.HOLD, Declined("insufficient funds", code="D2"))

    first = hold(provider, key="hold-1")
    second = hold(provider, key="hold-2", order_id="order-2")
    third = hold(provider, key="hold-3", order_id="order-3")

    assert isinstance(first.error, NetworkError)
    assert first.error.code == "N1"
    assert isinstance(second.error, Declined)
    assert second.error.code == "D2"
    assert third.ok


def test_a_queued_failure_belongs_to_one_operation_only() -> None:
    provider = FakeProvider()
    held = hold(provider, key="hold-1").unwrap()
    provider.fail_next(Operation.REFUND, Declined("refund window closed", code="R12"))

    assert provider.capture(
        transaction_id=held.transaction_id, idempotency_key="capture-1"
    ).ok
    refund = provider.refund(
        transaction_id=held.transaction_id, idempotency_key="refund-1"
    )

    assert isinstance(refund.error, Declined)
    assert refund.error.code == "R12"


def test_a_queued_failure_leaves_the_transaction_and_its_key_untouched() -> None:
    provider = FakeProvider()
    held = hold(provider, key="hold-1").unwrap()
    provider.fail_next(Operation.CAPTURE, NetworkError("gateway timeout"))

    failed = provider.capture(
        transaction_id=held.transaction_id, idempotency_key="capture-1"
    )
    current = provider.status(transaction_id=held.transaction_id).unwrap()

    assert not failed.ok
    assert current.status is PaymentStatus.AUTHORIZED
    assert current.amount == money("40.00")

    retried = provider.capture(
        transaction_id=held.transaction_id, idempotency_key="capture-1"
    ).unwrap()
    assert retried.status is PaymentStatus.CAPTURED


def test_a_stored_key_wins_over_a_queued_failure() -> None:
    provider = FakeProvider()
    first = hold(provider, key="hold-1").unwrap()
    provider.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    replay = hold(provider, key="hold-1")

    assert replay.replayed
    assert replay.unwrap().transaction_id == first.transaction_id

    # The queued failure was not spent on the replay; it waits for a real call.
    fresh = hold(provider, key="hold-2", order_id="order-2")
    assert isinstance(fresh.error, NetworkError)


def test_a_conflict_is_answered_before_a_queued_failure() -> None:
    provider = FakeProvider()
    hold(provider, key="hold-1").unwrap()
    provider.fail_next(Operation.HOLD, Declined("insufficient funds"))

    conflict = hold(provider, key="hold-1", amount="50.00")

    assert conflict.error is not None
    assert conflict.error.kind is ErrorKind.IDEMPOTENCY_CONFLICT

    still_queued = hold(provider, key="hold-2", order_id="order-2")
    assert isinstance(still_queued.error, Declined)


# --- pending holds --------------------------------------------------------


def test_pending_next_leaves_one_hold_pending_per_call() -> None:
    provider = FakeProvider()
    provider.pending_next()
    provider.pending_next()

    statuses = [
        hold(provider, order_id=f"order-{n}", key=f"hold-{n}").unwrap().status
        for n in (1, 2, 3)
    ]

    assert statuses == [
        PaymentStatus.PENDING,
        PaymentStatus.PENDING,
        PaymentStatus.AUTHORIZED,
    ]


def test_a_refused_hold_does_not_consume_the_pending_mark() -> None:
    provider = FakeProvider()
    provider.pending_next()

    refused = hold(provider, key="hold-1", amount="0")

    assert refused.error is not None
    assert refused.error.kind is ErrorKind.INVALID_STATE
    assert (
        hold(provider, key="hold-2", order_id="order-2").unwrap().status
        is PaymentStatus.PENDING
    )


# --- what every answer carries --------------------------------------------


def test_every_answer_reaches_the_call_log() -> None:
    provider = FakeProvider()
    provider.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    hold(provider, key="hold-1")
    hold(provider, key="hold-1")
    hold(provider, key="hold-1")
    hold(provider, key="hold-1", amount="50.00")

    kinds = [None if call.ok else call.error.kind for call in provider.calls]  # type: ignore[union-attr]
    assert kinds == [ErrorKind.NETWORK, None, None, ErrorKind.IDEMPOTENCY_CONFLICT]
    assert not provider.calls[1].replayed
    assert provider.calls[2].replayed


def test_the_name_lands_on_every_request_and_result() -> None:
    provider = FakeProvider(name="acquirer")

    response = hold(provider, key="hold-1")

    assert response.request.provider == "acquirer"
    assert response.unwrap().provider == "acquirer"
    assert response.unwrap().transaction_id.startswith("acquirer-")


def test_the_clock_is_injectable() -> None:
    moment = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    provider = FakeProvider(clock=lambda: moment)

    response = hold(provider, key="hold-1")

    assert response.request.created_at == moment
    assert response.received_at == moment
    assert response.unwrap().created_at == moment


def test_metadata_is_copied_into_the_recorded_request() -> None:
    provider = FakeProvider()
    metadata: dict[str, object] = {"channel": "web"}

    response = hold(provider, key="hold-1", metadata=metadata)
    metadata["channel"] = "pos"

    assert response.request.metadata == {"channel": "web"}


def test_a_blank_idempotency_key_never_reaches_the_provider() -> None:
    provider = FakeProvider()

    with pytest.raises(ValueError):
        hold(provider, key="   ")

    assert provider.calls == []
