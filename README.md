# payment-gateways

Payment provider abstraction: one protocol for hold, capture, refund, cancel and
status, with every request and callback recorded for audit.

## Status

Early core. The provider protocol and the value objects it exchanges are in
place; no concrete provider is implemented yet.

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

## Development

```bash
pip install -e .
```

## License

MIT, see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
