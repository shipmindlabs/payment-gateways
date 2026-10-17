"""A retry resends only what may still succeed, under the key that failed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_gateways import (
    Declined,
    FakeProvider,
    Money,
    NetworkError,
    Operation,
    PaymentStatus,
    RetryingProvider,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


@pytest.fixture
def sleeps() -> list[float]:
    return []


def retrying(
    inner: FakeProvider, sleeps: list[float], **kwargs: object
) -> RetryingProvider:
    return RetryingProvider(inner, backoff=0, sleep=sleeps.append, **kwargs)  # type: ignore[arg-type]


def hold(provider: object, *, order_id: str = "order-1", key: str = "hold-order-1"):
    return provider.hold(  # type: ignore[attr-defined]
        order_id=order_id, amount=money("40.00"), idempotency_key=key
    )


def test_a_retryable_failure_is_resent_and_succeeds(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    response = hold(provider)

    assert response.ok
    assert response.unwrap().status is PaymentStatus.AUTHORIZED
    assert len(inner.calls) == 2
    assert len(sleeps) == 1


def test_every_attempt_carries_the_key_it_started_with(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout again"))

    assert hold(provider, key="hold-order-1").ok
    assert [call.request.idempotency_key for call in inner.calls] == [
        "hold-order-1"
    ] * 3


def test_it_gives_up_after_the_last_attempt_with_the_last_error(
    sleeps: list[float],
) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)
    for code in ("N1", "N2", "N3"):
        inner.fail_next(Operation.HOLD, NetworkError("gateway timeout", code=code))

    response = hold(provider)

    assert isinstance(response.error, NetworkError)
    assert response.error.code == "N3"
    assert len(inner.calls) == 3
    assert len(sleeps) == 2


def test_a_single_attempt_never_resends(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=1)
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    assert not hold(provider).ok
    assert len(inner.calls) == 1
    assert sleeps == []


def test_a_successful_call_is_not_waited_on(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)

    assert hold(provider).ok
    assert len(inner.calls) == 1
    assert sleeps == []


def test_a_decline_is_never_resent(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)
    inner.fail_next(Operation.HOLD, Declined("insufficient funds", code="51"))

    response = hold(provider)

    assert isinstance(response.error, Declined)
    assert len(inner.calls) == 1
    assert sleeps == []


def test_an_invalid_state_is_never_resent(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)

    response = provider.capture(
        transaction_id="no-such-transaction", idempotency_key="capture-1"
    )

    assert not response.ok
    assert len(inner.calls) == 1
    assert sleeps == []


def test_an_idempotency_conflict_is_never_resent(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=3)
    assert hold(provider, key="hold-order-1").ok

    conflict = provider.hold(
        order_id="order-1",
        amount=money("50.00"),
        idempotency_key="hold-order-1",
    )

    assert not conflict.ok
    assert len(inner.calls) == 2
    assert sleeps == []


def test_the_wait_grows_and_stops_at_the_cap() -> None:
    inner = FakeProvider()
    waits: list[float] = []
    provider = RetryingProvider(
        inner,
        attempts=4,
        backoff=0.1,
        multiplier=2.0,
        max_backoff=0.25,
        sleep=waits.append,
    )
    for _ in range(4):
        inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    assert not hold(provider).ok
    assert waits == pytest.approx([0.1, 0.2, 0.25])


def test_each_operation_resends_a_retryable_failure(sleeps: list[float]) -> None:
    inner = FakeProvider()
    provider = retrying(inner, sleeps, attempts=2)
    first = hold(provider, key="hold-1").unwrap().transaction_id

    inner.fail_next(Operation.CAPTURE, NetworkError("gateway timeout"))
    assert provider.capture(
        transaction_id=first, idempotency_key="capture-1", amount=money("30.00")
    ).ok

    inner.fail_next(Operation.PARTIAL_REFUND, NetworkError("gateway timeout"))
    assert provider.partial_refund(
        transaction_id=first, amount=money("10.00"), idempotency_key="partial-1"
    ).ok

    inner.fail_next(Operation.REFUND, NetworkError("gateway timeout"))
    assert provider.refund(transaction_id=first, idempotency_key="refund-1").ok

    inner.fail_next(Operation.STATUS, NetworkError("gateway timeout"))
    assert provider.status(transaction_id=first).ok

    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))
    second = hold(provider, order_id="order-2", key="hold-2").unwrap()

    inner.fail_next(Operation.CANCEL, NetworkError("gateway timeout"))
    assert provider.cancel(
        transaction_id=second.transaction_id, idempotency_key="cancel-1"
    ).ok

    assert len(sleeps) == 6


def test_it_answers_to_the_name_of_the_provider_it_wraps() -> None:
    inner = FakeProvider(name="acquirer")
    provider = RetryingProvider(inner, sleep=lambda _: None)

    assert provider.name == "acquirer"
    assert provider.provider is inner


def test_a_policy_that_cannot_try_is_refused() -> None:
    with pytest.raises(ValueError):
        RetryingProvider(FakeProvider(), attempts=0)
    with pytest.raises(ValueError):
        RetryingProvider(FakeProvider(), backoff=-1.0)
