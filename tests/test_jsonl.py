"""The trail reaches a file, one readable line per exchange.

What is written has to be readable back without the process that wrote it, so
the shape of a line, the exactness of its amounts and the moment it becomes
readable are all worth holding down.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


import pytest

from payment_gateways import (
    AuditedProvider,
    AuditRecord,
    AuditSink,
    Declined,
    FakeProvider,
    JsonlAuditSink,
    Money,
    Operation,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
    record_callback,
    record_to_dict,
)


def money(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


def read(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def hold(provider: Any, *, order_id: str = "order-1", key: str = "hold-1", **kwargs: Any):
    return provider.hold(
        order_id=order_id, amount=money("40.00"), idempotency_key=key, **kwargs
    )


# --- what a line carries ---------------------------------------------------


def test_every_call_is_written_as_one_line(path: Path) -> None:
    with JsonlAuditSink(path) as sink:
        provider = AuditedProvider(FakeProvider(), sink)
        held = hold(provider).unwrap()
        provider.capture(
            transaction_id=held.transaction_id, idempotency_key="capture-1"
        )

    lines = read(path)

    assert [line["request"]["operation"] for line in lines] == ["hold", "capture"]
    assert [line["origin"] for line in lines] == ["call", "call"]
    assert all(line["ok"] for line in lines)
    assert all(line["duration_ms"] is not None for line in lines)
    assert all(line["exception"] is None for line in lines)


def test_amounts_keep_their_exact_decimal_text(path: Path) -> None:
    with JsonlAuditSink(path) as sink:
        AuditedProvider(FakeProvider(), sink).hold(
            order_id="order-1", amount=money("40.10"), idempotency_key="hold-1"
        )

    line = read(path)[0]

    assert line["request"]["amount"] == {"amount": "40.10", "currency": "EUR"}
    assert line["response"]["result"]["amount"] == {
        "amount": "40.10",
        "currency": "EUR",
    }
    assert line["response"]["result"]["status"] == "authorized"
    assert line["response"]["error"] is None


def test_a_failed_answer_records_the_error_it_came_back_with(path: Path) -> None:
    inner = FakeProvider()
    inner.fail_next(
        Operation.HOLD,
        Declined("insufficient funds", code="51", raw={"issuer": "mock"}),
    )

    with JsonlAuditSink(path) as sink:
        hold(AuditedProvider(inner, sink))

    line = read(path)[0]

    assert not line["ok"]
    assert line["response"]["result"] is None
    assert line["response"]["error"] == {
        "kind": "declined",
        "message": "insufficient funds",
        "code": "51",
        "raw": {"issuer": "mock"},
    }


def test_a_raised_exception_reaches_the_file_too(path: Path) -> None:
    class Raising:
        name = "raising"

        def hold(self, **kwargs: Any) -> ProviderResponse:
            raise RuntimeError("socket closed")

    with JsonlAuditSink(path) as sink:
        provider = AuditedProvider(Raising(), sink)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            hold(provider)

    line = read(path)[0]

    assert line["exception"] == "RuntimeError: socket closed"
    assert line["response"] is None
    assert not line["ok"]
    assert line["duration_ms"] is not None


def test_a_replay_is_marked_in_the_file(path: Path) -> None:
    with JsonlAuditSink(path) as sink:
        provider = AuditedProvider(FakeProvider(), sink)
        hold(provider)
        hold(provider)

    first, again = read(path)

    assert not first["response"]["replayed"]
    assert again["response"]["replayed"]
    assert (
        again["response"]["result"]["transaction_id"]
        == first["response"]["result"]["transaction_id"]
    )


def test_a_callback_is_written_with_its_own_origin(path: Path) -> None:
    provider = FakeProvider()
    provider.pending_next()
    held = hold(provider).unwrap()
    settled = provider.callback(
        transaction_id=held.transaction_id, outcome=PaymentStatus.AUTHORIZED
    )

    with JsonlAuditSink(path) as sink:
        record_callback(sink, settled)

    line = read(path)[0]

    assert line["origin"] == "callback"
    assert line["request"]["operation"] == "callback"
    assert line["request"]["idempotency_key"] is None
    assert line["duration_ms"] is None
    assert line["response"]["result"]["status"] == "authorized"


def test_secrets_do_not_reach_the_file(path: Path) -> None:
    with JsonlAuditSink(path) as sink:
        hold(
            AuditedProvider(FakeProvider(), sink),
            metadata={"card_number": "4111111111111111", "channel": "web"},
        )

    assert "4111111111111111" not in path.read_text(encoding="utf-8")
    assert read(path)[0]["request"]["metadata"] == {
        "card_number": "***",
        "channel": "web",
    }


def test_a_value_json_cannot_hold_is_written_as_text(path: Path) -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    with JsonlAuditSink(path) as sink:
        hold(AuditedProvider(FakeProvider(), sink), metadata={"channel": Opaque()})

    assert read(path)[0]["request"]["metadata"]["channel"] == "<opaque>"


# --- the file itself -------------------------------------------------------


def test_a_record_is_readable_before_the_sink_is_closed(path: Path) -> None:
    sink = JsonlAuditSink(path)

    hold(AuditedProvider(FakeProvider(), sink))

    assert len(read(path)) == 1
    sink.close()


def test_a_new_sink_continues_the_trail_it_finds(path: Path) -> None:
    for key in ("hold-1", "hold-2"):
        with JsonlAuditSink(path) as sink:
            hold(AuditedProvider(FakeProvider(), sink), key=key)

    assert [line["request"]["idempotency_key"] for line in read(path)] == [
        "hold-1",
        "hold-2",
    ]


def test_it_names_the_file_it_writes_to(path: Path) -> None:
    with JsonlAuditSink(str(path)) as sink:
        assert sink.path == path


def test_it_satisfies_the_audit_sink_protocol(path: Path) -> None:
    with JsonlAuditSink(path) as sink:
        assert isinstance(sink, AuditSink)


def test_a_closed_sink_takes_no_further_records(path: Path) -> None:
    sink = JsonlAuditSink(path)
    sink.close()
    sink.close()

    with pytest.raises(ValueError):
        sink.record(
            AuditRecord(
                request=ProviderRequest(
                    operation=Operation.STATUS, provider="fake", transaction_id="fake-1"
                )
            )
        )


def test_a_closed_sink_cannot_fail_a_payment(path: Path) -> None:
    sink = JsonlAuditSink(path)
    sink.close()

    response = hold(AuditedProvider(FakeProvider(), sink))

    assert response.unwrap().status is PaymentStatus.AUTHORIZED
    assert read(path) == []


# --- the shape, apart from the file ---------------------------------------


def test_a_record_renders_its_timestamps_as_text() -> None:
    moment = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    request = ProviderRequest(
        operation=Operation.STATUS,
        provider="fake",
        transaction_id="fake-1",
        created_at=moment,
    )
    response = ProviderResponse(
        request=request, error=Declined("gone"), received_at=moment
    )

    payload = record_to_dict(
        AuditRecord(
            request=request,
            response=response,
            duration_ms=1.5,
            recorded_at=moment,
        )
    )

    assert payload["recorded_at"] == "2026-05-17T12:00:00+00:00"
    assert payload["request"]["created_at"] == payload["recorded_at"]
    assert payload["response"]["received_at"] == payload["recorded_at"]
    assert payload["duration_ms"] == 1.5
    assert payload["request"]["amount"] is None
    assert payload["request"]["order_id"] is None
