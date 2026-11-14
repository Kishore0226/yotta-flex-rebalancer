"""Tests for the policy-constrained rebalancing recommendation engine."""

from pathlib import Path

from src.database.connection import get_connection
from src.database.schema import initialize_database
from decimal import Decimal

from src.services.rebalancer import FeeRule, _candidate_decision, generate_rebalance_proposal


def _setup(database_path: Path, cash: float = 0) -> None:
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES ('INV-000001', 'Test', 'test@example.test', 'MODERATE', 10)")
        connection.execute("INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES ('PORT-INV-000001-01', 1, 'Test', 'USD', ?)", (cash,))
        connection.execute("INSERT INTO fee_schedules (portfolio_id, schedule_name, fixed_fee, percentage_fee, effective_from) VALUES (1, 'Test', 0, 0, '2026-01-01')")


def _holding(database_path: Path, instrument_id: int, asset_class: str, quantity: float, price: float | None = 10, price_date: str = '2026-10-22') -> None:
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO instruments (external_id, ticker, instrument_name, asset_class, currency, is_active) VALUES (?, ?, 'Test', ?, 'USD', 1)", (f'INS-{instrument_id:06d}', f'T{instrument_id}', asset_class))
        connection.execute("INSERT INTO holdings (external_id, portfolio_id, instrument_id, quantity, average_cost) VALUES (?, 1, ?, ?, 1)", (f'HOLD-1-{instrument_id}', instrument_id, quantity))
        if price is not None:
            connection.execute("INSERT INTO market_prices (instrument_id, price_date, closing_price, average_daily_volume) VALUES (?, ?, ?, 100000)", (instrument_id, price_date, price))


def _targets(database_path: Path, weights: list[tuple[int, float]]) -> None:
    with get_connection(database_path) as connection:
        connection.executemany("INSERT INTO target_allocations (portfolio_id, instrument_id, target_date, target_weight) VALUES (1, ?, '2026-10-22', ?)", weights)


def _proposal(database_path: Path):
    return generate_rebalance_proposal(1, '2026-10-22', database_path)


def test_normal_buy_and_sell_recommendations(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db)
    _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    decisions = {item.action for item in _proposal(db).recommendations}
    assert decisions == {'BUY', 'SELL'}


def test_hold_recommendation_when_on_target(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _targets(db, [(1, 1)])
    result = _proposal(db).recommendations[0]
    assert result.action == 'HOLD' and result.approval_status == 'proposed'


def test_excluded_instrument_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    with get_connection(db) as connection: connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, description) VALUES (1, 1, 'excluded_instrument', 'No equity')")
    assert any(item.constraint_status == 'BLOCKED_EXCLUDED_INSTRUMENT' for item in _proposal(db).recommendations)


def test_insufficient_cash_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db, cash=0); _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 0); _targets(db, [(1, 1), (2, 0)])
    # Change targets so BOND needs a buy but EQUITY sale is excluded.
    with get_connection(db) as connection:
        connection.execute("UPDATE target_allocations SET target_weight = 0 WHERE instrument_id = 1")
        connection.execute("UPDATE target_allocations SET target_weight = 1 WHERE instrument_id = 2")
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, description) VALUES (1, 1, 'excluded_instrument', 'No sales')")
    assert any(item.constraint_status == 'BLOCKED_INSUFFICIENT_CASH' for item in _proposal(db).recommendations)


def test_minimum_trade_size_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0.51), (2, 0.49)])
    with get_connection(db) as connection: connection.execute("INSERT INTO \"constraints\" (portfolio_id, constraint_type, numeric_value, description) VALUES (1, 'minimum_trade_value', 50, 'Min')")
    assert any(item.constraint_status == 'BLOCKED_MINIMUM_TRADE_SIZE' for item in _proposal(db).recommendations)


