"""A contract test suite a provider can run against itself.

`PaymentProvider` fixes the signatures; the promises around them — a capture
settles a hold once, a repeated key replays instead of charging again, an
operation that does not apply to the current state is refused rather than
obeyed — live in prose no type checker reads. This module turns those promises
into tests: subclass `PaymentProviderContract`, return the provider under test
from `make_provider()`, and any runner that collects `test_*` methods runs the
whole suite against it.

The suite reaches the provider only through the protocol, so it assumes nothing
about how one is built, and asserts with plain `assert`, so it pulls in no test
framework of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from payment_gateways.errors import ErrorKind, PaymentFailed, ProviderError
from payment_gateways.models import (
    Operation,
    PaymentResult,
    PaymentStatus,
    ProviderResponse,
)
from payment_gateways.money import Money
from payment_gateways.protocol import PaymentProvider

__all__ = ["PaymentProviderContract"]


class PaymentProviderContract:
    """The lifecycle, idempotency and refusal rules every provider owes callers."""

    currency = "EUR"
    foreign_currency = "USD"

    def make_provider(self) -> PaymentProvider:
        """Return a provider that holds no transactions yet."""
        raise NotImplementedError("a contract subclass returns the provider to test")

    # --- helpers ---------------------------------------------------------

    def money(self, amount: str) -> Money:
        return Money(Decimal(amount), self.currency)

    def key(self, label: str) -> str:
        counter = getattr(self, "_key_counter", 0) + 1
        self._key_counter = counter
        return f"{label}-{counter}"

    def _hold(
        self,
        provider: PaymentProvider,
        *,
        amount: str = "40.00",
        order_id: str = "order-1",
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        return provider.hold(
            order_id=order_id,
            amount=self.money(amount),
            idempotency_key=idempotency_key or self.key("hold"),
            metadata=metadata,
        )

    def _held(
        self, provider: PaymentProvider, *, amount: str = "40.00"
    ) -> PaymentResult:
        return self._hold(provider, amount=amount).unwrap()

    def _captured(
        self,
        provider: PaymentProvider,
        *,
        held: str = "40.00",
        amount: str | None = None,
    ) -> PaymentResult:
        hold = self._held(provider, amount=held)
        return provider.capture(
            transaction_id=hold.transaction_id,
            idempotency_key=self.key("capture"),
            amount=None if amount is None else self.money(amount),
        ).unwrap()

    def _status(self, provider: PaymentProvider, transaction_id: str) -> PaymentResult:
        return provider.status(transaction_id=transaction_id).unwrap()

    def _refused(self, response: ProviderResponse, kind: ErrorKind) -> ProviderError:
        assert not response.ok
        assert response.result is None
        assert response.error is not None
        assert response.error.kind is kind
        return response.error

    def _invalid(self, response: ProviderResponse) -> ProviderError:
        return self._refused(response, ErrorKind.INVALID_STATE)

    def _conflict(self, response: ProviderResponse) -> ProviderError:
        return self._refused(response, ErrorKind.IDEMPOTENCY_CONFLICT)

    # --- shape of every answer -------------------------------------------

    def test_provider_satisfies_the_protocol(self) -> None:
        assert isinstance(self.make_provider(), PaymentProvider)

    def test_provider_answers_to_a_name(self) -> None:
        provider = self.make_provider()
        assert isinstance(provider.name, str)
        assert provider.name

    def test_a_response_carries_a_result_or_an_error_but_never_both(self) -> None:
        provider = self.make_provider()
        succeeded = self._hold(provider)
        assert succeeded.ok
        assert succeeded.result is not None
        assert succeeded.error is None
        failed = provider.status(transaction_id="no-such-transaction")
        assert not failed.ok
        assert failed.result is None
        assert failed.error is not None

    def test_a_response_echoes_the_request_it_answers(self) -> None:
        provider = self.make_provider()
        key = self.key("hold")
        amount = self.money("40.00")
        response = provider.hold(
            order_id="order-1", amount=amount, idempotency_key=key
        )
        request = response.request
        assert request.operation is Operation.HOLD
        assert request.provider == provider.name
        assert request.order_id == "order-1"
        assert request.amount == amount
        assert request.idempotency_key == key

    def test_every_money_moving_operation_records_its_key(self) -> None:
        provider = self.make_provider()
        hold = self._held(provider)
        capture_key = self.key("capture")
        capture = provider.capture(
            transaction_id=hold.transaction_id, idempotency_key=capture_key
        )
        refund_key = self.key("refund")
        refund = provider.refund(
            transaction_id=hold.transaction_id, idempotency_key=refund_key
        )
        assert capture.request.idempotency_key == capture_key
        assert capture.request.transaction_id == hold.transaction_id
        assert refund.request.idempotency_key == refund_key

    def test_status_carries_no_idempotency_key(self) -> None:
        provider = self.make_provider()
        hold = self._held(provider)
        response = provider.status(transaction_id=hold.transaction_id)
        assert response.request.operation is Operation.STATUS
        assert response.request.idempotency_key is None

    def test_unwrap_returns_the_result_of_a_successful_call(self) -> None:
        provider = self.make_provider()
        response = self._hold(provider)
        assert response.unwrap() is response.result

    def test_unwrap_raises_the_error_of_a_failed_call(self) -> None:
        provider = self.make_provider()
        response = provider.status(transaction_id="no-such-transaction")
        try:
            response.unwrap()
        except PaymentFailed as exc:
            assert exc.error is response.error
        else:
            raise AssertionError("unwrap must raise on a failed response")

    # --- hold -------------------------------------------------------------

    def test_hold_authorizes_the_requested_amount(self) -> None:
        provider = self.make_provider()
        result = self._held(provider, amount="40.00")
        assert result.status is PaymentStatus.AUTHORIZED
        assert result.amount == self.money("40.00")
        assert result.amount.currency == self.currency
        assert result.provider == provider.name
        assert result.transaction_id

    def test_status_reports_a_hold_as_authorized(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        current = self._status(provider, held.transaction_id)
        assert current.transaction_id == held.transaction_id
        assert current.status is PaymentStatus.AUTHORIZED
        assert current.amount == self.money("40.00")

    def test_separate_holds_get_separate_transactions(self) -> None:
        provider = self.make_provider()
        first = self._hold(provider, order_id="order-1").unwrap()
        second = self._hold(provider, order_id="order-2").unwrap()
        assert first.transaction_id != second.transaction_id

    # --- capture ----------------------------------------------------------

    def test_capture_without_an_amount_settles_the_whole_hold(self) -> None:
        provider = self.make_provider()
        result = self._captured(provider, held="40.00")
        assert result.status is PaymentStatus.CAPTURED
        assert result.amount == self.money("40.00")

    def test_capture_with_an_amount_settles_that_amount(self) -> None:
        provider = self.make_provider()
        result = self._captured(provider, held="40.00", amount="25.00")
        assert result.status is PaymentStatus.CAPTURED
        assert result.amount == self.money("25.00")
        assert self._status(provider, result.transaction_id).amount == self.money(
            "25.00"
        )

    def test_capture_beyond_the_hold_is_refused_and_changes_nothing(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        self._invalid(
            provider.capture(
                transaction_id=held.transaction_id,
                idempotency_key=self.key("capture"),
                amount=self.money("50.00"),
            )
        )
        current = self._status(provider, held.transaction_id)
        assert current.status is PaymentStatus.AUTHORIZED
        assert current.amount == self.money("40.00")

    def test_capture_in_another_currency_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        self._invalid(
            provider.capture(
                transaction_id=held.transaction_id,
                idempotency_key=self.key("capture"),
                amount=Money(Decimal("10.00"), self.foreign_currency),
            )
        )
        assert (
            self._status(provider, held.transaction_id).status
            is PaymentStatus.AUTHORIZED
        )

    def test_capture_of_a_non_positive_amount_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        for amount in ("0.00", "-5.00"):
            self._invalid(
                provider.capture(
                    transaction_id=held.transaction_id,
                    idempotency_key=self.key("capture"),
                    amount=self.money(amount),
                )
            )
        assert (
            self._status(provider, held.transaction_id).status
            is PaymentStatus.AUTHORIZED
        )

    def test_capturing_twice_is_refused(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00", amount="25.00")
        self._invalid(
            provider.capture(
                transaction_id=captured.transaction_id,
                idempotency_key=self.key("capture"),
                amount=self.money("10.00"),
            )
        )
        assert self._status(provider, captured.transaction_id).amount == self.money(
            "25.00"
        )

    def test_capturing_a_cancelled_hold_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider)
        provider.cancel(
            transaction_id=held.transaction_id, idempotency_key=self.key("cancel")
        ).unwrap()
        self._invalid(
            provider.capture(
                transaction_id=held.transaction_id,
                idempotency_key=self.key("capture"),
            )
        )
        assert (
            self._status(provider, held.transaction_id).status
            is PaymentStatus.CANCELLED
        )

    # --- refund -----------------------------------------------------------

    def test_refund_returns_the_whole_capture(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        refunded = provider.refund(
            transaction_id=captured.transaction_id,
            idempotency_key=self.key("refund"),
        ).unwrap()
        assert refunded.status is PaymentStatus.REFUNDED
        assert refunded.amount == self.money("0")
        assert self._status(provider, captured.transaction_id).amount == self.money("0")

    def test_refund_after_a_partial_refund_returns_the_rest(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        provider.partial_refund(
            transaction_id=captured.transaction_id,
            amount=self.money("10.00"),
            idempotency_key=self.key("partial"),
        ).unwrap()
        refunded = provider.refund(
            transaction_id=captured.transaction_id,
            idempotency_key=self.key("refund"),
        ).unwrap()
        assert refunded.status is PaymentStatus.REFUNDED
        assert refunded.amount == self.money("0")

    def test_refunding_an_uncaptured_hold_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider)
        self._invalid(
            provider.refund(
                transaction_id=held.transaction_id,
                idempotency_key=self.key("refund"),
            )
        )
        assert (
            self._status(provider, held.transaction_id).status
            is PaymentStatus.AUTHORIZED
        )

    def test_refunding_twice_is_refused(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        provider.refund(
            transaction_id=captured.transaction_id,
            idempotency_key=self.key("refund"),
        ).unwrap()
        self._invalid(
            provider.refund(
                transaction_id=captured.transaction_id,
                idempotency_key=self.key("refund"),
            )
        )

    # --- partial refund ---------------------------------------------------

    def test_partial_refund_reports_what_is_left(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        result = provider.partial_refund(
            transaction_id=captured.transaction_id,
            amount=self.money("15.00"),
            idempotency_key=self.key("partial"),
        ).unwrap()
        assert result.status is PaymentStatus.PARTIALLY_REFUNDED
        assert result.amount == self.money("25.00")

    def test_partial_refunds_that_add_up_leave_the_payment_refunded(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        for amount in ("15.00", "25.00"):
            result = provider.partial_refund(
                transaction_id=captured.transaction_id,
                amount=self.money(amount),
                idempotency_key=self.key("partial"),
            ).unwrap()
        assert result.status is PaymentStatus.REFUNDED
        assert result.amount == self.money("0")

    def test_partial_refund_beyond_the_capture_is_refused(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        provider.partial_refund(
            transaction_id=captured.transaction_id,
            amount=self.money("10.00"),
            idempotency_key=self.key("partial"),
        ).unwrap()
        self._invalid(
            provider.partial_refund(
                transaction_id=captured.transaction_id,
                amount=self.money("40.00"),
                idempotency_key=self.key("partial"),
            )
        )
        current = self._status(provider, captured.transaction_id)
        assert current.status is PaymentStatus.PARTIALLY_REFUNDED
        assert current.amount == self.money("30.00")

    def test_partial_refund_of_an_uncaptured_hold_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider)
        self._invalid(
            provider.partial_refund(
                transaction_id=held.transaction_id,
                amount=self.money("5.00"),
                idempotency_key=self.key("partial"),
            )
        )

    # --- cancel -----------------------------------------------------------

    def test_cancel_releases_a_hold(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        result = provider.cancel(
            transaction_id=held.transaction_id, idempotency_key=self.key("cancel")
        ).unwrap()
        assert result.status is PaymentStatus.CANCELLED
        assert result.amount == self.money("0")

    def test_cancelling_a_captured_payment_is_refused(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        self._invalid(
            provider.cancel(
                transaction_id=captured.transaction_id,
                idempotency_key=self.key("cancel"),
            )
        )
        assert (
            self._status(provider, captured.transaction_id).status
            is PaymentStatus.CAPTURED
        )

    def test_cancelling_twice_is_refused(self) -> None:
        provider = self.make_provider()
        held = self._held(provider)
        provider.cancel(
            transaction_id=held.transaction_id, idempotency_key=self.key("cancel")
        ).unwrap()
        self._invalid(
            provider.cancel(
                transaction_id=held.transaction_id,
                idempotency_key=self.key("cancel"),
            )
        )

    # --- unknown transactions --------------------------------------------

    def test_operations_on_an_unknown_transaction_are_refused(self) -> None:
        provider = self.make_provider()
        unknown = "no-such-transaction"
        self._invalid(provider.status(transaction_id=unknown))
        self._invalid(
            provider.capture(
                transaction_id=unknown, idempotency_key=self.key("capture")
            )
        )
        self._invalid(
            provider.refund(transaction_id=unknown, idempotency_key=self.key("refund"))
        )
        self._invalid(
            provider.partial_refund(
                transaction_id=unknown,
                amount=self.money("5.00"),
                idempotency_key=self.key("partial"),
            )
        )
        self._invalid(
            provider.cancel(transaction_id=unknown, idempotency_key=self.key("cancel"))
        )

    # --- idempotency ------------------------------------------------------

    def test_a_repeated_hold_key_replays_the_first_answer(self) -> None:
        provider = self.make_provider()
        key = self.key("hold")
        first = self._hold(provider, amount="40.00", idempotency_key=key)
        again = self._hold(provider, amount="40.00", idempotency_key=key)
        assert not first.replayed
        assert again.replayed
        assert again.unwrap().transaction_id == first.unwrap().transaction_id
        current = self._status(provider, first.unwrap().transaction_id)
        assert current.status is PaymentStatus.AUTHORIZED
        assert current.amount == self.money("40.00")

    def test_a_repeated_capture_key_does_not_capture_twice(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        key = self.key("capture")
        first = provider.capture(
            transaction_id=held.transaction_id,
            idempotency_key=key,
            amount=self.money("10.00"),
        )
        again = provider.capture(
            transaction_id=held.transaction_id,
            idempotency_key=key,
            amount=self.money("10.00"),
        )
        assert again.replayed
        assert again.unwrap().amount == first.unwrap().amount
        assert self._status(provider, held.transaction_id).amount == self.money("10.00")

    def test_a_repeated_refund_key_does_not_refund_twice(self) -> None:
        provider = self.make_provider()
        captured = self._captured(provider, held="40.00")
        key = self.key("partial")
        for _ in range(2):
            response = provider.partial_refund(
                transaction_id=captured.transaction_id,
                amount=self.money("10.00"),
                idempotency_key=key,
            )
        assert response.replayed
        assert self._status(provider, captured.transaction_id).amount == self.money(
            "30.00"
        )

    def test_metadata_is_not_part_of_the_idempotency_comparison(self) -> None:
        provider = self.make_provider()
        key = self.key("hold")
        first = self._hold(provider, idempotency_key=key, metadata={"attempt": 1})
        again = self._hold(provider, idempotency_key=key, metadata={"attempt": 2})
        assert again.replayed
        assert again.unwrap().transaction_id == first.unwrap().transaction_id

    def test_a_key_returning_with_another_amount_is_a_conflict(self) -> None:
        provider = self.make_provider()
        key = self.key("hold")
        first = self._hold(provider, amount="40.00", idempotency_key=key).unwrap()
        self._conflict(self._hold(provider, amount="50.00", idempotency_key=key))
        current = self._status(provider, first.transaction_id)
        assert current.status is PaymentStatus.AUTHORIZED
        assert current.amount == self.money("40.00")

    def test_a_key_returning_for_another_operation_is_a_conflict(self) -> None:
        provider = self.make_provider()
        key = self.key("hold")
        held = self._hold(provider, amount="40.00", idempotency_key=key).unwrap()
        self._conflict(
            provider.capture(
                transaction_id=held.transaction_id, idempotency_key=key
            )
        )
        assert (
            self._status(provider, held.transaction_id).status
            is PaymentStatus.AUTHORIZED
        )

    def test_a_failed_answer_leaves_its_key_free(self) -> None:
        provider = self.make_provider()
        held = self._held(provider, amount="40.00")
        key = self.key("capture")
        self._invalid(
            provider.capture(
                transaction_id="no-such-transaction", idempotency_key=key
            )
        )
        retried = provider.capture(
            transaction_id=held.transaction_id, idempotency_key=key
        )
        assert retried.ok
        assert not retried.replayed
        assert retried.unwrap().status is PaymentStatus.CAPTURED
