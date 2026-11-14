"""Read-only post-trade simulation for a saved rebalancing proposal.

No database records are changed. A ``proposed`` trade is treated as a valid
recommendation for simulation only; it is not a real trade or an execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from src.config import ALLOCATION_DRIFT_TOLERANCE, DATABASE_PATH, STALE_PRICE_THRESHOLD_DAYS
from src.database.connection import get_connection
from src.services.constraints import ConstraintSet, validate_trade_constraints
from src.services.audit import append_audit_event
from src.services.valuation import MONEY_PRECISION, PortfolioValuation, value_portfolio


@dataclass(frozen=True)
class SimulatedTrade:
    """One saved recommendation's read-only simulation outcome."""

    trade_id: int
    instrument_id: int
    action: str
    quantity: Decimal
    market_price: Optional[Decimal]
    trade_value: Decimal
    fee: Decimal
    status: str
    reason: str


@dataclass(frozen=True)
class SimulatedHolding:
    """Before-and-after quantities and values for one original holding."""

    instrument_id: int
    asset_class: str
    original_quantity: Decimal
    simulated_quantity: Decimal
    market_price: Optional[Decimal]
    price_status: str
    original_market_value: Optional[Decimal]
    simulated_market_value: Optional[Decimal]
    original_allocation: Decimal
    target_allocation: Decimal
    simulated_allocation: Decimal
    final_drift: Decimal
    allocation_status: str


@dataclass(frozen=True)
class SimulationResult:
    """A complete before-and-after view without changing the original portfolio."""

    portfolio_id: int
    proposal_id: int
    valuation_date: date
    original_portfolio_value: Decimal
    simulated_portfolio_value: Decimal
    starting_cash: Decimal
    simulated_remaining_cash: Decimal
    accepted_buys: tuple[SimulatedTrade, ...]
    accepted_sells: tuple[SimulatedTrade, ...]
    blocked_trades: tuple[SimulatedTrade, ...]
    total_fees: Decimal
    holdings: tuple[SimulatedHolding, ...]
    missing_price_count: int
    stale_price_count: int


def _decimal(value: object | None) -> Decimal:
    """Convert SQLite numeric data through text for safe Decimal arithmetic."""
    return Decimal("0") if value is None else Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    """Round currency values to cents once, at the financial boundary."""
    return value.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def _as_date(value: date | str) -> date:
    """Accept an ISO string or a Python date."""
    return value if isinstance(value, date) else date.fromisoformat(value)


