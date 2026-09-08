# payment-gateways

Payment provider abstraction: one protocol for hold, capture, refund, cancel and
status, with every request and callback recorded for audit.

## Why this exists

Taking the money is rarely the hard part. The hard part is everything around it:

- **Every acquirer has its own vocabulary.** One calls it an authorization, the
  next a pre-auth, a third only settles on charge. Code written against one
  SDK is code that has to be rewritten to add a second provider, or to leave
  the first.
- **Retries are unavoidable and dangerous.** A timeout never says whether the
  charge happened. Sending it again is the only safe reaction, and it is also
  how a customer gets billed twice, unless the provider recognises the retry.
- **Failure is the normal path.** Declines, expired holds, closed refund
  windows: those are outcomes of taking payments, not exceptional events, and
  they belong at the call site rather than unwinding past it.
- **Somebody will ask what happened.** Support, finance, a chargeback, an
  auditor. If the trail is a log line written when someone remembered to write
  one, the answer is a reconstruction.

This library fixes one small surface for all of that: six operations, failures
returned as values, idempotency rules a provider is held to, and an audit
wrapper that records the exchange whether it succeeded, failed or raised.
Providers plug in behind it; the call sites never learn which one is in use.

## Status

Early core. The provider protocol, the value objects it exchanges, an in-memory
provider, the idempotency rules, the audit trail and the contract test are in
place; no network provider is implemented yet.

## Installation

```bash
pip install payment-gateways
```

## Quickstart

Hold, settle, and keep the trail — the whole surface in one screen:

```python
from decimal import Decimal

from payment_gateways import AuditedProvider, FakeProvider, InMemoryAuditSink, Money

sink = InMemoryAuditSink()
provider = AuditedProvider(FakeProvider(), sink)

hold = provider.hold(
    order_id="order-1",
    amount=Money(Decimal("40.00"), "EUR"),
    idempotency_key="hold:order-1",
)
if not hold.ok:
    raise SystemExit(f"hold refused: {hold.error}")

capture = provider.capture(
    transaction_id=hold.unwrap().transaction_id,
    idempotency_key="capture:order-1",
)
print(capture.unwrap().status.value, len(sink.records))  # captured 2
```

Swap `FakeProvider()` for a real implementation and nothing above changes.

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

## Plugging in a provider

A provider is any object that satisfies `PaymentProvider` — there is no base
class to inherit and nothing to register. What the protocol expects of it, in
the order it is worth working through:

1. **Implement the six operations with the signatures in `protocol.py`.** They
   are keyword-only, so an argument cannot slip into the wrong position; a
   `status` read takes no idempotency key.
2. **Answer with a `ProviderResponse`, always.** It carries the request as it was
   sent plus exactly one of a result or an error, which is what makes the whole
   exchange recordable before the caller reacts.
3. **Map the acquirer's failures onto the four error shapes.** Keep its own code
   and payload in `code` and `raw`: the shape decides whether a retry makes
   sense, the raw fields are what an investigation needs.
4. **Refuse what the state does not allow.** Capturing a cancelled hold or
   refunding an uncaptured one answers with `InvalidState` and changes nothing.
5. **Honour the idempotency key.** Ask an `IdempotencyStore` before executing and
   record the answer after, the way `FakeProvider` does; a repeat replays, a
   failure frees its key, a mismatch conflicts.
6. **Report `result.amount` as the protocol promises:** the authorized amount
   while held, captured minus refunded once settled, zero when cancelled or
   fully refunded.
7. **Give the provider a `name`.** It lands on every request and every audit
   record, so a trail spanning two acquirers stays readable.
8. **Leave auditing out of it.** Recording is `AuditedProvider`'s job; a provider
   that logs on its own duplicates the trail and diverges from it.
9. **Run the contract test.** Subclass `PaymentProviderContract` and the promises
   above stop being prose.

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

## Contract test

The promises above are what a provider owes its callers, and none of them is
visible in a signature. `PaymentProviderContract` turns them into tests an
implementation runs against itself:

```python
from payment_gateways import FakeProvider
from payment_gateways.contract import PaymentProviderContract


class TestFakeProvider(PaymentProviderContract):
    def make_provider(self):
        return FakeProvider()
```

The suite drives the whole lifecycle, checks what each state refuses, and holds
the provider to the idempotency rules: a repeat replays instead of charging
again, a failure leaves its key free, a reused key is answered with a conflict
and executes nothing. It reaches the provider only through the protocol and
asserts with plain `assert`, so a wrapper such as `AuditedProvider` subclasses it
the same way an acquirer client does. Providers that do not settle in EUR
override the `currency` and `foreign_currency` attributes.

## Development

```bash
pip install -e ".[test]"
python -m pytest
```

## License

MIT, see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
