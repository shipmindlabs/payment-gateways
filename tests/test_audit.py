"""The trail keeps the three rules it promises.

Secrets are replaced before a record is handed over, a sink that raises is
logged instead of failing the payment, and a provider that raises leaves the
exception behind on its way up.
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
    Operation,
    Origin,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
    record_callback,
    redact,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


class ScriptedProvider:
    """A provider that answers with the response a test wrote, or raises it."""

    name = "scripted"

    def __init__(self, answer: ProviderResponse | Exception) -> None:
        self._answer = answer

    def hold(
        self,
        *,
        order_id: str,
        amount: Money,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class RaisingSink:
    """A sink that is down, the way a database or a queue can be."""

    def __init__(self) -> None:
        self.attempts = 0

    def record(self, record: AuditRecord) -> None:
        self.attempts += 1
        raise RuntimeError("sink is down")


# --- secrets never reach the sink ----------------------------------------


def test_a_nested_secret_in_metadata_is_replaced() -> None:
    sink = InMemoryAuditSink()
    provider = AuditedProvider(FakeProvider(), sink)
    metadata = {"payer": {"card_number": "4111111111111111", "name": "Ada"}}

    provider.hold(
        order_id="order-1",
        amount=money("40.00"),
        idempotency_key="hold-order-1",
        metadata=metadata,
    )

    assert sink.records[0].request.metadata == {
        "payer": {"card_number": REDACTED, "name": "Ada"}
    }
    assert metadata["payer"]["card_number"] == "4111111111111111"


def test_a_nested_secret_in_a_result_payload_is_replaced() -> None:
    sink = InMemoryAuditSink()
    answer = ProviderResponse(
        request=ProviderRequest(
            operation=Operation.HOLD,
            provider="scripted",
            idempotency_key="hold-order-1",
            order_id="order-1",
            amount=money("40.00"),
        ),
        result=PaymentResult(
            transaction_id="scripted-1",
            status=PaymentStatus.AUTHORIZED,
            amount=money("40.00"),
            provider="scripted",
            raw={"acquirer": {"token": "tok_live_1", "scheme": "visa"}},
        ),
        raw={"headers": {"Authorization": "Bearer abc", "trace": "t-1"}},
    )
    provider = AuditedProvider(ScriptedProvider(answer), sink)

    response = provider.hold(
        order_id="order-1", amount=money("40.00"), idempotency_key="hold-order-1"
    )

    recorded = sink.records[0].response
    assert recorded is not None
    assert recorded.result is not None
    assert recorded.result.raw == {"acquirer": {"token": REDACTED, "scheme": "visa"}}
    assert recorded.raw == {"headers": {"Authorization": REDACTED, "trace": "t-1"}}
    assert response.unwrap().raw["acquirer"]["token"] == "tok_live_1"


def test_a_nested_secret_in_an_error_payload_is_replaced() -> None:
    sink = InMemoryAuditSink()
    inner = FakeProvider()
    provider = AuditedProvider(inner, sink)
    inner.fail_next(
        Operation.HOLD,
        Declined(
            "issuer refused",
            code="51",
            raw={"issuer": {"card_number": "4111111111111111", "reason": "limit"}},
        ),
    )

    response = provider.hold(
        order_id="order-1", amount=money("40.00"), idempotency_key="hold-order-1"
    )

    recorded = sink.records[0].error
    assert recorded is not None
    assert recorded.raw == {"issuer": {"card_number": REDACTED, "reason": "limit"}}
    assert response.error is not None
    assert response.error.raw["issuer"]["card_number"] == "4111111111111111"


def test_redaction_reaches_into_lists_as_well_as_mappings() -> None:
    payload = {"cards": [{"cvv": "123", "last4": "1111"}], "channel": "web"}

    assert redact(payload) == {
        "cards": [{"cvv": REDACTED, "last4": "1111"}],
        "channel": "web",
    }


# --- a failing sink cannot fail a payment ---------------------------------


def test_a_failing_sink_is_logged_and_the_payment_still_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = RaisingSink()
    provider = AuditedProvider(FakeProvider(), sink)

    with caplog.at_level(logging.WARNING, logger="payment_gateways.audit"):
        response = provider.hold(
            order_id="order-1", amount=money("40.00"), idempotency_key="hold-order-1"
        )

    assert response.unwrap().status is PaymentStatus.AUTHORIZED
    assert sink.attempts == 1
    assert "audit sink failed" in caplog.text
    assert caplog.records[-1].exc_info is not None


# --- unexpected exceptions are recorded too -------------------------------


def test_a_raising_provider_leaves_a_record_and_still_raises() -> None:
    sink = InMemoryAuditSink()
    provider = AuditedProvider(ScriptedProvider(RuntimeError("acquirer gone")), sink)

    with pytest.raises(RuntimeError, match="acquirer gone"):
        provider.hold(
            order_id="order-1",
            amount=money("40.00"),
            idempotency_key="hold-order-1",
            metadata={"card_number": "4111111111111111"},
        )

    record = sink.records[0]
    assert record.exception == "RuntimeError: acquirer gone"
    assert record.response is None
    assert not record.ok
    assert record.error is None
    assert record.operation is Operation.HOLD
    assert record.duration_ms is not None
    assert record.request.metadata == {"card_number": REDACTED}


# --- callbacks join the same trail ----------------------------------------


def test_a_recorded_callback_is_tagged_as_a_callback() -> None:
    sink = InMemoryAuditSink()
    inner = FakeProvider()
    provider = AuditedProvider(inner, sink)
    inner.pending_next()
    held = provider.hold(
        order_id="order-1", amount=money("40.00"), idempotency_key="hold-order-1"
    ).unwrap()

    settled = inner.callback(
        transaction_id=held.transaction_id, outcome=PaymentStatus.AUTHORIZED
    )
    record_callback(sink, settled)

    call, callback = sink.records
    assert call.origin is Origin.CALL
    assert call.operation is Operation.HOLD
    assert callback.origin is Origin.CALLBACK
    assert callback.operation is Operation.CALLBACK
    assert callback.request.transaction_id == held.transaction_id
    assert callback.request.idempotency_key is None
    assert callback.duration_ms is None
    assert callback.ok
