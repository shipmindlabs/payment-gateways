"""The store keeps the three rules a retry under the same key depends on.

A success is replayed, a failure leaves its key free, and a key that returns
with a different request is a conflict rather than either of the two.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from payment_gateways import (
    IdempotencyConflict,
    IdempotencyStore,
    Money,
    NetworkError,
    Operation,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
    request_fingerprint,
)


def money(amount: str, currency: str = "EUR") -> Money:
    return Money(Decimal(amount), currency)


def hold_request(**overrides: Any) -> ProviderRequest:
    fields: dict[str, Any] = {
        "operation": Operation.HOLD,
        "provider": "fake",
        "idempotency_key": "hold-order-1",
        "order_id": "order-1",
        "amount": money("40.00"),
    }
    fields.update(overrides)
    return ProviderRequest(**fields)


def answer(
    request: ProviderRequest, *, transaction_id: str = "fake-1"
) -> ProviderResponse:
    return ProviderResponse(
        request=request,
        result=PaymentResult(
            transaction_id=transaction_id,
            status=PaymentStatus.AUTHORIZED,
            amount=request.amount or money("0"),
            provider=request.provider,
        ),
    )


# --- what makes two calls the same request --------------------------------


def test_metadata_and_timestamps_do_not_change_the_fingerprint() -> None:
    annotated = hold_request(
        metadata={"attempt": 2},
        created_at=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
    )

    assert request_fingerprint(annotated) == request_fingerprint(hold_request())


def test_the_key_itself_is_not_part_of_the_fingerprint() -> None:
    other = hold_request(idempotency_key="a-key-of-its-own")

    assert request_fingerprint(other) == request_fingerprint(hold_request())


def test_the_same_amount_written_differently_is_the_same_request() -> None:
    assert request_fingerprint(hold_request(amount=money("40"))) == (
        request_fingerprint(hold_request(amount=money("40.00")))
    )


def test_every_money_moving_field_changes_the_fingerprint() -> None:
    base = request_fingerprint(hold_request())

    assert request_fingerprint(hold_request(amount=money("40.01"))) != base
    assert request_fingerprint(hold_request(amount=money("40.00", "USD"))) != base
    assert request_fingerprint(hold_request(operation=Operation.CAPTURE)) != base
    assert request_fingerprint(hold_request(order_id="order-2")) != base
    assert request_fingerprint(hold_request(transaction_id="fake-1")) != base
    assert request_fingerprint(hold_request(provider="acquirer")) != base


def test_fields_cannot_be_shuffled_into_the_same_fingerprint() -> None:
    split = hold_request(order_id="a", transaction_id="b")
    joined = hold_request(order_id="ab", transaction_id=None)

    assert request_fingerprint(split) != request_fingerprint(joined)


# --- what the store answers ------------------------------------------------


def test_an_unknown_key_has_nothing_to_replay() -> None:
    assert IdempotencyStore().replay(hold_request()) is None


def test_a_read_is_neither_stored_nor_replayed() -> None:
    store = IdempotencyStore()
    read = ProviderRequest(
        operation=Operation.STATUS, provider="fake", transaction_id="fake-1"
    )

    store.record(answer(read))

    assert store.replay(read) is None


def test_the_first_success_is_replayed_for_every_repeat() -> None:
    store = IdempotencyStore()
    store.record(answer(hold_request()))

    for _ in range(3):
        replayed = store.replay(hold_request())
        assert isinstance(replayed, PaymentResult)
        assert replayed.transaction_id == "fake-1"


def test_a_retry_that_only_carries_new_metadata_still_replays() -> None:
    store = IdempotencyStore()
    store.record(answer(hold_request(metadata={"attempt": 1})))

    replayed = store.replay(hold_request(metadata={"attempt": 2}))

    assert isinstance(replayed, PaymentResult)
    assert replayed.transaction_id == "fake-1"


def test_a_failed_answer_does_not_claim_the_key() -> None:
    store = IdempotencyStore()

    store.record(
        ProviderResponse(request=hold_request(), error=NetworkError("gateway timeout"))
    )

    assert store.replay(hold_request()) is None


def test_the_key_keeps_the_answer_it_was_first_bound_to() -> None:
    store = IdempotencyStore()
    store.record(answer(hold_request(), transaction_id="fake-1"))

    store.record(answer(hold_request(), transaction_id="fake-2"))

    replayed = store.replay(hold_request())
    assert isinstance(replayed, PaymentResult)
    assert replayed.transaction_id == "fake-1"


def test_a_key_returning_with_another_request_is_a_conflict() -> None:
    store = IdempotencyStore()
    store.record(answer(hold_request()))

    conflict = store.replay(hold_request(amount=money("50.00")))

    assert isinstance(conflict, IdempotencyConflict)
    assert not conflict.retryable
    assert "hold-order-1" in conflict.message
    assert conflict.raw == {"transaction_id": "fake-1"}


def test_a_conflict_leaves_the_stored_answer_in_place() -> None:
    store = IdempotencyStore()
    store.record(answer(hold_request()))

    assert isinstance(
        store.replay(hold_request(amount=money("50.00"))), IdempotencyConflict
    )

    replayed = store.replay(hold_request())
    assert isinstance(replayed, PaymentResult)
    assert replayed.transaction_id == "fake-1"


def test_keys_do_not_leak_into_one_another() -> None:
    store = IdempotencyStore()
    first = hold_request(idempotency_key="hold-1")
    second = hold_request(idempotency_key="hold-2", order_id="order-2")
    store.record(answer(first, transaction_id="fake-1"))
    store.record(answer(second, transaction_id="fake-2"))

    replayed_first = store.replay(first)
    replayed_second = store.replay(second)

    assert isinstance(replayed_first, PaymentResult)
    assert isinstance(replayed_second, PaymentResult)
    assert replayed_first.transaction_id == "fake-1"
    assert replayed_second.transaction_id == "fake-2"


def test_two_stores_share_nothing() -> None:
    first = IdempotencyStore()
    second = IdempotencyStore()
    first.record(answer(hold_request()))

    assert second.replay(hold_request()) is None