def test_insufficient_holdings_is_blocked_before_a_sell(tmp_path: Path) -> None:
    """A defensive check prevents a caller from selling more than is held."""
    decision = _candidate_decision(
        proposal_id=1, portfolio_id=1, instrument_id=1, asset_class='EQUITY',
        current_quantity=Decimal('10'), market_price=Decimal('10'),
        current_weight=Decimal('1'), target_weight=Decimal('0'),
        desired_value=Decimal('200'), action='SELL', fee_rule=FeeRule(Decimal('0'), Decimal('0')),
        minimum_trade=Decimal('0'), excluded=set(), liquidity_limit=None,
        cash_available=Decimal('0'),
    )
    assert decision.constraint_status == 'BLOCKED_INSUFFICIENT_HOLDINGS'


def test_missing_and_stale_prices_are_blocked(tmp_path: Path) -> None:
    missing_db = tmp_path / 'missing.db'; _setup(missing_db); _holding(missing_db, 1, 'EQUITY', 10, None); _targets(missing_db, [(1, 1)])
    assert _proposal(missing_db).recommendations[0].constraint_status == 'BLOCKED_MISSING_PRICE'
    stale_db = tmp_path / 'stale.db'; _setup(stale_db); _holding(stale_db, 1, 'EQUITY', 10, 10, '2026-10-01'); _targets(stale_db, [(1, 1)])
    assert _proposal(stale_db).recommendations[0].constraint_status == 'BLOCKED_STALE_PRICE'


def test_liquidity_limit_is_blocked_and_records_are_valid(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    with get_connection(db) as connection: connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, numeric_value, description) VALUES (1, 1, 'liquidity_limit', 10, 'Cap')")
    result = _proposal(db)
    with get_connection(db) as connection:
        saved = connection.execute('SELECT COUNT(*) FROM trades WHERE proposal_id = ?', (result.proposal_id,)).fetchone()[0]
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
    assert any(item.constraint_status == 'BLOCKED_LIQUIDITY_LIMIT' for item in result.recommendations)
    assert saved == 2 and violations == []


def test_missing_fee_schedule_blocks_all_recommendations(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _targets(db, [(1, 1)])
    with get_connection(db) as connection:
        connection.execute('DELETE FROM fee_schedules WHERE portfolio_id = 1')
    result = _proposal(db)
    assert all(item.constraint_status == 'BLOCKED_INVALID_FEE_SCHEDULE' for item in result.recommendations)


def test_cash_uses_net_sell_proceeds_and_buy_fee(tmp_path: Path) -> None:
    """A $100 sell with a $10 fee creates only $90 of buying power, not $100."""
    db = tmp_path / 'test.db'; _setup(db, cash=0)
    _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    with get_connection(db) as connection:
        connection.execute('UPDATE fee_schedules SET fixed_fee = 10, percentage_fee = 0 WHERE portfolio_id = 1')
    decisions = {item.instrument_id: item for item in _proposal(db).recommendations}
    assert decisions[1].action == 'SELL' and decisions[1].estimated_fee == Decimal('10.00')
    assert decisions[2].constraint_status == 'BLOCKED_INSUFFICIENT_CASH'


def test_blocked_sell_does_not_fund_buy(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db, cash=0)
    _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    with get_connection(db) as connection:
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, numeric_value, description) VALUES (1, 1, 'liquidity_limit', 10, 'Blocked sale')")
    decisions = {item.instrument_id: item for item in _proposal(db).recommendations}
    assert decisions[1].constraint_status == 'BLOCKED_LIQUIDITY_LIMIT'
    assert decisions[2].constraint_status == 'BLOCKED_INSUFFICIENT_CASH'


def test_maximum_trade_value_and_invalid_targets_are_blocked(tmp_path: Path) -> None:
    capped = tmp_path / 'capped.db'; _setup(capped); _holding(capped, 1, 'EQUITY', 10); _holding(capped, 2, 'BOND', 10); _targets(capped, [(1, 0), (2, 1)])
    with get_connection(capped) as connection:
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, numeric_value, description) VALUES (1, 1, 'maximum_trade_value', 10, 'Cap')")
    assert any(item.constraint_status == 'BLOCKED_LIQUIDITY_LIMIT' for item in _proposal(capped).recommendations)

    invalid = tmp_path / 'invalid.db'; _setup(invalid); _holding(invalid, 1, 'EQUITY', 10); _targets(invalid, [(1, 0.90)])
    assert _proposal(invalid).recommendations[0].constraint_status == 'BLOCKED_INVALID_TARGET_ALLOCATION'
