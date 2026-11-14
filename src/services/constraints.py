"""Reusable policy and fee validation for proposed rebalancing trades.

These functions make no database changes. They return a reason when a trade is
unsafe, allowing the rebalancer to save an explicit blocked HOLD decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class ConstraintSet:
    """Active portfolio rules needed to validate one candidate trade."""

    minimum_trade_value: Decimal
    excluded_instruments: frozenset[int]
    liquidity_limits: dict[int | None, Decimal]

    def liquidity_limit_for(self, instrument_id: int) -> Optional[Decimal]:
        """Prefer an instrument limit; otherwise use a portfolio-wide limit."""
        return self.liquidity_limits.get(instrument_id, self.liquidity_limits.get(None))


@dataclass(frozen=True)
class ValidationFailure:
    """A machine-readable safety code and an explanation for a user."""

    status: str
    reason: str


def validate_fee_rule(fixed_fee: Decimal, percentage_fee: Decimal) -> Optional[ValidationFailure]:
    """Check fee components before they are allowed into a cash calculation."""
    if fixed_fee < 0:
        return ValidationFailure("BLOCKED_INVALID_FEE_SCHEDULE", "Fee schedule has a negative fixed fee.")
    if percentage_fee < 0 or percentage_fee > 1:
        return ValidationFailure("BLOCKED_INVALID_FEE_SCHEDULE", "Fee percentage must be between 0% and 100%.")
    return None


def validate_trade_constraints(
    *, action: str, instrument_id: int, current_quantity: Decimal,
    proposed_quantity: Decimal, trade_value: Decimal, estimated_fee: Decimal,
    cash_available: Decimal, constraints: ConstraintSet,
) -> Optional[ValidationFailure]:
    """Return the first safety violation for a calculated trade, if any.

    A BUY needs enough cash for value *and* fee. A SELL may never be larger
    than the holding, so a negative holding cannot be recommended.
    """
    if instrument_id in constraints.excluded_instruments:
        return ValidationFailure("BLOCKED_EXCLUDED_INSTRUMENT", "Instrument is excluded by policy.")
    if proposed_quantity <= 0 or trade_value <= 0:
        return ValidationFailure("BLOCKED_ZERO_TRADE", "Calculated trade quantity or value is zero.")
    if trade_value < constraints.minimum_trade_value:
        return ValidationFailure("BLOCKED_MINIMUM_TRADE_SIZE", "Trade value is below the minimum trade-size policy.")
    liquidity_limit = constraints.liquidity_limit_for(instrument_id)
    if liquidity_limit is not None and trade_value > liquidity_limit:
        return ValidationFailure("BLOCKED_LIQUIDITY_LIMIT", "Trade value exceeds the configured liquidity limit.")
    if action == "SELL" and proposed_quantity > current_quantity:
        return ValidationFailure("BLOCKED_INSUFFICIENT_HOLDINGS", "Requested sale exceeds available holdings.")
    if action == "BUY" and trade_value + estimated_fee > cash_available:
        return ValidationFailure("BLOCKED_INSUFFICIENT_CASH", "Cash is insufficient to pay for this BUY plus its fee.")
    return None
