"""Database definition and initialization for the YottaFlex project."""

import sqlite3
from pathlib import Path

from src.config import DATABASE_PATH
from src.database.connection import get_connection


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS investor_profiles (
    investor_id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    risk_profile TEXT NOT NULL CHECK (risk_profile IN ('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE')),
    investment_horizon_years INTEGER NOT NULL CHECK (investment_horizon_years > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    investor_id INTEGER NOT NULL,
    portfolio_name TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'USD' CHECK (length(base_currency) = 3),
    available_cash REAL NOT NULL DEFAULT 0 CHECK (available_cash >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (investor_id, portfolio_name),
    FOREIGN KEY (investor_id) REFERENCES investor_profiles(investor_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    ticker TEXT NOT NULL UNIQUE,
    instrument_name TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'BOND', 'GOLD', 'CASH', 'ETF')),
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS market_prices (
    market_price_id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    price_date TEXT NOT NULL,
    closing_price REAL NOT NULL CHECK (closing_price > 0),
    average_daily_volume REAL CHECK (average_daily_volume IS NULL OR average_daily_volume >= 0),
    UNIQUE (instrument_id, price_date),
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS holdings (
    holding_id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    portfolio_id INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    quantity REAL NOT NULL CHECK (quantity >= 0),
    average_cost REAL CHECK (average_cost IS NULL OR average_cost >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (portfolio_id, instrument_id),
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE CASCADE,
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS target_allocations (
    target_allocation_id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    target_date TEXT NOT NULL,
    target_weight REAL NOT NULL CHECK (target_weight >= 0 AND target_weight <= 1),
    UNIQUE (portfolio_id, instrument_id, target_date),
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE CASCADE,
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fee_schedules (
    fee_schedule_id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    schedule_name TEXT NOT NULL,
    fixed_fee REAL NOT NULL DEFAULT 0 CHECK (fixed_fee >= 0),
    percentage_fee REAL NOT NULL DEFAULT 0 CHECK (percentage_fee >= 0 AND percentage_fee <= 1),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    UNIQUE (portfolio_id, schedule_name, effective_from),
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rebalance_proposals (
    proposal_id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    portfolio_id INTEGER NOT NULL,
    proposal_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected', 'executed')),
    total_portfolio_value REAL NOT NULL CHECK (total_portfolio_value >= 0),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id INTEGER PRIMARY KEY,
    proposal_id INTEGER,
    portfolio_id INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    trade_action TEXT NOT NULL CHECK (trade_action IN ('BUY', 'SELL', 'HOLD')),
    recommended_quantity REAL NOT NULL DEFAULT 0 CHECK (recommended_quantity >= 0),
    estimated_price REAL CHECK (estimated_price IS NULL OR estimated_price > 0),
    estimated_fee REAL NOT NULL DEFAULT 0 CHECK (estimated_fee >= 0),
    estimated_trade_value REAL NOT NULL DEFAULT 0 CHECK (estimated_trade_value >= 0),
    trade_status TEXT NOT NULL DEFAULT 'proposed' CHECK (trade_status IN ('proposed', 'approved', 'rejected', 'executed', 'blocked')),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (proposal_id) REFERENCES rebalance_proposals(proposal_id) ON DELETE SET NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE RESTRICT,
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS "constraints" (
    constraint_id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    instrument_id INTEGER,
    constraint_type TEXT NOT NULL CHECK (constraint_type IN ('minimum_trade_value', 'excluded_instrument', 'liquidity_limit', 'maximum_trade_value')),
    numeric_value REAL CHECK (numeric_value IS NULL OR numeric_value >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE CASCADE,
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE RESTRICT,
    CHECK (
        (constraint_type = 'excluded_instrument' AND instrument_id IS NOT NULL)
        OR (constraint_type <> 'excluded_instrument')
    )
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id INTEGER PRIMARY KEY,
    portfolio_id INTEGER,
    proposal_id INTEGER,
    trade_id INTEGER,
    event_type TEXT NOT NULL,
    event_details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE SET NULL,
    FOREIGN KEY (proposal_id) REFERENCES rebalance_proposals(proposal_id) ON DELETE SET NULL,
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id INTEGER PRIMARY KEY,
    portfolio_id INTEGER,
    instrument_id INTEGER,
    proposal_id INTEGER,
    anomaly_type TEXT NOT NULL CHECK (anomaly_type IN ('missing_price', 'excluded_instrument', 'minimum_trade_size', 'liquidity_limit', 'missing_target', 'zero_value_portfolio', 'insufficient_cash')),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    message TEXT NOT NULL,
    is_resolved INTEGER NOT NULL DEFAULT 0 CHECK (is_resolved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id) ON DELETE SET NULL,
    FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id) ON DELETE SET NULL,
    FOREIGN KEY (proposal_id) REFERENCES rebalance_proposals(proposal_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_holdings_portfolio ON holdings(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_prices_instrument_date ON market_prices(instrument_id, price_date);
CREATE INDEX IF NOT EXISTS idx_targets_portfolio_date ON target_allocations(portfolio_id, target_date);
CREATE INDEX IF NOT EXISTS idx_trades_proposal ON trades(proposal_id);
CREATE INDEX IF NOT EXISTS idx_audit_portfolio ON audit_events(portfolio_id);
"""


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    """Return a table's column names for safe, one-time migrations."""
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table_name}")')}


def _migrate_phase_two_schema(connection: sqlite3.Connection) -> None:
    """Upgrade an empty/Phase-1 database without recreating the database file.

    The Phase-2 categories require wider CHECK constraints for investors and
    instruments. SQLite cannot alter a CHECK constraint directly, so temporary
    replacement tables are used while preserving existing rows and integer IDs.
    """
    if "external_id" not in _column_names(connection, "investor_profiles"):
        connection.execute("PRAGMA foreign_keys = OFF;")
        connection.executescript("""
            CREATE TABLE investor_profiles_new (
                investor_id INTEGER PRIMARY KEY,
                external_id TEXT UNIQUE,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                risk_profile TEXT NOT NULL CHECK (risk_profile IN ('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE')),
                investment_horizon_years INTEGER NOT NULL CHECK (investment_horizon_years > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO investor_profiles_new (investor_id, full_name, email, risk_profile, investment_horizon_years, created_at)
            SELECT investor_id, full_name, email,
                   CASE UPPER(risk_profile)
                       WHEN 'GROWTH' THEN 'AGGRESSIVE'
                       ELSE UPPER(risk_profile)
                   END,
                   investment_horizon_years, created_at
            FROM investor_profiles;
            DROP TABLE investor_profiles;
            ALTER TABLE investor_profiles_new RENAME TO investor_profiles;

            CREATE TABLE instruments_new (
                instrument_id INTEGER PRIMARY KEY,
                external_id TEXT UNIQUE,
                ticker TEXT NOT NULL UNIQUE,
                instrument_name TEXT NOT NULL,
                asset_class TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'BOND', 'GOLD', 'CASH', 'ETF')),
                currency TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );
            INSERT INTO instruments_new (instrument_id, ticker, instrument_name, asset_class, currency, is_active)
            SELECT instrument_id, ticker, instrument_name,
                   CASE LOWER(asset_class)
                       WHEN 'equity' THEN 'EQUITY'
                       WHEN 'bond' THEN 'BOND'
                       WHEN 'cash' THEN 'CASH'
                       WHEN 'commodity' THEN 'GOLD'
                       ELSE 'ETF'
                   END,
                   currency, is_active
            FROM instruments;
            DROP TABLE instruments;
            ALTER TABLE instruments_new RENAME TO instruments;
        """)
        connection.execute("PRAGMA foreign_keys = ON;")

    for table_name in ("portfolios", "holdings"):
        if "external_id" not in _column_names(connection, table_name):
            connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN external_id TEXT;')
            connection.execute(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{table_name}_external_id" '
                f'ON "{table_name}" (external_id);'
            )

    if "external_id" not in _column_names(connection, "rebalance_proposals"):
        connection.execute('ALTER TABLE rebalance_proposals ADD COLUMN external_id TEXT;')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_rebalance_proposals_external_id ON rebalance_proposals (external_id);')
        connection.execute("UPDATE rebalance_proposals SET external_id = printf('RB-2026-%06d', proposal_id) WHERE external_id IS NULL;")


def initialize_database(database_path: Path = DATABASE_PATH) -> None:
    """Create every project table and index if it does not already exist."""
    with get_connection(database_path) as connection:
        connection.executescript(SCHEMA_SQL)
        _migrate_phase_two_schema(connection)


def list_tables(database_path: Path = DATABASE_PATH) -> list[str]:
    """Return application table names, ordered alphabetically."""
    with get_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]


if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully.")
