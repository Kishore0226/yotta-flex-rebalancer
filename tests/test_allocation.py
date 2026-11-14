"""Tests for Phase 2 allocation source data."""

from pathlib import Path

from src.database.connection import get_connection
from src.database.repositories import list_portfolios, list_proposals
from src.services.allocation import analyze_allocation
from src.services.synthetic_data import generate_synthetic_data


def test_each_portfolio_target_weights_total_one(tmp_path: Path) -> None:
    """Risk-based targets must total exactly 100 percent per portfolio."""
    database_path = tmp_path / "test.db"
    generate_synthetic_data("starter", database_path, tmp_path / "exports")
    with get_connection(database_path) as connection:
        totals = connection.execute(
            "SELECT portfolio_id, ROUND(SUM(target_weight), 8) AS total FROM target_allocations GROUP BY portfolio_id"
        ).fetchall()
    assert len(totals) == 100
    assert all(row["total"] == 1.0 for row in totals)


def test_dashboard_selector_data_and_allocation_analysis_use_existing_services(tmp_path: Path) -> None:
    """Dashboard-facing data comes from SQLite and the shared valuation service."""
    database_path = tmp_path / "test.db"
    generate_synthetic_data("starter", database_path, tmp_path / "exports")
    portfolios = list_portfolios(database_path)
    analysis = analyze_allocation(1, "2026-10-22", database_path)
    assert portfolios[0]["portfolio_id"] == 1
    assert len(analysis.rows) == 5
    assert analysis.valuation.price_coverage_percentage == 100
    assert list_proposals(1, database_path) == []
