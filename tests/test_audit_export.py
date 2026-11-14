"""Tests for append-only audit events and safe CSV/JSON exports."""

import json
from decimal import Decimal
from pathlib import Path

from src.database.connection import get_connection
from src.database.schema import initialize_database
from src.services.audit import append_audit_event, list_audit_events
from src.services.export import DATABASE_TABLES, _write_records, export_database_snapshot
from src.services.rebalancer import generate_rebalance_proposal


def test_audit_events_are_append_only_and_keep_entity_details(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"; initialize_database(database_path)
    first = append_audit_event("PORTFOLIO_CREATED", {"source": "test"}, entity_type="portfolio", entity_id=1, database_path=database_path)
    second = append_audit_event("VALUATION_EXECUTED", {"total": Decimal("12.30")}, entity_type="portfolio", entity_id=1, database_path=database_path)
    events = list_audit_events(database_path)
    assert [event.audit_event_id for event in events] == [first, second]
    assert events[1].event_details["total"] == "12.30"
    with get_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2


def test_snapshot_exports_csv_json_empty_data_and_anomalies_separately(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"; initialize_database(database_path)
    destination = export_database_snapshot(database_path=database_path, export_root=tmp_path / "exports", mode="starter")
    for table in DATABASE_TABLES:
        assert (destination / f"{table}.csv").exists()
        assert (destination / f"{table}.json").exists()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ground_truth_included"] is False
    assert json.loads((destination / "anomalies.json").read_text(encoding="utf-8")) == []


def test_export_decimal_serialization_and_content_are_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"; second = tmp_path / "second"; first.mkdir(); second.mkdir()
    _write_records(first, "decimal", [{"amount": Decimal("1.20"), "name": "test"}])
    _write_records(second, "decimal", [{"amount": Decimal("1.20"), "name": "test"}])
    assert json.loads((first / "decimal.json").read_text(encoding="utf-8"))[0]["amount"] == "1.20"
    assert (first / "decimal.csv").read_text(encoding="utf-8") == (second / "decimal.csv").read_text(encoding="utf-8")
    assert (first / "decimal.json").read_text(encoding="utf-8") == (second / "decimal.json").read_text(encoding="utf-8")


def test_snapshot_preserves_historical_records(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"; initialize_database(database_path)
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES ('INV-000001', 'Test', 'test@example.test', 'MODERATE', 10)")
        connection.execute("INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES ('PORT-INV-000001-01', 1, 'Test', 'USD', 200)")
        proposal_id = connection.execute("INSERT INTO rebalance_proposals (portfolio_id, proposal_date, status, total_portfolio_value) VALUES (1, '2026-10-22', 'draft', 0)").lastrowid
    export_database_snapshot(database_path=database_path, export_root=tmp_path / "exports")
    with get_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rebalance_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()[0] == 1


def test_new_proposal_appends_proposal_trade_and_constraint_audits(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"; initialize_database(database_path)
    with get_connection(database_path) as connection:
        connection.execute("INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES ('INV-000001', 'Test', 'test@example.test', 'MODERATE', 10)")
        connection.execute("INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES ('PORT-INV-000001-01', 1, 'Test', 'USD', 200)")
        connection.execute("INSERT INTO fee_schedules (portfolio_id, schedule_name, fixed_fee, percentage_fee, effective_from) VALUES (1, 'Test', 0, 0, '2026-01-01')")
        for instrument_id, asset_class in ((1, 'EQUITY'), (2, 'BOND')):
            connection.execute("INSERT INTO instruments (external_id, ticker, instrument_name, asset_class, currency, is_active) VALUES (?, ?, 'Test', ?, 'USD', 1)", (f'INS-{instrument_id:06d}', f'T{instrument_id}', asset_class))
            connection.execute("INSERT INTO holdings (external_id, portfolio_id, instrument_id, quantity, average_cost) VALUES (?, 1, ?, 10, 1)", (f'HOLD-1-{instrument_id}', instrument_id))
            connection.execute("INSERT INTO market_prices (instrument_id, price_date, closing_price, average_daily_volume) VALUES (?, '2026-10-22', 10, 100000)", (instrument_id,))
        connection.executemany("INSERT INTO target_allocations (portfolio_id, instrument_id, target_date, target_weight) VALUES (1, ?, '2026-10-22', ?)", [(1, 0), (2, 1)])
        connection.execute("INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, description) VALUES (1, 1, 'excluded_instrument', 'Excluded')")
    generate_rebalance_proposal(1, '2026-10-22', database_path)
    event_types = [event.event_type for event in list_audit_events(database_path)]
    assert 'REBALANCE_PROPOSAL_CREATED' in event_types
    assert 'TRADE_RECOMMENDATION_CREATED' in event_types
    assert 'CONSTRAINT_VIOLATION' in event_types
