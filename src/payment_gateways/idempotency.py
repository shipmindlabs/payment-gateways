"""Replay protection for retried provider operations.

A network error never tells the caller whether the request was executed: it may
have been lost on the way out, or executed and lost on the way back. The only
safe reaction to that uncertainty is to send the request again, which is safe
only if the provider recognises the retry. The idempotency key is what makes it
recognisable.

The rules kept here are the ones a real acquirer applies:

- The first successful answer for a key is stored and replayed for every later
  call carrying that key, so a retry cannot charge twice.
- A failed answer is not stored, so the same key retries the operation that
  failed instead of replaying the failure.
- A key that returns with different money-moving fields is a conflict, not a
  replay: replaying would silently swallow the second request, and executing it
  would charge twice under a key the caller believes is protecting them.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from payment_gateways.errors import IdempotencyConflict
from payment_gateways.models import PaymentResult, ProviderRequest, ProviderResponse

__all__ = ["IdempotencyStore", "request_fingerprint"]

_SEPARATOR = "\x1f"


def request_fingerprint(request: ProviderRequest) -> str:
    """Digest the fields of `request` that decide where money moves.

    Metadata and timestamps are left out: they annotate a call without changing
    its effect, so a retry that only carries fresh metadata is still the same
    operation.
    """
    amount = request.amount
    fields = (
        request.operation.value,
        request.provider,
        request.order_id or "",
        request.transaction_id or "",
        "" if amount is None else f"{amount.amount.normalize()}|{amount.currency}",
    )
    return sha256(_SEPARATOR.join(fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Entry:
    """The answer a key is bound to, with the request that produced it."""

    fingerprint: str
    result: PaymentResult


class IdempotencyStore:
    """The first successful answer per idempotency key, kept for replay.

    Keys are scoped to the store, not to an operation: a key identifies one
    attempt at one operation, so reusing it for another is answered with
    `IdempotencyConflict`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def replay(
        self, request: ProviderRequest
    ) -> PaymentResult | IdempotencyConflict | None:
        """Answer a repeated key.

        Returns the stored result when `request` repeats a completed call, an
        `IdempotencyConflict` when the key was used for a different request, and
        None when the key is unknown and the operation still has to run.
        """
        key = request.idempotency_key
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.fingerprint != request_fingerprint(request):
            return IdempotencyConflict(
                f"idempotency key {key!r} was already used for a different request",
                raw={"transaction_id": entry.result.transaction_id},
            )
        return entry.result

    def record(self, response: ProviderResponse) -> None:
        """Bind the key of `response` to its result, if it carries both."""
        key = response.request.idempotency_key
        if key is None or response.result is None or key in self._entries:
            return
        self._entries[key] = _Entry(
            fingerprint=request_fingerprint(response.request),
            result=response.result,
        )
