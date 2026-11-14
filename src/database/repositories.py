"""Small data-access helpers used by the data generator and later services."""

from collections.abc import Iterable
import sqlite3
from pathlib import Path

from src.database.schema import initialize_database, list_tables
from src.database.connection import get_connection
from src.config import DATABASE_PATH


def setup_database() -> list[str]:
    """Create the schema and return the resulting table names."""
    initialize_database()
    return list_tables()


def insert_many_ignore(
    connection: sqlite3.Connection, statement: str, records: Iterable[tuple],
) -> None:
    """Insert records in bulk while making repeat generator runs safe.

    The schema's unique rules identify the deterministic records. Existing rows
    are left untouched instead of clearing or rebuilding the user's database.
    """
    connection.executemany(statement.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), records)


def list_portfolios(database_path: Path = DATABASE_PATH) -> list[dict]:
    """Return portfolio IDs and display names for a selector without writes."""
    with get_connection(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT portfolio_id, external_id, portfolio_name FROM portfolios ORDER BY portfolio_id"
        )]


def list_proposals(portfolio_id: int, database_path: Path = DATABASE_PATH) -> list[dict]:
    """Return saved proposal headers for one portfolio, newest first."""
    with get_connection(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT proposal_id, proposal_date, status, total_portfolio_value, created_at FROM rebalance_proposals WHERE portfolio_id = ? ORDER BY proposal_id DESC",
            (portfolio_id,),
        )]
