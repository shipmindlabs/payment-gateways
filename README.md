# payment-gateways

Payment provider abstraction: one protocol for hold, capture, refund, cancel and
status, with every request and callback recorded for audit.

## Status

Early core. The provider protocol, the value objects it exchanges, an in-memory
provider, the idempotency rules and the audit trail are in place; no network
provider is implemented yet.

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
- `Declined`, `NetworkError`, `InvalidState`, `IdempotencyConflict` — the four
  failure shapes, each carrying the provider's own code and raw payload.

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

## Idempotency

Every operation that moves money takes an `idempotency_key`. A network error
never says whether the request was executed, so the only safe reaction is to send
it again under the same key.

What a provider guarantees for that key:

- **A repeat never charges twice.** The first successful answer for a key is
  stored and returned again — flagged by `ProviderResponse.replayed` — without
  the transaction being touched a second time.
- **A failure does not claim the key.** Failed answers are not stored, so a
  `NetworkError` can be retried under the very key that failed.
- **A reused key is refused, not replayed.** If a key returns with different
  money-moving fields — operation, order, transaction or amount — the call is
  answered with `IdempotencyConflict` and nothing is executed. Metadata and
  timestamps are not part of that comparison.
- **Replays are audited.** A replayed call is a real answer to a real request and
  reaches `provider.calls` and the audit sink like any other.

```python
first = provider.hold(
    order_id="order-1",
    amount=Money(Decimal("40.00"), "EUR"),
    idempotency_key="hold-order-1",
)
retry = provider.hold(
    order_id="order-1",
    amount=Money(Decimal("40.00"), "EUR"),
    idempotency_key="hold-order-1",
)
assert retry.replayed
assert retry.unwrap().transaction_id == first.unwrap().transaction_id
```

The guarantees are only worth as much as the keys handed to them, which is the
caller's side of the deal:

- **Derive the key from the operation, never generate it per attempt.** Something
  like `f"capture:{order_id}"` is stable across retries; a fresh UUID per attempt
  is a new operation every time, and that is how double charges happen.
- **Persist the key before the call.** Store it with the order, not in memory, or
  a crash mid-call leaves the retry unable to recognise itself.
- **One key per operation.** The hold, the capture and the refund of one order
  each need their own key; the same key across two of them is a conflict.
- **Change the request, change the key.** Capturing a different amount is a
  different operation, not a retry.
- **Retry only what is retryable.** `NetworkError.retryable` is true; a `Declined`
  or an `IdempotencyConflict` will answer the same way however often it is sent.

`IdempotencyStore` implements these rules and is reusable: an implementation of
the protocol can hold one and ask it before every call, as `FakeProvider` does.

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

A stored key still wins over a queued failure, because a completed operation may
not run again. Every answered call — replays, conflicts and failures included —
is appended to `provider.calls` as a `ProviderResponse`: the audit trail in its
smallest possible form.

## Audit

Auditing is a wrapper, not a provider feature: `AuditedProvider` implements the
same protocol, forwards each call and writes an `AuditRecord` — request,
response, duration, error — to an `AuditSink`.

```python
from decimal import Decimal

from payment_gateways import AuditedProvider, FakeProvider, InMemoryAuditSink, Money

sink = InMemoryAuditSink()
provider = AuditedProvider(FakeProvider(), sink)

provider.hold(
    order_id="order-1",
    amount=Money(Decimal("40.00"), "EUR"),
    idempotency_key="hold-order-1",
    metadata={"card_number": "4111111111111111", "channel": "web"},
)

record = sink.records[0]
assert record.request.metadata == {"card_number": "***", "channel": "web"}
assert record.duration_ms is not None
```

A sink is anything with `record(AuditRecord) -> None`, so a database writer or a
queue producer drops in without changing call sites. Inbound callbacks go to the
same trail through `record_callback(sink, response)`, tagged `Origin.CALLBACK`.

Three rules hold for everything the trail receives:

- **Secrets never reach the sink.** Values under keys that look like card data,
  tokens, signatures or passwords are replaced by `***` before the record is
  handed over, including inside nested payloads.
- **A failing sink cannot fail a payment.** Sink errors are logged and dropped;
  the payment result reaches the caller untouched.
- **Unexpected exceptions are recorded too.** If a provider raises rather than
  returning an error, the record carries the exception and the call still
  propagates.

## Development

```bash
pip install -e .
```

## License

MIT, see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
