"""Tests for the read-only post-trade proposal simulator."""

from decimal import Decimal
from pathlib import Path

from src.database.connection import get_connection
from src.database.schema import initialize_database
from src.services.rebalancer import generate_rebalance_proposal
from src.services.simulation import simulate_proposal


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


def _manual_proposal(database_path: Path, action: str, quantity: float, fee: float = 0) -> int:
    with get_connection(database_path) as connection:
        proposal_id = connection.execute("INSERT INTO rebalance_proposals (portfolio_id, proposal_date, status, total_portfolio_value) VALUES (1, '2026-10-22', 'draft', 0)").lastrowid
        connection.execute("INSERT INTO trades (proposal_id, portfolio_id, instrument_id, trade_action, recommended_quantity, estimated_price, estimated_fee, estimated_trade_value, trade_status, rationale) VALUES (?, 1, 1, ?, ?, 10, ?, 0, 'proposed', 'Manual test')", (proposal_id, action, quantity, fee))
    return int(proposal_id)


def test_successful_buy_sell_and_multiple_trade_simulation(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db)
    _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    proposal = generate_rebalance_proposal(1, '2026-10-22', db)
    result = simulate_proposal(1, proposal.proposal_id, '2026-10-22', db)
    quantities = {item.instrument_id: item.simulated_quantity for item in result.holdings}
    assert len(result.accepted_sells) == 1 and len(result.accepted_buys) == 1
    assert quantities[1] == Decimal('0.0000')
    assert quantities[2] == Decimal('20.0000')


def test_blocked_trades_are_never_simulated(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db)
    _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    with get_connection(db) as connection:
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, numeric_value, description) VALUES (1, 1, 'liquidity_limit', 10, 'Cap')")
    proposal = generate_rebalance_proposal(1, '2026-10-22', db)
    result = simulate_proposal(1, proposal.proposal_id, '2026-10-22', db)
    assert len(result.accepted_sells) == 0
    assert {item.instrument_id: item.simulated_quantity for item in result.holdings}[1] == Decimal('10.0')


def test_simulation_rechecks_exclusion_for_a_saved_proposed_trade(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db); _holding(db, 1, 'EQUITY', 10)
    proposal_id = _manual_proposal(db, 'SELL', 1)
    with get_connection(db) as connection:
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, description) VALUES (1, 1, 'excluded_instrument', 'Excluded')")
    result = simulate_proposal(1, proposal_id, '2026-10-22', db)
    assert result.blocked_trades[0].reason == 'Instrument is excluded by policy.'


def test_insufficient_cash_and_holdings_are_blocked_in_simulation(tmp_path: Path) -> None:
    cash_db = tmp_path / 'cash.db'; _setup(cash_db, cash=0); _holding(cash_db, 1, 'EQUITY', 10)
    cash_result = simulate_proposal(1, _manual_proposal(cash_db, 'BUY', 20), '2026-10-22', cash_db)
    assert 'insufficient' in cash_result.blocked_trades[0].reason.lower()

    holding_db = tmp_path / 'holding.db'; _setup(holding_db); _holding(holding_db, 1, 'EQUITY', 10)
    holding_result = simulate_proposal(1, _manual_proposal(holding_db, 'SELL', 20), '2026-10-22', holding_db)
    assert 'exceeds available holding' in holding_result.blocked_trades[0].reason.lower()


def test_transaction_fee_is_included_once_in_cash(tmp_path: Path) -> None:
    db = tmp_path / 'test.db'; _setup(db, cash=20); _holding(db, 1, 'EQUITY', 10)
    result = simulate_proposal(1, _manual_proposal(db, 'BUY', 1, fee=1), '2026-10-22', db)
    assert result.total_fees == Decimal('1.00')
    assert result.simulated_remaining_cash == Decimal('9.00')


def test_missing_and_stale_prices_are_blocked(tmp_path: Path) -> None:
    missing_db = tmp_path / 'missing.db'; _setup(missing_db); _holding(missing_db, 1, 'EQUITY', 10, None)
    assert 'Missing market price' in simulate_proposal(1, _manual_proposal(missing_db, 'SELL', 1), '2026-10-22', missing_db).blocked_trades[0].reason
    stale_db = tmp_path / 'stale.db'; _setup(stale_db); _holding(stale_db, 1, 'EQUITY', 10, 10, '2026-10-01')
    assert 'Stale market price' in simulate_proposal(1, _manual_proposal(stale_db, 'SELL', 1), '2026-10-22', stale_db).blocked_trades[0].reason


def test_zero_holdings_is_safe_and_simulation_is_repeatable_without_mutation(tmp_path: Path) -> None:
    empty = tmp_path / 'empty.db'; _setup(empty)
    with get_connection(empty) as connection:
        proposal_id = connection.execute("INSERT INTO rebalance_proposals (portfolio_id, proposal_date, status, total_portfolio_value) VALUES (1, '2026-10-22', 'draft', 0)").lastrowid
    empty_result = simulate_proposal(1, int(proposal_id), '2026-10-22', empty)
    assert empty_result.simulated_portfolio_value == Decimal('0.00')

    db = tmp_path / 'repeat.db'; _setup(db); _holding(db, 1, 'EQUITY', 10); _holding(db, 2, 'BOND', 10); _targets(db, [(1, 0), (2, 1)])
    proposal = generate_rebalance_proposal(1, '2026-10-22', db)
    with get_connection(db) as connection:
        original_quantity = connection.execute('SELECT quantity FROM holdings WHERE instrument_id = 1').fetchone()[0]
        original_cash = connection.execute('SELECT available_cash FROM portfolios WHERE portfolio_id = 1').fetchone()[0]
    first = simulate_proposal(1, proposal.proposal_id, '2026-10-22', db)
    second = simulate_proposal(1, proposal.proposal_id, '2026-10-22', db)
    with get_connection(db) as connection:
        assert connection.execute('SELECT quantity FROM holdings WHERE instrument_id = 1').fetchone()[0] == original_quantity
        assert connection.execute('SELECT available_cash FROM portfolios WHERE portfolio_id = 1').fetchone()[0] == original_cash
    assert first == second
