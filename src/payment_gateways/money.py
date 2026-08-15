"""Money value type shared by every provider operation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["Money"]


@dataclass(frozen=True, slots=True)
class Money:
    """An exact monetary amount in a single ISO 4217 currency."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter ISO 4217 code")
        object.__setattr__(self, "currency", self.currency.upper())

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
