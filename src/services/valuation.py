"""Read-only portfolio valuation using the latest eligible market prices.

This module never changes holdings or prices. It reads the source records from
SQLite, then performs financial calculations with :class:`decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from src.config import DATABASE_PATH, STALE_PRICE_THRESHOLD_DAYS, SYNTHETIC_AS_OF_DATE
from src.database.connection import get_connection
from src.services.audit import append_audit_event

MONEY_PRECISION = Decimal("0.01")
PERCENT_PRECISION = Decimal("0.01")


@dataclass(frozen=True)
class HoldingPriceInput:
    """One holding plus the latest price that was eligible for valuation."""

    portfolio_id: int
    instrument_id: int
    asset_class: str
    quantity: Decimal
    market_price: Optional[Decimal]
    price_date: Optional[date]


@dataclass(frozen=True)
class HoldingValuation:
    """The valuation result for one instrument held by a portfolio."""

    portfolio_id: int
    instrument_id: int
    asset_class: str
    quantity: Decimal
    market_price: Optional[Decimal]
    price_date: Optional[date]
    market_value: Optional[Decimal]
    price_status: str


@dataclass(frozen=True)
class PortfolioValuation:
    """Portfolio total, asset-class subtotals, and price-quality indicators."""

    portfolio_id: int
    valuation_date: date
    holdings: tuple[HoldingValuation, ...]
    total_portfolio_value: Decimal
    asset_class_values: dict[str, Decimal]
    missing_price_count: int
    stale_price_count: int
    price_coverage_percentage: Decimal


def _as_date(value: date | str | None) -> date:
    """Accept a Python date, ISO date string, or the configured default date."""
    if value is None:
        return SYNTHETIC_AS_OF_DATE
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _to_decimal(value: object | None) -> Optional[Decimal]:
    """Convert SQLite numeric values through text to avoid binary-float noise."""
    return None if value is None else Decimal(str(value))


def read_portfolio_prices(
    portfolio_id: int,
    valuation_date: date | str | None = None,
    database_path: Path = DATABASE_PATH,
) -> list[HoldingPriceInput]:
    """Read holdings and their latest prices on or before a valuation date.

    The correlated query deliberately uses ``price_date <= valuation_date``.
    A future price can therefore never enter the calculation.
    """
    requested_date = _as_date(valuation_date)
    with get_connection(database_path) as connection:
        portfolio_exists = connection.execute(
            "SELECT 1 FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio_exists is None:
            raise ValueError(f"Portfolio {portfolio_id} does not exist.")

        rows = connection.execute(
            """
            SELECT h.portfolio_id, h.instrument_id, i.asset_class, h.quantity,
                   latest.closing_price, latest.price_date
            FROM holdings AS h
            INNER JOIN instruments AS i ON i.instrument_id = h.instrument_id
            LEFT JOIN market_prices AS latest ON latest.market_price_id = (
                SELECT mp.market_price_id
                FROM market_prices AS mp
                WHERE mp.instrument_id = h.instrument_id
                  AND mp.price_date <= ?
                ORDER BY mp.price_date DESC, mp.market_price_id DESC
                LIMIT 1
            )
            WHERE h.portfolio_id = ?
            ORDER BY h.instrument_id
            """,
            (requested_date.isoformat(), portfolio_id),
        ).fetchall()

    return [
        HoldingPriceInput(
            portfolio_id=row["portfolio_id"],
            instrument_id=row["instrument_id"],
            asset_class=row["asset_class"],
            quantity=_to_decimal(row["quantity"]) or Decimal("0"),
            market_price=_to_decimal(row["closing_price"]),
            price_date=date.fromisoformat(row["price_date"]) if row["price_date"] else None,
        )
        for row in rows
    ]


def calculate_portfolio_valuation(
    portfolio_id: int,
    holding_prices: list[HoldingPriceInput],
    valuation_date: date | str | None = None,
    stale_price_threshold_days: int = STALE_PRICE_THRESHOLD_DAYS,
) -> PortfolioValuation:
    """Calculate a structured valuation from previously-read holdings and prices.

    Missing prices are excluded from the total because no honest market value is
    available. Stale prices remain in the total but are explicitly labelled so
    a caller can decide whether that estimate is acceptable.
    """
    if stale_price_threshold_days < 0:
        raise ValueError("stale_price_threshold_days cannot be negative.")
    requested_date = _as_date(valuation_date)
    holdings: list[HoldingValuation] = []
    asset_class_values: dict[str, Decimal] = {}
    missing_price_count = 0
    stale_price_count = 0

    for item in holding_prices:
        if item.portfolio_id != portfolio_id:
            raise ValueError("Every holding price input must belong to the requested portfolio.")
        if item.market_price is None or item.price_date is None:
            status = "MISSING"
            market_value = None
            missing_price_count += 1
        else:
            age_in_days = (requested_date - item.price_date).days
            status = "STALE" if age_in_days > stale_price_threshold_days else "CURRENT"
            if status == "STALE":
                stale_price_count += 1
            market_value = (item.quantity * item.market_price).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
            asset_class_values[item.asset_class] = asset_class_values.get(item.asset_class, Decimal("0")) + market_value

        holdings.append(
            HoldingValuation(
                portfolio_id=item.portfolio_id,
                instrument_id=item.instrument_id,
                asset_class=item.asset_class,
                quantity=item.quantity,
                market_price=item.market_price,
                price_date=item.price_date,
                market_value=market_value,
                price_status=status,
            )
        )

    total_portfolio_value = sum(asset_class_values.values(), Decimal("0")).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
    asset_class_values = {
        asset_class: value.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
        for asset_class, value in asset_class_values.items()
    }
    coverage = Decimal("0") if not holdings else (
        Decimal(len(holdings) - missing_price_count) / Decimal(len(holdings)) * Decimal("100")
    )
    return PortfolioValuation(
        portfolio_id=portfolio_id,
        valuation_date=requested_date,
        holdings=tuple(holdings),
        total_portfolio_value=total_portfolio_value,
        asset_class_values=asset_class_values,
        missing_price_count=missing_price_count,
        stale_price_count=stale_price_count,
        price_coverage_percentage=coverage.quantize(PERCENT_PRECISION, rounding=ROUND_HALF_UP),
    )


def value_portfolio(
    portfolio_id: int,
    valuation_date: date | str | None = None,
    database_path: Path = DATABASE_PATH,
    stale_price_threshold_days: int = STALE_PRICE_THRESHOLD_DAYS,
) -> PortfolioValuation:
    """Read and value a portfolio without writing any database records."""
    requested_date = _as_date(valuation_date)
    holding_prices = read_portfolio_prices(portfolio_id, requested_date, database_path)
    result = calculate_portfolio_valuation(
        portfolio_id, holding_prices, requested_date, stale_price_threshold_days
    )
    append_audit_event(
        "VALUATION_EXECUTED",
        {"valuation_date": requested_date, "total_portfolio_value": result.total_portfolio_value,
         "price_coverage_percentage": result.price_coverage_percentage,
         "missing_price_count": result.missing_price_count, "stale_price_count": result.stale_price_count},
        entity_type="portfolio", entity_id=portfolio_id, portfolio_id=portfolio_id, database_path=database_path,
    )
    return result
