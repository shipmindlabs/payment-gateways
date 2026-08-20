# payment-gateways

Payment provider abstraction: one protocol for hold, capture, refund, cancel and
status, with every request and callback recorded for audit.

## Status

Early core. The provider protocol, the value objects it exchanges and an
in-memory provider are in place; no network provider is implemented yet.

## Installation

```bash
pip install payment-gateways
```

## Value objects

Everything crossing the provider boundary is a frozen dataclass, so a recorded
object cannot be edited after the fact:

- `Money` — an exact `Decimal` amount with an ISO 4217 currency.
- `ProviderRequest` — the operation, the amount and the idempotency key that
  were sent.
- `ProviderResponse` — that request paired with exactly one of a
  `PaymentResult` or a `ProviderError`.
- `Declined`, `NetworkError`, `InvalidState` — the three failure shapes, each
  carrying the provider's own code and raw payload.

## Why errors are values, not only exceptions

A declined card is a normal outcome of taking payments, not an exceptional one.
Three properties follow from returning failures instead of raising them:

- **Audit stays complete.** The failure is part of the response object that gets
  written to the trail; an exception travelling up the stack can bypass the
  recording step.
- **Handling is checked.** `ProviderResponse.error` is part of the type, so a
  call site that ignores declines is visible to a type checker and to review.
- **Retries are decidable.** `NetworkError.retryable` is true while a decline is
  final, which is the distinction a retry policy actually needs.

Callers that want the raising style keep it: `ProviderResponse.unwrap()` returns
the `PaymentResult` or raises `PaymentFailed` with the error attached.

## Reference implementation

`FakeProvider` keeps its transactions in memory. It is the implementation to
read when writing a real one, and the one to use in tests:

```python
from decimal import Decimal

from payment_gateways import Declined, FakeProvider, Money, Operation

provider = FakeProvider()

hold = provider.hold(
    order_id="order-1",
    amount=Money(Decimal("40.00"), "EUR"),
    idempotency_key="hold-order-1",
).unwrap()

capture = provider.capture(
    transaction_id=hold.transaction_id,
    idempotency_key="capture-order-1",
    amount=Money(Decimal("25.00"), "EUR"),
)
assert capture.ok
```

Outcomes are scriptable, so error handling can be exercised without a network.
A queued error is returned by the next call to that operation and leaves the
transaction untouched:

```python
provider.fail_next(Operation.REFUND, Declined("refund window closed", code="R12"))

refund = provider.refund(
    transaction_id=hold.transaction_id,
    idempotency_key="refund-order-1",
)
assert isinstance(refund.error, Declined)
```

Two more behaviours match what a real acquirer does: a successful call is
replayed for a repeated idempotency key, while a failed one is not, so a
retryable error can be retried under the same key. Every answered call, replays
and failures included, is appended to `provider.calls` as a `ProviderResponse` —
the audit trail in its smallest possible form.

## Development

```bash
pip install -e .
```

## License

MIT, see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
