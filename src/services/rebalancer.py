"""Policy-constrained BUY, SELL, and HOLD recommendations.

The engine creates an append-only proposal and proposed trades. It never
executes trades or changes holdings, prices, target allocations, or cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from src.config import ALLOCATION_DRIFT_TOLERANCE, DATABASE_PATH, STALE_PRICE_THRESHOLD_DAYS
from src.database.connection import get_connection
from src.services.constraints import ConstraintSet, validate_fee_rule, validate_trade_constraints
from src.services.audit import append_audit_event
from src.services.valuation import MONEY_PRECISION, PortfolioValuation, value_portfolio

QUANTITY_PRECISION = Decimal("0.0001")
WEIGHT_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class FeeRule:
    """The fee rule that applies to a proposal on its valuation date."""

    fixed_fee: Decimal
    percentage_fee: Decimal


@dataclass(frozen=True)
class RebalanceRecommendation:
    """A complete decision record for one held instrument.

    ``approval_status`` is always ``proposed`` or ``blocked`` here. Approval
    and execution are separate future user actions.
    """

    proposal_id: int
    portfolio_id: int
    instrument_id: int
    asset_class: str
    action: str
    current_quantity: Decimal
    proposed_quantity: Decimal
    market_price: Optional[Decimal]
    trade_value: Decimal
    estimated_fee: Decimal
    current_allocation: Decimal
    target_allocation: Decimal
    allocation_drift: Decimal
    decision_reason: str
    constraint_status: str
    approval_status: str


@dataclass(frozen=True)
class RebalanceProposalResult:
    """The saved proposal together with all of its instrument decisions."""

    proposal_id: int
    portfolio_id: int
    valuation_date: date
    total_portfolio_value: Decimal
    recommendations: tuple[RebalanceRecommendation, ...]


def _decimal(value: object | None, default: Decimal = Decimal("0")) -> Decimal:
    """Convert SQLite values through text, avoiding binary floating-point noise."""
    return default if value is None else Decimal(str(value))


def _date(value: date | str) -> date:
    """Normalize a date parameter accepted by public entry points."""
    return value if isinstance(value, date) else date.fromisoformat(value)


def _money(value: Decimal) -> Decimal:
    """Round a monetary result to cents using normal financial rounding."""
    return value.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def _quantity(value: Decimal) -> Decimal:
    """Round down so a recommendation cannot exceed its money budget."""
    return max(Decimal("0"), value.quantize(QUANTITY_PRECISION, rounding=ROUND_DOWN))


def _read_policy_inputs(portfolio_id: int, valuation_date: date, database_path: Path) -> tuple[dict[str, Decimal], Decimal, Optional[FeeRule], Optional[str], ConstraintSet]:
    """Read target weights, cash, fees, exclusions, and liquidity limits."""
    with get_connection(database_path) as connection:
        portfolio = connection.execute(
            "SELECT available_cash FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} does not exist.")

        latest_target_date = connection.execute(
            "SELECT MAX(target_date) FROM target_allocations WHERE portfolio_id = ? AND target_date <= ?",
            (portfolio_id, valuation_date.isoformat()),
        ).fetchone()[0]
        targets: dict[str, Decimal] = {}
        if latest_target_date is not None:
            target_rows = connection.execute(
                """
                SELECT i.asset_class, SUM(ta.target_weight) AS target_weight
                FROM target_allocations AS ta
                INNER JOIN instruments AS i ON i.instrument_id = ta.instrument_id
                WHERE ta.portfolio_id = ? AND ta.target_date = ?
                GROUP BY i.asset_class
                """,
                (portfolio_id, latest_target_date),
            ).fetchall()
            targets = {row["asset_class"]: _decimal(row["target_weight"]) for row in target_rows}

        fee_row = connection.execute(
            """
            SELECT fixed_fee, percentage_fee FROM fee_schedules
            WHERE portfolio_id = ? AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to >= ?)
            ORDER BY effective_from DESC, fee_schedule_id DESC LIMIT 1
            """,
            (portfolio_id, valuation_date.isoformat(), valuation_date.isoformat()),
        ).fetchone()
        fee_rule = FeeRule(
            fixed_fee=_decimal(fee_row["fixed_fee"]),
            percentage_fee=_decimal(fee_row["percentage_fee"]),
        ) if fee_row else None
        constraint_rows = connection.execute(
            """
            SELECT instrument_id, constraint_type, numeric_value
            FROM "constraints"
            WHERE portfolio_id = ? AND is_active = 1
            """,
            (portfolio_id,),
        ).fetchall()

    minimum_trade = Decimal("0")
    excluded: set[int] = set()
    liquidity_limits: dict[int | None, Decimal] = {}
    for row in constraint_rows:
        kind = row["constraint_type"]
        instrument_id = row["instrument_id"]
        amount = _decimal(row["numeric_value"])
        if kind == "minimum_trade_value":
            minimum_trade = max(minimum_trade, amount)
        elif kind == "excluded_instrument" and instrument_id is not None:
            excluded.add(instrument_id)
        elif kind in ("liquidity_limit", "maximum_trade_value"):
            # Both rules are dollar caps. If more than one applies, use the
            # stricter (smaller) cap rather than silently ignoring either.
            existing_limit = liquidity_limits.get(instrument_id)
            liquidity_limits[instrument_id] = amount if existing_limit is None else min(existing_limit, amount)
    fee_problem = None
    if fee_rule is None:
        fee_problem = "No active fee schedule applies on the valuation date."
    else:
        failure = validate_fee_rule(fee_rule.fixed_fee, fee_rule.percentage_fee)
        fee_problem = failure.reason if failure else None
    constraints = ConstraintSet(minimum_trade, frozenset(excluded), liquidity_limits)
    return targets, _decimal(portfolio["available_cash"]), fee_rule, fee_problem, constraints


def _fee_for(trade_value: Decimal, fee_rule: FeeRule) -> Decimal:
    """Calculate fixed plus percentage fee for a non-zero proposed trade."""
    return Decimal("0") if trade_value <= 0 else _money(fee_rule.fixed_fee + trade_value * fee_rule.percentage_fee)


def _blocked(
    proposal_id: int, portfolio_id: int, instrument_id: int, asset_class: str,
    current_quantity: Decimal, market_price: Optional[Decimal], current_weight: Decimal,
    target_weight: Decimal, reason: str, status: str,
) -> RebalanceRecommendation:
    """Make a visible no-trade decision when a safety rule blocks trading."""
    return RebalanceRecommendation(
        proposal_id, portfolio_id, instrument_id, asset_class, "HOLD", current_quantity,
        Decimal("0"), market_price, Decimal("0"), Decimal("0"), current_weight,
        target_weight, current_weight - target_weight, reason, status, "blocked",
    )


def _normal_hold(
    proposal_id: int, portfolio_id: int, instrument_id: int, asset_class: str,
    current_quantity: Decimal, market_price: Optional[Decimal], current_weight: Decimal,
    target_weight: Decimal,
) -> RebalanceRecommendation:
    """Make a normal HOLD when the allocation is already close enough to target."""
    return RebalanceRecommendation(
        proposal_id, portfolio_id, instrument_id, asset_class, "HOLD", current_quantity,
        Decimal("0"), market_price, Decimal("0"), Decimal("0"), current_weight,
        target_weight, current_weight - target_weight,
        "Allocation is within the configured drift tolerance; no trade is needed.",
        "CLEAR", "proposed",
    )


def _candidate_decision(
    *, proposal_id: int, portfolio_id: int, instrument_id: int, asset_class: str,
    current_quantity: Decimal, market_price: Optional[Decimal], current_weight: Decimal,
    target_weight: Decimal, desired_value: Decimal, action: str, fee_rule: FeeRule,
    minimum_trade: Decimal, excluded: set[int], liquidity_limit: Optional[Decimal],
    cash_available: Decimal,
) -> RebalanceRecommendation:
    """Apply safety checks to one calculated BUY or SELL candidate.

    The checks are intentionally ordered from data safety to policy rules. A
    failed check returns a visible blocked HOLD rather than disappearing.
    """
    if market_price is None:
        return _blocked(proposal_id, portfolio_id, instrument_id, asset_class, current_quantity, None, current_weight, target_weight, "Missing market price: trading is blocked.", "BLOCKED_MISSING_PRICE")
    proposed_quantity = _quantity(desired_value / market_price)
    trade_value = _money(proposed_quantity * market_price)
    estimated_fee = _fee_for(trade_value, fee_rule)
    if proposed_quantity == 0 or trade_value == 0:
        return _normal_hold(proposal_id, portfolio_id, instrument_id, asset_class, current_quantity, market_price, current_weight, target_weight)
    constraint_set = ConstraintSet(minimum_trade, frozenset(excluded), {instrument_id: liquidity_limit} if liquidity_limit is not None else {})
    failure = validate_trade_constraints(
        action=action, instrument_id=instrument_id, current_quantity=current_quantity,
        proposed_quantity=proposed_quantity, trade_value=trade_value, estimated_fee=estimated_fee,
        cash_available=cash_available, constraints=constraint_set,
    )
    if failure:
        return _blocked(proposal_id, portfolio_id, instrument_id, asset_class, current_quantity, market_price, current_weight, target_weight, failure.reason, failure.status)

    return RebalanceRecommendation(
        proposal_id, portfolio_id, instrument_id, asset_class, action, current_quantity,
        proposed_quantity, market_price, trade_value, estimated_fee, current_weight,
        target_weight, current_weight - target_weight,
        f"{asset_class} is {'overweight' if action == 'SELL' else 'underweight'} versus its target.",
        "CLEAR", "proposed",
    )


def _validate_target_weights(targets: dict[str, Decimal]) -> Optional[str]:
    """Return a clear problem message, or None when targets form a valid 100% set."""
    if not targets:
        return "No target allocation is available on or before the valuation date."
    if any(weight < 0 or weight > 1 for weight in targets.values()):
        return "A target allocation weight is outside the permitted 0% to 100% range."
    if abs(sum(targets.values(), Decimal("0")) - Decimal("1")) > Decimal("0.000001"):
        return "Target allocation weights do not total 100%."
    return None


def generate_rebalance_proposal(
    portfolio_id: int,
    valuation_date: date | str,
    database_path: Path = DATABASE_PATH,
    stale_price_threshold_days: int = STALE_PRICE_THRESHOLD_DAYS,
    drift_tolerance: Decimal = Decimal(str(ALLOCATION_DRIFT_TOLERANCE)),
) -> RebalanceProposalResult:
    """Save a new proposal containing safe recommendations for one portfolio.

    Sales are evaluated before buys. Only the net proceeds from accepted sales
    increase the temporary cash budget used to evaluate accepted buys.
    """
    as_of_date = _date(valuation_date)
    valuation = value_portfolio(portfolio_id, as_of_date, database_path, stale_price_threshold_days)
    targets, starting_cash, fee_rule, fee_problem, constraints = _read_policy_inputs(portfolio_id, as_of_date, database_path)

    with get_connection(database_path) as connection:
        proposal_cursor = connection.execute(
            "INSERT INTO rebalance_proposals (portfolio_id, proposal_date, status, total_portfolio_value, notes) VALUES (?, ?, ?, ?, ?)",
            (portfolio_id, as_of_date.isoformat(), "draft", float(valuation.total_portfolio_value), "Generated by the policy-constrained rebalancing engine."),
        )
        proposal_id = int(proposal_cursor.lastrowid)
        connection.execute("UPDATE rebalance_proposals SET external_id = ? WHERE proposal_id = ?", (f"RB-{as_of_date.year}-{proposal_id:06d}", proposal_id))

    valuation_by_instrument = {holding.instrument_id: holding for holding in valuation.holdings}
    asset_classes = sorted({holding.asset_class for holding in valuation.holdings} | set(targets))
    current_values = valuation.asset_class_values
    current_weights = {
        asset_class: (current_values.get(asset_class, Decimal("0")) / valuation.total_portfolio_value)
        if valuation.total_portfolio_value else Decimal("0")
        for asset_class in asset_classes
    }
    target_problem = _validate_target_weights(targets)
    global_problem: Optional[tuple[str, str]] = None
    if valuation.missing_price_count:
        global_problem = ("Price coverage is incomplete; trading is blocked until missing prices are resolved.", "BLOCKED_MISSING_PRICE")
    elif valuation.stale_price_count:
        global_problem = ("At least one market price is stale; refresh prices before trading.", "BLOCKED_STALE_PRICE")
    elif valuation.total_portfolio_value == 0:
        global_problem = ("Portfolio value is zero, so safe allocation adjustments cannot be calculated.", "BLOCKED_ZERO_PORTFOLIO_VALUE")
    elif target_problem:
        global_problem = (target_problem, "BLOCKED_INVALID_TARGET_ALLOCATION")
    elif fee_problem:
        global_problem = (fee_problem, "BLOCKED_INVALID_FEE_SCHEDULE")

    recommendations: list[RebalanceRecommendation] = []
    if global_problem:
        for holding in valuation.holdings:
            recommendations.append(_blocked(proposal_id, portfolio_id, holding.instrument_id, holding.asset_class, holding.quantity, holding.market_price, current_weights.get(holding.asset_class, Decimal("0")), targets.get(holding.asset_class, Decimal("0")), *global_problem))
    else:
        cash_available = starting_cash
        sell_candidates: list[tuple] = []
        buy_candidates: list[tuple] = []
        for asset_class in asset_classes:
            holding = next((item for item in valuation.holdings if item.asset_class == asset_class), None)
            if holding is None:
                continue
            current_weight = current_weights[asset_class]
            target_weight = targets.get(asset_class, Decimal("0"))
            drift = current_weight - target_weight
            if holding.price_status == "STALE":
                recommendations.append(_blocked(proposal_id, portfolio_id, holding.instrument_id, asset_class, holding.quantity, holding.market_price, current_weight, target_weight, "Stale market price: refresh the price before trading.", "BLOCKED_STALE_PRICE"))
            elif abs(drift) <= drift_tolerance:
                recommendations.append(_normal_hold(proposal_id, portfolio_id, holding.instrument_id, asset_class, holding.quantity, holding.market_price, current_weight, target_weight))
            elif drift > 0:
                sell_candidates.append((holding, current_weight, target_weight, _money(drift * valuation.total_portfolio_value)))
            else:
                buy_candidates.append((holding, current_weight, target_weight, _money((-drift) * valuation.total_portfolio_value)))

        for holding, current_weight, target_weight, desired_value in sell_candidates:
            limit = constraints.liquidity_limit_for(holding.instrument_id)
            decision = _candidate_decision(proposal_id=proposal_id, portfolio_id=portfolio_id, instrument_id=holding.instrument_id, asset_class=holding.asset_class, current_quantity=holding.quantity, market_price=holding.market_price, current_weight=current_weight, target_weight=target_weight, desired_value=desired_value, action="SELL", fee_rule=fee_rule or FeeRule(Decimal("0"), Decimal("0")), minimum_trade=constraints.minimum_trade_value, excluded=set(constraints.excluded_instruments), liquidity_limit=limit, cash_available=cash_available)
            recommendations.append(decision)
            if decision.approval_status == "proposed":
                cash_available += decision.trade_value - decision.estimated_fee

        for holding, current_weight, target_weight, desired_value in buy_candidates:
            limit = constraints.liquidity_limit_for(holding.instrument_id)
            decision = _candidate_decision(proposal_id=proposal_id, portfolio_id=portfolio_id, instrument_id=holding.instrument_id, asset_class=holding.asset_class, current_quantity=holding.quantity, market_price=holding.market_price, current_weight=current_weight, target_weight=target_weight, desired_value=desired_value, action="BUY", fee_rule=fee_rule or FeeRule(Decimal("0"), Decimal("0")), minimum_trade=constraints.minimum_trade_value, excluded=set(constraints.excluded_instruments), liquidity_limit=limit, cash_available=cash_available)
            recommendations.append(decision)
            if decision.approval_status == "proposed":
                cash_available -= decision.trade_value + decision.estimated_fee

    recommendations.sort(key=lambda item: item.instrument_id)
    with get_connection(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO trades (proposal_id, portfolio_id, instrument_id, trade_action, recommended_quantity,
                                estimated_price, estimated_fee, estimated_trade_value, trade_status, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (item.proposal_id, item.portfolio_id, item.instrument_id, item.action, float(item.proposed_quantity),
                 float(item.market_price) if item.market_price is not None else None, float(item.estimated_fee),
                 float(item.trade_value), item.approval_status,
                 f"{item.decision_reason} Constraint status: {item.constraint_status}")
                for item in recommendations
            ],
        )
        saved_trades = connection.execute(
            "SELECT trade_id, instrument_id, trade_action, trade_status FROM trades WHERE proposal_id = ? ORDER BY trade_id",
            (proposal_id,),
        ).fetchall()
    append_audit_event(
        "REBALANCE_PROPOSAL_CREATED",
        {"valuation_date": as_of_date, "total_portfolio_value": valuation.total_portfolio_value,
         "recommendation_count": len(recommendations)},
        entity_type="rebalance_proposal", entity_id=proposal_id, portfolio_id=portfolio_id,
        proposal_id=proposal_id, database_path=database_path,
    )
    for trade, recommendation in zip(saved_trades, recommendations):
        event_type = "CONSTRAINT_VIOLATION" if recommendation.approval_status == "blocked" else "TRADE_RECOMMENDATION_CREATED"
        append_audit_event(
            event_type,
            {"action": trade["trade_action"], "instrument_id": trade["instrument_id"],
             "constraint_status": recommendation.constraint_status, "reason": recommendation.decision_reason},
            entity_type="trade", entity_id=trade["trade_id"], portfolio_id=portfolio_id,
            proposal_id=proposal_id, trade_id=trade["trade_id"], database_path=database_path,
        )
    return RebalanceProposalResult(proposal_id, portfolio_id, as_of_date, valuation.total_portfolio_value, tuple(recommendations))