def _read_proposal_trades(portfolio_id: int, proposal_id: int, database_path: Path) -> tuple[Decimal, list[dict[str, object]]]:
    """Return original cash and the saved recommendation rows for one proposal."""
    with get_connection(database_path) as connection:
        proposal = connection.execute(
            "SELECT portfolio_id FROM rebalance_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if proposal is None:
            raise ValueError(f"Proposal {proposal_id} does not exist.")
        if proposal["portfolio_id"] != portfolio_id:
            raise ValueError(f"Proposal {proposal_id} does not belong to portfolio {portfolio_id}.")
        cash_row = connection.execute(
            "SELECT available_cash FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()
        if cash_row is None:
            raise ValueError(f"Portfolio {portfolio_id} does not exist.")
        rows = connection.execute(
            """
            SELECT t.trade_id, t.instrument_id, t.trade_action, t.recommended_quantity,
                   t.estimated_fee, t.trade_status, t.rationale
            FROM trades AS t
            WHERE t.proposal_id = ? AND t.portfolio_id = ?
            ORDER BY CASE t.trade_action WHEN 'SELL' THEN 0 WHEN 'BUY' THEN 1 ELSE 2 END, t.trade_id
            """,
            (proposal_id, portfolio_id),
        ).fetchall()
    return _decimal(cash_row["available_cash"]), [dict(row) for row in rows]


def _read_targets(portfolio_id: int, valuation_date: date, database_path: Path) -> dict[str, Decimal]:
    """Read the most recent target set on or before the simulation date."""
    with get_connection(database_path) as connection:
        target_date = connection.execute(
            "SELECT MAX(target_date) FROM target_allocations WHERE portfolio_id = ? AND target_date <= ?",
            (portfolio_id, valuation_date.isoformat()),
        ).fetchone()[0]
        if target_date is None:
            return {}
        rows = connection.execute(
            """
            SELECT i.asset_class, SUM(ta.target_weight) AS target_weight
            FROM target_allocations AS ta
            JOIN instruments AS i ON i.instrument_id = ta.instrument_id
            WHERE ta.portfolio_id = ? AND ta.target_date = ?
            GROUP BY i.asset_class
            """,
            (portfolio_id, target_date),
        ).fetchall()
    return {row["asset_class"]: _decimal(row["target_weight"]) for row in rows}


def _read_constraints(portfolio_id: int, database_path: Path) -> ConstraintSet:
    """Read the active safety rules again before simulating saved trades."""
    with get_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT instrument_id, constraint_type, numeric_value FROM \"constraints\" WHERE portfolio_id = ? AND is_active = 1",
            (portfolio_id,),
        ).fetchall()
    minimum = Decimal("0")
    excluded: set[int] = set()
    limits: dict[int | None, Decimal] = {}
    for row in rows:
        instrument_id = row["instrument_id"]
        amount = _decimal(row["numeric_value"])
        if row["constraint_type"] == "minimum_trade_value":
            minimum = max(minimum, amount)
        elif row["constraint_type"] == "excluded_instrument" and instrument_id is not None:
            excluded.add(instrument_id)
        elif row["constraint_type"] in ("liquidity_limit", "maximum_trade_value"):
            existing = limits.get(instrument_id)
            limits[instrument_id] = amount if existing is None else min(existing, amount)
    return ConstraintSet(minimum, frozenset(excluded), limits)


def _blocked_trade(row: dict[str, object], price: Optional[Decimal], reason: str) -> SimulatedTrade:
    """Represent a non-executed trade without changing simulated quantities."""
    return SimulatedTrade(int(row["trade_id"]), int(row["instrument_id"]), str(row["trade_action"]), _decimal(row["recommended_quantity"]), price, Decimal("0"), Decimal("0"), "BLOCKED", reason)


def simulate_proposal(
    portfolio_id: int,
    proposal_id: int,
    valuation_date: date | str,
    database_path: Path = DATABASE_PATH,
    stale_price_threshold_days: int = STALE_PRICE_THRESHOLD_DAYS,
    drift_tolerance: Decimal = Decimal(str(ALLOCATION_DRIFT_TOLERANCE)),
) -> SimulationResult:
    """Simulate a saved proposal in memory and return before-and-after values.

    A missing or stale price makes the affected saved recommendation blocked.
    A blocked database trade, invalid quantity, oversell, or cash shortfall is
    also visible in ``blocked_trades`` and has no effect on simulated cash.
    """
    as_of_date = _as_date(valuation_date)
    valuation = value_portfolio(portfolio_id, as_of_date, database_path, stale_price_threshold_days)
    starting_cash, saved_trades = _read_proposal_trades(portfolio_id, proposal_id, database_path)
    targets = _read_targets(portfolio_id, as_of_date, database_path)
    constraints = _read_constraints(portfolio_id, database_path)

    by_instrument = {holding.instrument_id: holding for holding in valuation.holdings}
    simulated_quantities = {holding.instrument_id: holding.quantity for holding in valuation.holdings}
    cash = starting_cash
    accepted_buys: list[SimulatedTrade] = []
    accepted_sells: list[SimulatedTrade] = []
    blocked: list[SimulatedTrade] = []
    total_fees = Decimal("0")

    for row in saved_trades:
        instrument_id = int(row["instrument_id"])
        action = str(row["trade_action"])
        holding = by_instrument.get(instrument_id)
        if str(row["trade_status"]) != "proposed":
            blocked.append(_blocked_trade(row, holding.market_price if holding else None, "Saved recommendation was already blocked or not proposed."))
            continue
        if action not in ("BUY", "SELL"):
            blocked.append(_blocked_trade(row, holding.market_price if holding else None, "Only BUY and SELL recommendations can be simulated."))
            continue
        if holding is None:
            blocked.append(_blocked_trade(row, None, "Instrument is not an original portfolio holding."))
            continue
        if holding.price_status == "MISSING":
            blocked.append(_blocked_trade(row, holding.market_price, "Missing market price prevents simulation."))
            continue
        if holding.price_status == "STALE":
            blocked.append(_blocked_trade(row, holding.market_price, "Stale market price prevents simulation."))
            continue
        quantity = _decimal(row["recommended_quantity"])
        fee = _decimal(row["estimated_fee"])
        if quantity <= 0 or fee < 0 or holding.market_price is None:
            blocked.append(_blocked_trade(row, holding.market_price, "Saved recommendation has an invalid quantity, fee, or price."))
            continue
        trade_value = _money(quantity * holding.market_price)
        failure = validate_trade_constraints(
            action=action, instrument_id=instrument_id, current_quantity=simulated_quantities[instrument_id],
            proposed_quantity=quantity, trade_value=trade_value, estimated_fee=fee,
            cash_available=cash, constraints=constraints,
        )
        if failure:
            blocked.append(_blocked_trade(row, holding.market_price, failure.reason))
            continue
        if action == "SELL":
            if quantity > simulated_quantities[instrument_id]:
                blocked.append(_blocked_trade(row, holding.market_price, "Simulated sale exceeds the available holding quantity."))
                continue
            simulated_quantities[instrument_id] -= quantity
            cash += trade_value - fee
            simulated = SimulatedTrade(int(row["trade_id"]), instrument_id, action, quantity, holding.market_price, trade_value, fee, "SIMULATED", "Saved proposed SELL passed simulation checks.")
            accepted_sells.append(simulated)
        else:
            if trade_value + fee > cash:
                blocked.append(_blocked_trade(row, holding.market_price, "Simulated BUY exceeds available cash after fees."))
                continue
            simulated_quantities[instrument_id] += quantity
            cash -= trade_value + fee
            simulated = SimulatedTrade(int(row["trade_id"]), instrument_id, action, quantity, holding.market_price, trade_value, fee, "SIMULATED", "Saved proposed BUY passed simulation checks.")
            accepted_buys.append(simulated)
        total_fees += fee

    simulated_asset_values: dict[str, Decimal] = {}
    simulated_values: dict[int, Optional[Decimal]] = {}
    for holding in valuation.holdings:
        if holding.market_price is None:
            simulated_values[holding.instrument_id] = None
            continue
        value = _money(simulated_quantities[holding.instrument_id] * holding.market_price)
        simulated_values[holding.instrument_id] = value
        simulated_asset_values[holding.asset_class] = simulated_asset_values.get(holding.asset_class, Decimal("0")) + value
    simulated_total = _money(sum(simulated_asset_values.values(), Decimal("0")))

    asset_classes = sorted(set(valuation.asset_class_values) | set(simulated_asset_values) | set(targets))
    original_weights = {
        asset_class: valuation.asset_class_values.get(asset_class, Decimal("0")) / valuation.total_portfolio_value
        if valuation.total_portfolio_value else Decimal("0")
        for asset_class in asset_classes
    }
    simulated_weights = {
        asset_class: simulated_asset_values.get(asset_class, Decimal("0")) / simulated_total
        if simulated_total else Decimal("0")
        for asset_class in asset_classes
    }
    holdings = []
    for holding in valuation.holdings:
        target_weight = targets.get(holding.asset_class, Decimal("0"))
        simulated_weight = simulated_weights.get(holding.asset_class, Decimal("0"))
        drift = simulated_weight - target_weight
        status = "ON_TARGET" if abs(drift) <= drift_tolerance else ("OVERWEIGHT" if drift > 0 else "UNDERWEIGHT")
        holdings.append(SimulatedHolding(
            holding.instrument_id, holding.asset_class, holding.quantity,
            simulated_quantities[holding.instrument_id], holding.market_price,
            holding.price_status, holding.market_value,
            simulated_values[holding.instrument_id], original_weights.get(holding.asset_class, Decimal("0")),
            target_weight, simulated_weight, drift, status,
        ))

    result = SimulationResult(
        portfolio_id, proposal_id, as_of_date, valuation.total_portfolio_value,
        simulated_total, starting_cash, _money(cash), tuple(accepted_buys),
        tuple(accepted_sells), tuple(blocked), _money(total_fees), tuple(holdings),
        valuation.missing_price_count, valuation.stale_price_count,
    )
    append_audit_event(
        "SIMULATION_EXECUTED",
        {"valuation_date": as_of_date, "original_portfolio_value": result.original_portfolio_value,
         "simulated_portfolio_value": result.simulated_portfolio_value,
         "accepted_buy_count": len(result.accepted_buys), "accepted_sell_count": len(result.accepted_sells),
         "blocked_trade_count": len(result.blocked_trades), "total_fees": result.total_fees},
        entity_type="rebalance_proposal", entity_id=proposal_id, portfolio_id=portfolio_id,
        proposal_id=proposal_id, database_path=database_path,
    )
    return result
