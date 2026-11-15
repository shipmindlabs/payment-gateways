"""The value objects refuse the states a trail could not be read back from."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from payment_gateways import (
    Declined,
    ErrorKind,
    IdempotencyConflict,
    InvalidState,
    Money,
    NetworkError,
    Operation,
    PaymentFailed,
    PaymentResult,
    PaymentStatus,
    ProviderRequest,
    ProviderResponse,
)


def money(amount: str, currency: str = "EUR") -> Money:
    return Money(Decimal(amount), currency)


def request() -> ProviderRequest:
    return ProviderRequest(
        operation=Operation.HOLD,
        provider="fake",
        idempotency_key="hold-order-1",
        order_id="order-1",
        amount=money("40.00"),
    )


def result() -> PaymentResult:
    return PaymentResult(
        transaction_id="fake-1",
        status=PaymentStatus.AUTHORIZED,
        amount=money("40.00"),
        provider="fake",
    )


# --- Money ----------------------------------------------------------------


def test_money_refuses_an_amount_that_is_not_a_decimal() -> None:
    for amount in (40.0, 40, "40.00", None):
        with pytest.raises(TypeError):
            Money(amount, "EUR")  # type: ignore[arg-type]


def test_money_refuses_a_non_finite_amount() -> None:
    for amount in ("NaN", "-NaN", "sNaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            Money(Decimal(amount), "EUR")


def test_money_refuses_anything_but_a_three_letter_code() -> None:
    for currency in ("", "E", "EU", "EURO", "E1R", "12 ", "eu r"):
        with pytest.raises(ValueError):
            Money(Decimal("40.00"), currency)


def test_money_normalises_the_currency_to_upper_case() -> None:
    assert Money(Decimal("40.00"), "eur").currency == "EUR"
    assert Money(Decimal("40.00"), "eUr") == money("40.00")


def test_money_cannot_be_edited_after_the_fact() -> None:
    amount = money("40.00")
    with pytest.raises(FrozenInstanceError):
        amount.currency = "USD"  # type: ignore[misc]


def test_money_compares_by_value_across_trailing_zeros() -> None:
    assert money("40.0") == money("40.00")
    assert len({money("40.0"), money("40.00")}) == 1
    assert money("40.00") != money("40.00", "USD")
    assert money("40.00") != money("40.01")


def test_money_prints_the_amount_with_its_currency() -> None:
    assert str(money("40.00")) == "40.00 EUR"


# --- requests and responses ------------------------------------------------


def test_a_blank_idempotency_key_is_refused() -> None:
    for key in ("", " ", "\t\n"):
        with pytest.raises(ValueError):
            ProviderRequest(
                operation=Operation.HOLD, provider="fake", idempotency_key=key
            )


def test_a_read_may_carry_no_key_at_all() -> None:
    read = ProviderRequest(
        operation=Operation.STATUS, provider="fake", transaction_id="fake-1"
    )

    assert read.idempotency_key is None
    assert read.metadata == {}
    assert read.amount is None


def test_a_response_without_an_outcome_is_refused() -> None:
    with pytest.raises(ValueError):
        ProviderResponse(request=request())


def test_a_response_with_both_outcomes_is_refused() -> None:
    with pytest.raises(ValueError):
        ProviderResponse(
            request=request(), result=result(), error=Declined("issuer refused")
        )


def test_replayed_is_false_unless_the_provider_flagged_it() -> None:
    plain = ProviderResponse(request=request(), result=result())
    flagged = ProviderResponse(
        request=request(), result=result(), raw={"replayed": True}
    )
    denied = ProviderResponse(
        request=request(), result=result(), raw={"replayed": False}
    )

    assert not plain.replayed
    assert flagged.replayed
    assert not denied.replayed


def test_unwrap_hands_back_the_result_it_was_given() -> None:
    response = ProviderResponse(request=request(), result=result())

    assert response.ok
    assert response.unwrap() is response.result


def test_unwrap_raises_the_error_it_was_given() -> None:
    error = Declined("insufficient funds", code="51")
    response = ProviderResponse(request=request(), error=error)

    assert not response.ok
    with pytest.raises(PaymentFailed) as raised:
        response.unwrap()
    assert raised.value.error is error
    assert str(raised.value) == "declined [51]: insufficient funds"


# --- errors ---------------------------------------------------------------


def test_only_a_network_error_is_worth_resending() -> None:
    assert NetworkError("gateway timeout").retryable
    assert not Declined("insufficient funds").retryable
    assert not InvalidState("already captured").retryable
    assert not IdempotencyConflict("key reused").retryable


def test_every_error_names_its_kind() -> None:
    assert Declined("x").kind is ErrorKind.DECLINED
    assert NetworkError("x").kind is ErrorKind.NETWORK
    assert InvalidState("x").kind is ErrorKind.INVALID_STATE
    assert IdempotencyConflict("x").kind is ErrorKind.IDEMPOTENCY_CONFLICT


def test_an_error_prints_its_own_code_when_it_has_one() -> None:
    assert str(NetworkError("gateway timeout")) == "network: gateway timeout"
    assert (
        str(NetworkError("gateway timeout", code="E7"))
        == "network [E7]: gateway timeout"
    )


def test_an_error_carries_an_empty_payload_by_default() -> None:
    error = Declined("issuer refused")

    assert error.code is None
    assert error.raw == {}
    with pytest.raises(FrozenInstanceError):
        error.message = "something else"  # type: ignore[misc]
