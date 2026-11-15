"""Failure modes of the two wrappers, and of the redaction they rely on.

What is exercised here is the part that only shows up when something goes
wrong: a sink that is down while a provider raises, a payload too deep to walk,
a key that only looks like a secret, an answer lost on its way back, and the
two orders the wrappers can be composed in.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from payment_gateways import (
    REDACTED,
    AuditedProvider,
    AuditRecord,
    Declined,
    FakeProvider,
    InMemoryAuditSink,
    Money,
    NetworkError,
    Operation,
    Origin,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
    RetryingProvider,
    record_callback,
    redact,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


class RaisingSink:
    """A sink that is down, the way a database or a queue can be."""

    def __init__(self) -> None:
        self.attempts = 0

    def record(self, record: AuditRecord) -> None:
        self.attempts += 1
        raise RuntimeError("sink is down")


class RaisingProvider:
    """A provider that breaks its side of the protocol by raising."""

    name = "raising"

    def __init__(self) -> None:
        self.attempts = 0

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        self.attempts += 1
        raise RuntimeError("acquirer gone")


class LosesTheAnswer:
    """A provider that executes the hold and then loses the answer once."""

    def __init__(self, inner: FakeProvider) -> None:
        self._inner = inner
        self._lose = 1
        self.name = inner.name

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        response = self._inner.hold(
            order_id=order_id,
            amount=amount,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
        if self._lose:
            self._lose -= 1
            return ProviderResponse(
                request=response.request,
                error=NetworkError("answer lost on the way back"),
            )
        return response


def nest(depth: int, leaf: Any) -> Any:
    value: Any = leaf
    for _ in range(depth):
        value = {"inner": value}
    return value


def hold(provider: Any, *, key: str = "hold-order-1", order_id: str = "order-1") -> Any:
    return provider.hold(
        order_id=order_id, amount=money("40.00"), idempotency_key=key
    )


# --- redaction edge cases -------------------------------------------------


def test_redaction_stops_at_a_depth_limit() -> None:
    shallow = nest(8, "visible")
    assert redact(shallow) == shallow

    cursor: Any = redact(nest(9, "visible"))
    for _ in range(9):
        cursor = cursor["inner"]
    assert cursor == REDACTED


def test_a_key_is_secret_by_its_words_not_by_chance() -> None:
    payload: dict[Any, Any] = {
        "Card-Number": "4111111111111111",
        "x-api-key": "k_live_1",
        "payment.token": "tok_1",
        "CVV": "123",
        "panel": "admin",
        "last4": "1111",
        "channel": "web",
        7: "seven",
    }

    assert redact(payload) == {
        "Card-Number": REDACTED,
        "x-api-key": REDACTED,
        "payment.token": REDACTED,
        "CVV": REDACTED,
        "panel": "admin",
        "last4": "1111",
        "channel": "web",
        7: "seven",
    }


def test_redaction_leaves_a_sequence_as_a_list_and_a_scalar_alone() -> None:
    assert redact({"attempts": ({"token": "tok_1"}, {"trace": "t-1"})}) == {
        "attempts": [{"token": REDACTED}, {"trace": "t-1"}]
    }
    assert redact("4111111111111111") == "4111111111111111"
    assert redact(None) is None
    assert redact(7) == 7


def test_a_secret_in_a_callback_payload_is_replaced() -> None:
    sink = InMemoryAuditSink()
    response = ProviderResponse(
        request=ProviderRequest(
            operation=Operation.CALLBACK,
            provider="scripted",
            transaction_id="scripted-1",
            metadata={"signature": "sig_1", "event": "charge.updated"},
        ),
        result=PaymentResult(
            transaction_id="scripted-1",
            status=PaymentStatus.AUTHORIZED,
            amount=money("40.00"),
            provider="scripted",
        ),
    )

    record_callback(sink, response)

    record = sink.records[0]
    assert record.origin is Origin.CALLBACK
    assert record.request.metadata == {"signature": REDACTED, "event": "charge.updated"}


# --- a failing sink -------------------------------------------------------


def test_a_failing_sink_does_not_replace_a_provider_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = RaisingSink()
    provider = AuditedProvider(RaisingProvider(), sink)

    with caplog.at_level(logging.WARNING, logger="payment_gateways.audit"):
        with pytest.raises(RuntimeError, match="acquirer gone"):
            hold(provider)

    assert sink.attempts == 1
    assert "audit sink failed" in caplog.text


def test_a_failing_sink_cannot_fail_a_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = RaisingSink()
    inner = FakeProvider()
    inner.pending_next()
    held = hold(inner).unwrap()
    settled = inner.callback(
        transaction_id=held.transaction_id, outcome=PaymentStatus.AUTHORIZED
    )

    with caplog.at_level(logging.WARNING, logger="payment_gateways.audit"):
        record_callback(sink, settled)

    assert sink.attempts == 1
    assert settled.unwrap().status is PaymentStatus.AUTHORIZED


# --- what the trail holds for a failure and a replay ----------------------


def test_a_refused_call_is_recorded_with_its_error() -> None:
    sink = InMemoryAuditSink()
    inner = FakeProvider()
    provider = AuditedProvider(inner, sink)
    inner.fail_next(Operation.HOLD, Declined("issuer refused", code="51"))

    assert not hold(provider).ok

    record = sink.records[0]
    assert not record.ok
    assert record.error is not None
    assert record.error.code == "51"
    assert record.exception is None
    assert record.response is not None
    assert record.duration_ms is not None
    assert record.recorded_at.tzinfo is not None


def test_a_replayed_answer_is_recorded_as_a_replay() -> None:
    sink = InMemoryAuditSink()
    provider = AuditedProvider(FakeProvider(), sink)

    hold(provider)
    hold(provider)

    first, again = sink.records
    assert first.response is not None and not first.response.replayed
    assert again.response is not None and again.response.replayed


def test_every_operation_lands_in_the_trail_under_its_own_name() -> None:
    sink = InMemoryAuditSink()
    provider = AuditedProvider(FakeProvider(), sink)
    held = hold(provider, key="hold-1").unwrap()
    provider.capture(
        transaction_id=held.transaction_id,
        idempotency_key="capture-1",
        amount=money("30.00"),
    )
    provider.partial_refund(
        transaction_id=held.transaction_id,
        amount=money("10.00"),
        idempotency_key="partial-1",
    )
    provider.refund(transaction_id=held.transaction_id, idempotency_key="refund-1")
    provider.status(transaction_id=held.transaction_id)
    second = hold(provider, key="hold-2", order_id="order-2").unwrap()
    provider.cancel(transaction_id=second.transaction_id, idempotency_key="cancel-1")

    assert [record.operation for record in sink.records] == [
        Operation.HOLD,
        Operation.CAPTURE,
        Operation.PARTIAL_REFUND,
        Operation.REFUND,
        Operation.STATUS,
        Operation.HOLD,
        Operation.CANCEL,
    ]
    assert all(record.origin is Origin.CALL for record in sink.records)
    assert sink.records[1].request.amount == money("30.00")
    assert sink.records[4].request.idempotency_key is None


def test_the_in_memory_sink_hands_out_a_snapshot_of_its_records() -> None:
    sink = InMemoryAuditSink()
    provider = AuditedProvider(FakeProvider(), sink)
    hold(provider, key="hold-1")

    records = sink.records
    snapshot = list(sink)
    hold(provider, key="hold-2", order_id="order-2")

    assert len(records) == 1
    assert len(snapshot) == 1
    assert len(sink) == 2

    sink.clear()
    assert len(sink) == 0
    assert sink.records == ()


# --- retrying around, and inside, the trail -------------------------------


def test_a_raised_exception_is_not_retried() -> None:
    inner = RaisingProvider()
    waits: list[float] = []
    provider = RetryingProvider(inner, attempts=3, backoff=0, sleep=waits.append)

    with pytest.raises(RuntimeError, match="acquirer gone"):
        hold(provider)

    assert inner.attempts == 1
    assert waits == []


def test_a_retry_replays_the_answer_the_lost_one_already_produced() -> None:
    inner = FakeProvider()
    waits: list[float] = []
    provider = RetryingProvider(
        LosesTheAnswer(inner), attempts=3, backoff=0, sleep=waits.append
    )

    response = hold(provider)

    assert response.ok
    assert response.replayed
    assert waits == [0]
    assert len(inner.calls) == 2
    assert inner.calls[0].unwrap().transaction_id == response.unwrap().transaction_id


def test_retrying_outside_the_trail_records_every_attempt() -> None:
    sink = InMemoryAuditSink()
    inner = FakeProvider()
    provider = RetryingProvider(
        AuditedProvider(inner, sink), attempts=3, backoff=0, sleep=lambda _: None
    )
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    assert hold(provider).ok
    assert [record.ok for record in sink.records] == [False, True]


def test_retrying_inside_the_trail_records_only_the_answer() -> None:
    sink = InMemoryAuditSink()
    inner = FakeProvider()
    provider = AuditedProvider(
        RetryingProvider(inner, attempts=3, backoff=0, sleep=lambda _: None), sink
    )
    inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    assert hold(provider).ok
    assert len(sink.records) == 1
    assert sink.records[0].ok
    assert len(inner.calls) == 2


def test_a_multiplier_of_one_waits_the_same_every_time() -> None:
    inner = FakeProvider()
    waits: list[float] = []
    provider = RetryingProvider(
        inner, attempts=3, backoff=0.05, multiplier=1.0, sleep=waits.append
    )
    for _ in range(3):
        inner.fail_next(Operation.HOLD, NetworkError("gateway timeout"))

    assert not hold(provider).ok
    assert waits == pytest.approx([0.05, 0.05])
