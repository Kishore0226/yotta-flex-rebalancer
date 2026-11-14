"""Reproducible synthetic data generation for the YottaFlex demonstration.

Run from the project directory, for example:
    python -m src.services.synthetic_data --mode starter
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.config import DATABASE_PATH, EXPORT_DIRECTORY, RANDOM_SEED, SYNTHETIC_AS_OF_DATE
from src.database.connection import get_connection
from src.database.repositories import insert_many_ignore
from src.database.schema import initialize_database
from src.services.audit import append_audit_event


@dataclass(frozen=True)
class GenerationSettings:
    """The row sizes for one supported data-generation mode."""

    investors: int
    instruments: int
    trading_days: int


MODES = {
    "starter": GenerationSettings(investors=100, instruments=500, trading_days=60),
    "full": GenerationSettings(investors=1000, instruments=5000, trading_days=250),
}

RISK_PROFILES = ("CONSERVATIVE", "MODERATE", "AGGRESSIVE")
ASSET_CLASSES = ("EQUITY", "BOND", "GOLD", "CASH", "ETF")
RISK_TARGETS = {
    "CONSERVATIVE": {"EQUITY": 0.20, "BOND": 0.40, "GOLD": 0.10, "CASH": 0.10, "ETF": 0.20},
    "MODERATE": {"EQUITY": 0.35, "BOND": 0.30, "GOLD": 0.10, "CASH": 0.05, "ETF": 0.20},
    "AGGRESSIVE": {"EQUITY": 0.50, "BOND": 0.10, "GOLD": 0.10, "CASH": 0.05, "ETF": 0.25},
}
FEE_TIERS = (("Basic", 4.95, 0.0025), ("Standard", 2.95, 0.0015), ("Premium", 0.00, 0.0008))


def _inject_labelled_edge_cases(connection: sqlite3.Connection) -> list[dict]:
    """Inject deterministic, labelled full-mode exceptions after valid baseline data.

    Labels are written separately and are never read by valuation/rebalancing.
    """
    cases = [
        {"case_id": "GT-001", "portfolio_id": 996, "issue_type": "high_drift", "expected_decision": "rebalance_required"},
        {"case_id": "GT-002", "portfolio_id": 997, "issue_type": "invalid_target_total", "expected_decision": "block_invalid_target_allocation"},
        {"case_id": "GT-003", "portfolio_id": 998, "issue_type": "fee_omitted", "expected_decision": "block_invalid_fee_schedule"},
        {"case_id": "GT-004", "portfolio_id": 999, "issue_type": "liquidity_constraint", "expected_decision": "block_liquidity_limit"},
        {"case_id": "GT-005", "portfolio_id": 1000, "issue_type": "excluded_instrument", "expected_decision": "block_excluded_instrument"},
    ]
    connection.execute("UPDATE holdings SET quantity = quantity * 10 WHERE portfolio_id = 996 AND instrument_id = (SELECT MIN(instrument_id) FROM holdings WHERE portfolio_id = 996)")
    connection.execute("UPDATE target_allocations SET target_weight = target_weight - 0.01 WHERE portfolio_id = 997 AND instrument_id = (SELECT MIN(instrument_id) FROM target_allocations WHERE portfolio_id = 997)")
    connection.execute("DELETE FROM fee_schedules WHERE portfolio_id = 998")
    instrument_999 = connection.execute("SELECT MIN(instrument_id) FROM holdings WHERE portfolio_id = 999").fetchone()[0]
    connection.execute("INSERT INTO \"constraints\" (portfolio_id,instrument_id,constraint_type,numeric_value,description) VALUES (999, ?, 'liquidity_limit', 10, 'Labelled liquidity edge case.')", (instrument_999,))
    instrument_1000 = connection.execute("SELECT MIN(instrument_id) FROM holdings WHERE portfolio_id = 1000").fetchone()[0]
    connection.execute("INSERT INTO \"constraints\" (portfolio_id,instrument_id,constraint_type,description) VALUES (1000, ?, 'excluded_instrument', 'Labelled excluded-instrument edge case.')", (instrument_1000,))
    for case in cases:
        anomaly_type = {"liquidity_constraint": "liquidity_limit", "excluded_instrument": "excluded_instrument", "invalid_target_total": "missing_target", "fee_omitted": "insufficient_cash", "high_drift": "zero_value_portfolio"}[case["issue_type"]]
        connection.execute("INSERT INTO anomalies (portfolio_id, anomaly_type, severity, message) VALUES (?, ?, 'warning', ?)", (case["portfolio_id"], anomaly_type, case["issue_type"]))
    return cases


def _write_ground_truth(cases: list[dict], export_directory: Path, mode: str) -> None:
    export_directory.mkdir(parents=True, exist_ok=True)
    fields = ["case_id", "portfolio_id", "issue_type", "expected_decision"]
    with (export_directory / f"{mode}_ground_truth.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(cases)
    (export_directory / f"{mode}_ground_truth.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")


def business_days(end_date: date, number_of_days: int) -> list[date]:
    """Return exactly `number_of_days` weekdays ending at `end_date`."""
    dates: list[date] = []
    candidate = end_date
    while len(dates) < number_of_days:
        if candidate.weekday() < 5:
            dates.append(candidate)
        candidate = date.fromordinal(candidate.toordinal() - 1)
    return list(reversed(dates))


def _instrument_id_for(asset_index: int, asset_class_index: int, instruments: int) -> int:
    """Choose one deterministic instrument of a requested class."""
    per_class = instruments // len(ASSET_CLASSES)
    return asset_class_index * per_class + (asset_index % per_class) + 1


def _make_investors(count: int) -> list[tuple]:
    return [
        (f"INV-{number:06d}", f"Investor {number:06d}", f"inv-{number:06d}@example.test", RISK_PROFILES[(number - 1) % 3], 3 + ((number * 7) % 23))
        for number in range(1, count + 1)
    ]


def _make_portfolios(count: int, rng: random.Random) -> list[tuple]:
    records = []
    for number in range(1, count + 1):
        external_id = f"PORT-INV-{number:06d}-01"
        records.append((external_id, number, external_id, "USD", round(rng.uniform(1_000, 25_000), 2)))
    return records


def _make_instruments(count: int) -> list[tuple]:
    records = []
    per_class = count // len(ASSET_CLASSES)
    for number in range(1, count + 1):
        asset_class = ASSET_CLASSES[(number - 1) // per_class]
        external_id = f"INS-{number:06d}"
        records.append((external_id, external_id, f"Synthetic {asset_class.title()} Instrument {number:06d}", asset_class, "USD", 1))
    return records


def _price_history(instrument_id: int, asset_class: str, dates: list[date], rng: random.Random) -> Iterable[tuple]:
    """Yield a plausible deterministic random-walk price series."""
    bases = {"EQUITY": 80.0, "BOND": 105.0, "GOLD": 190.0, "CASH": 1.0, "ETF": 125.0}
    volatility = {"EQUITY": 0.018, "BOND": 0.004, "GOLD": 0.012, "CASH": 0.0001, "ETF": 0.012}
    price = bases[asset_class] * (0.75 + rng.random() * 0.50)
    for price_date in dates:
        price = max(0.01, price * (1 + rng.gauss(0.0002, volatility[asset_class])))
        volume = int(rng.uniform(50_000, 2_000_000))
        yield (instrument_id, price_date.isoformat(), round(price, 4), volume)


def _make_holdings_and_targets(count: int, instruments: int, rng: random.Random) -> tuple[list[tuple], list[tuple]]:
    holdings: list[tuple] = []
    targets: list[tuple] = []
    target_date = SYNTHETIC_AS_OF_DATE.isoformat()
    for portfolio_id in range(1, count + 1):
        risk_profile = RISK_PROFILES[(portfolio_id - 1) % 3]
        for class_index, asset_class in enumerate(ASSET_CLASSES):
            instrument_id = _instrument_id_for(portfolio_id - 1, class_index, instruments)
            external_id = f"HOLD-{portfolio_id}-{instrument_id}"
            quantity = round(rng.uniform(10, 500), 4)
            average_cost = round(rng.uniform(15, 250), 4)
            holdings.append((external_id, portfolio_id, instrument_id, quantity, average_cost))
            targets.append((portfolio_id, instrument_id, target_date, RISK_TARGETS[risk_profile][asset_class]))
    return holdings, targets


def _make_fees_and_constraints(count: int, instruments: int) -> tuple[list[tuple], list[tuple]]:
    fees: list[tuple] = []
    constraints: list[tuple] = []
    for portfolio_id in range(1, count + 1):
        tier_name, fixed_fee, percentage_fee = FEE_TIERS[(portfolio_id - 1) % len(FEE_TIERS)]
        fees.append((portfolio_id, f"{tier_name} tier", fixed_fee, percentage_fee, SYNTHETIC_AS_OF_DATE.isoformat(), None))
        minimum_value = (250.0, 500.0, 1_000.0)[(portfolio_id - 1) % 3]
        constraints.append((portfolio_id, None, "minimum_trade_value", minimum_value, 1, f"Minimum trade value is USD {minimum_value:,.0f}."))
        if portfolio_id % 5 == 0:
            excluded = _instrument_id_for(portfolio_id - 1, 0, instruments)
            constraints.append((portfolio_id, excluded, "excluded_instrument", None, 1, "This instrument is excluded by portfolio policy."))
    return fees, constraints


def _export_table(connection: sqlite3.Connection, table_name: str, export_directory: Path, mode: str) -> None:
    """Export one table to CSV and JSON without loading the whole table at once."""
    export_directory.mkdir(parents=True, exist_ok=True)
    cursor = connection.execute(f'SELECT * FROM "{table_name}" ORDER BY rowid')
    columns = [description[0] for description in cursor.description]
    base_name = export_directory / f"{mode}_{table_name}"
    with (base_name.with_suffix(".csv")).open("w", newline="", encoding="utf-8") as csv_file, \
         (base_name.with_suffix(".json")).open("w", encoding="utf-8") as json_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        json_file.write("[")
        first = True
        while rows := cursor.fetchmany(10_000):
            for row in rows:
                record = dict(row)
                writer.writerow(record)
                if not first:
                    json_file.write(",")
                json.dump(record, json_file, separators=(",", ":"))
                first = False
        json_file.write("]\n")


def export_generated_records(connection: sqlite3.Connection, mode: str, export_directory: Path = EXPORT_DIRECTORY) -> None:
    """Export every database table to one CSV and one JSON file."""
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    for row in tables:
        _export_table(connection, row["name"], export_directory, mode)


def generate_synthetic_data(
    mode: str,
    database_path: Path = DATABASE_PATH,
    export_directory: Path = EXPORT_DIRECTORY,
    export_records: bool = True,
) -> dict[str, int]:
    """Generate one deterministic starter/full dataset and return table counts.

    Existing rows are preserved. Repeating the same mode is safe because each
    generated business record uses deterministic values and database uniqueness
    rules prevent duplicates.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode '{mode}'. Choose one of: {', '.join(MODES)}.")
    settings = MODES[mode]
    if settings.instruments % len(ASSET_CLASSES) != 0:
        raise ValueError("Instrument count must divide evenly across asset classes.")

    initialize_database(database_path)
    rng = random.Random(RANDOM_SEED)
    dates = business_days(SYNTHETIC_AS_OF_DATE, settings.trading_days)
    investors = _make_investors(settings.investors)
    portfolios = _make_portfolios(settings.investors, rng)
    instruments = _make_instruments(settings.instruments)
    holdings, targets = _make_holdings_and_targets(settings.investors, settings.instruments, rng)
    fees, constraints = _make_fees_and_constraints(settings.investors, settings.instruments)

    with get_connection(database_path) as connection:
        # Parent tables first, then the children that point to them.
        insert_many_ignore(connection, "INSERT INTO investor_profiles (external_id, full_name, email, risk_profile, investment_horizon_years) VALUES (?, ?, ?, ?, ?)", investors)
        insert_many_ignore(connection, "INSERT INTO portfolios (external_id, investor_id, portfolio_name, base_currency, available_cash) VALUES (?, ?, ?, ?, ?)", portfolios)
        insert_many_ignore(connection, "INSERT INTO instruments (external_id, ticker, instrument_name, asset_class, currency, is_active) VALUES (?, ?, ?, ?, ?, ?)", instruments)
        insert_many_ignore(connection, "INSERT INTO holdings (external_id, portfolio_id, instrument_id, quantity, average_cost) VALUES (?, ?, ?, ?, ?)", holdings)

        for instrument_id, instrument in enumerate(instruments, start=1):
            asset_class = instrument[3]
            insert_many_ignore(
                connection,
                "INSERT INTO market_prices (instrument_id, price_date, closing_price, average_daily_volume) VALUES (?, ?, ?, ?)",
                _price_history(instrument_id, asset_class, dates, rng),
            )

        insert_many_ignore(connection, "INSERT INTO target_allocations (portfolio_id, instrument_id, target_date, target_weight) VALUES (?, ?, ?, ?)", targets)
        insert_many_ignore(connection, "INSERT INTO fee_schedules (portfolio_id, schedule_name, fixed_fee, percentage_fee, effective_from, effective_to) VALUES (?, ?, ?, ?, ?, ?)", fees)
        insert_many_ignore(connection, "INSERT INTO \"constraints\" (portfolio_id, instrument_id, constraint_type, numeric_value, is_active, description) VALUES (?, ?, ?, ?, ?, ?)", constraints)
        edge_cases = _inject_labelled_edge_cases(connection) if mode == "full" else []
        counts = {
            row["name"]: connection.execute(f'SELECT COUNT(*) FROM "{row["name"]}"').fetchone()[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        }
        if export_records:
            export_generated_records(connection, mode, export_directory)
        if edge_cases:
            _write_ground_truth(edge_cases, export_directory, mode)
    append_audit_event(
        "SYNTHETIC_DATA_GENERATED",
        {"mode": mode, "seed": RANDOM_SEED, "investors_requested": settings.investors,
         "portfolios_requested": settings.investors, "instruments_requested": settings.instruments,
         "trading_days": settings.trading_days, "table_counts": counts},
        entity_type="synthetic_data_batch", entity_id=mode, database_path=database_path,
    )
    return counts


def main() -> None:
    """Parse CLI arguments and print a compact result."""
    parser = argparse.ArgumentParser(description="Generate reproducible YottaFlex synthetic data.")
    parser.add_argument("--mode", choices=MODES, required=True, help="Dataset size to generate.")
    parser.add_argument("--database-path", type=Path, default=DATABASE_PATH, help="SQLite file to populate; use a separate path for full data.")
    parser.add_argument("--export-directory", type=Path, default=EXPORT_DIRECTORY, help="Directory for legacy per-table CSV/JSON exports.")
    parser.add_argument("--no-export", action="store_true", help="Skip legacy exports; useful for reproducibility checks.")
    args = parser.parse_args()
    counts = generate_synthetic_data(args.mode, args.database_path, args.export_directory, not args.no_export)
    print(f"Generated or verified {args.mode} data using seed {RANDOM_SEED}.")
    for table_name in sorted(counts):
        print(f"{table_name}: {counts[table_name]}")


if __name__ == "__main__":
    main()
