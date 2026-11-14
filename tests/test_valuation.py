"""Tests for Phase 3 portfolio valuation."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import get_connection
from src.database.schema import initialize_database
from src.services.synthetic_data import generate_synthetic_data
from src.services.valuation import value_portfolio


def _create_portfolio(database_path: Path, portfolio_id: int = 1) -> None:
    """Create the smallest valid parent set for an isolated valuation test."""
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES (?, ?, ?, ?, ?)", ("INV-000001", "Test Investor", "test@example.test", "MODERATE", 10))
        connection.execute("INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES (?, ?, ?, ?, ?)", ("PORT-INV-000001-01", 1, "Test Portfolio", "USD", 0))


def _add_holding(database_path: Path, instrument_id: int, asset_class: str, quantity: float) -> None:
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO instruments (external_id, ticker, instrument_name, asset_class, currency, is_active) VALUES (?, ?, ?, ?, ?, ?)", (f"INS-{instrument_id:06d}", f"T{instrument_id}", "Test Instrument", asset_class, "USD", 1))
        connection.execute("INSERT INTO holdings (external_id, portfolio_id, instrument_id, quantity, average_cost) VALUES (?, ?, ?, ?, ?)", (f"HOLD-1-{instrument_id}", 1, instrument_id, quantity, 1))


def _add_price(database_path: Path, instrument_id: int, price_date: str, price: float) -> None:
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO market_prices (instrument_id, price_date, closing_price, average_daily_volume) VALUES (?, ?, ?, ?)", (instrument_id, price_date, price, 100_000))


def test_normal_valuation_uses_quantity_times_latest_price(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)
    _add_holding(database_path, 1, "EQUITY", 2)
    _add_price(database_path, 1, "2026-10-21", 12.50)

    result = value_portfolio(1, "2026-10-22", database_path)

    assert result.total_portfolio_value == Decimal("25.00")
    assert result.holdings[0].price_status == "CURRENT"
    assert result.price_coverage_percentage == Decimal("100.00")


def test_missing_price_is_explicit_and_excluded_from_total(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)
    _add_holding(database_path, 1, "EQUITY", 2)

    result = value_portfolio(1, "2026-10-22", database_path)

    assert result.total_portfolio_value == Decimal("0.00")
    assert result.missing_price_count == 1
    assert result.holdings[0].market_value is None
    assert result.holdings[0].price_status == "MISSING"


def test_stale_price_is_labelled(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)
    _add_holding(database_path, 1, "BOND", 3)
    _add_price(database_path, 1, "2026-10-10", 10)

    result = value_portfolio(1, "2026-10-22", database_path, stale_price_threshold_days=5)

    assert result.total_portfolio_value == Decimal("30.00")
    assert result.stale_price_count == 1
    assert result.holdings[0].price_status == "STALE"


def test_zero_holding_portfolio_is_safe(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)

    result = value_portfolio(1, "2026-10-22", database_path)

    assert result.total_portfolio_value == Decimal("0.00")
    assert result.holdings == ()
    assert result.price_coverage_percentage == Decimal("0.00")


def test_multiple_instruments_create_asset_class_totals(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)
    _add_holding(database_path, 1, "EQUITY", 2)
    _add_holding(database_path, 2, "BOND", 3)
    _add_price(database_path, 1, "2026-10-22", 10)
    _add_price(database_path, 2, "2026-10-22", 20)

    result = value_portfolio(1, "2026-10-22", database_path)

    assert result.total_portfolio_value == Decimal("80.00")
    assert result.asset_class_values == {"EQUITY": Decimal("20.00"), "BOND": Decimal("60.00")}


def test_future_prices_are_never_used(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    _create_portfolio(database_path)
    _add_holding(database_path, 1, "ETF", 2)
    _add_price(database_path, 1, "2026-10-21", 10)
    _add_price(database_path, 1, "2026-10-23", 999)

    result = value_portfolio(1, date(2026, 10, 22), database_path)

    assert result.holdings[0].price_date == date(2026, 10, 21)
    assert result.holdings[0].market_price == Decimal("10.0")
    assert result.total_portfolio_value == Decimal("20.00")


def test_starter_generator_creates_expected_source_rows(tmp_path: Path) -> None:
    """Keep Phase 2's generation coverage while adding valuation tests."""
    counts = generate_synthetic_data("starter", tmp_path / "test.db", tmp_path / "exports")
    assert counts["market_prices"] == 30_000
