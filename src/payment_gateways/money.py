"""Money value type shared by every provider operation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["Money"]


@dataclass(frozen=True, slots=True)
class Money:
    """An exact, finite monetary amount in a single ISO 4217 currency."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        # NaN and Infinity compare unlike any real amount, so a single one of
        # them entering here breaks every later limit check silently.
        if not self.amount.is_finite():
            raise ValueError("amount must be a finite number")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter ISO 4217 code")
        object.__setattr__(self, "currency", self.currency.upper())

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
