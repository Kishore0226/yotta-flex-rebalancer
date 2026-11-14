"""Current-versus-target allocation analysis used by the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.config import ALLOCATION_DRIFT_TOLERANCE, DATABASE_PATH
from src.database.connection import get_connection
from src.services.valuation import PortfolioValuation, value_portfolio


@dataclass(frozen=True)
class AllocationRow:
    """One asset class's current value, target, drift, and clear status."""

    asset_class: str
    current_value: Decimal
    current_weight: Decimal
    target_weight: Decimal
    drift: Decimal
    status: str


@dataclass(frozen=True)
class AllocationAnalysis:
    """Allocation results plus the valuation quality on which they depend."""

    portfolio_id: int
    valuation: PortfolioValuation
    rows: tuple[AllocationRow, ...]


def analyze_allocation(
    portfolio_id: int,
    valuation_date: date | str,
    database_path: Path = DATABASE_PATH,
    drift_tolerance: Decimal = Decimal(str(ALLOCATION_DRIFT_TOLERANCE)),
) -> AllocationAnalysis:
    """Compare actual asset-class weights with the latest eligible targets.

    Missing prices are not hidden: their counts remain available through the
    included valuation object, so the UI can show a clear warning.
    """
    valuation = value_portfolio(portfolio_id, valuation_date, database_path)
    date_text = valuation.valuation_date.isoformat()
    with get_connection(database_path) as connection:
        target_date = connection.execute(
            "SELECT MAX(target_date) FROM target_allocations WHERE portfolio_id = ? AND target_date <= ?",
            (portfolio_id, date_text),
        ).fetchone()[0]
        target_rows = [] if target_date is None else connection.execute(
            """
            SELECT i.asset_class, SUM(ta.target_weight) AS target_weight
            FROM target_allocations AS ta JOIN instruments AS i ON i.instrument_id = ta.instrument_id
            WHERE ta.portfolio_id = ? AND ta.target_date = ? GROUP BY i.asset_class
            """,
            (portfolio_id, target_date),
        ).fetchall()
    targets = {row["asset_class"]: Decimal(str(row["target_weight"])) for row in target_rows}
    asset_classes = sorted(set(valuation.asset_class_values) | set(targets))
    rows = []
    for asset_class in asset_classes:
        value = valuation.asset_class_values.get(asset_class, Decimal("0"))
        current_weight = value / valuation.total_portfolio_value if valuation.total_portfolio_value else Decimal("0")
        target_weight = targets.get(asset_class, Decimal("0"))
        drift = current_weight - target_weight
        status = "ON_TARGET" if abs(drift) <= drift_tolerance else ("OVERWEIGHT" if drift > 0 else "UNDERWEIGHT")
        rows.append(AllocationRow(asset_class, value, current_weight, target_weight, drift, status))
    return AllocationAnalysis(portfolio_id, valuation, tuple(rows))
