"""Tests for reusable policy and fee validation."""

from decimal import Decimal
from pathlib import Path

from src.database.connection import get_connection
from src.services.synthetic_data import generate_synthetic_data
from src.services.constraints import ConstraintSet, validate_fee_rule, validate_trade_constraints
from src.services.rebalancer import FeeRule, _fee_for, _read_policy_inputs
from src.database.schema import initialize_database


def test_minimum_trade_and_exclusion_constraints_exist(tmp_path: Path) -> None:
    """Every portfolio gets a minimum trade rule; some get exclusions."""
    database_path = tmp_path / "test.db"
    generate_synthetic_data("starter", database_path, tmp_path / "exports")
    with get_connection(database_path) as connection:
        minimum_rules = connection.execute("SELECT COUNT(*) FROM \"constraints\" WHERE constraint_type = 'minimum_trade_value'").fetchone()[0]
        exclusions = connection.execute("SELECT COUNT(*) FROM \"constraints\" WHERE constraint_type = 'excluded_instrument'").fetchone()[0]
    assert minimum_rules == 100
    assert exclusions == 20


def test_fee_uses_fixed_plus_percentage_and_rounds_once() -> None:
    """One fee is fixed fee plus percentage of the trade value, rounded to cents."""
    fee = _fee_for(Decimal("100.01"), FeeRule(Decimal("1.25"), Decimal("0.0015")))
    assert fee == Decimal("1.40")  # 1.25 + (100.01 × 0.0015) = 1.400015


def test_fee_schedule_uses_most_recent_effective_rule(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES ('INV-000001', 'Test', 'test@example.test', 'MODERATE', 10)")
        connection.execute("INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES ('PORT-INV-000001-01', 1, 'Test', 'USD', 0)")
        connection.execute("INSERT INTO fee_schedules (portfolio_id, schedule_name, fixed_fee, percentage_fee, effective_from) VALUES (1, 'Old', 9.99, 0.01, '2026-01-01')")
        connection.execute("INSERT INTO fee_schedules (portfolio_id, schedule_name, fixed_fee, percentage_fee, effective_from) VALUES (1, 'New', 1.25, 0.0015, '2026-10-01')")
    _, _, rule, problem, _ = _read_policy_inputs(1, __import__('datetime').date(2026, 10, 22), database_path)
    assert problem is None
    assert rule == FeeRule(Decimal("1.25"), Decimal("0.0015"))


def test_invalid_fee_and_blocked_trade_have_clear_failures() -> None:
    """Bad fee inputs and policy violations return visible reasons, never silence."""
    assert validate_fee_rule(Decimal("-1"), Decimal("0")) is not None
    constraints = ConstraintSet(Decimal("50"), frozenset({7}), {7: Decimal("500")})
    failure = validate_trade_constraints(
        action="BUY", instrument_id=7, current_quantity=Decimal("0"), proposed_quantity=Decimal("10"),
        trade_value=Decimal("100"), estimated_fee=Decimal("1"), cash_available=Decimal("1_000"), constraints=constraints,
    )
    assert failure is not None and failure.status == "BLOCKED_EXCLUDED_INSTRUMENT"
