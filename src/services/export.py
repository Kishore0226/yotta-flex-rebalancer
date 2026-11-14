"""Timestamped CSV and JSON exports for database and operational results."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config import DATABASE_PATH, EXPORT_DIRECTORY, RANDOM_SEED
from src.database.connection import get_connection
from src.services.audit import append_audit_event

DATABASE_TABLES = (
    "investor_profiles", "portfolios", "instruments", "holdings", "market_prices",
    "target_allocations", "fee_schedules", "constraints", "rebalance_proposals",
    "trades", "audit_events", "anomalies",
)


def _value(value: Any) -> Any:
    """Turn Decimal/date/dataclass data into stable JSON and CSV values."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return _value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _new_export_directory(export_root: Path, label: str) -> Path:
    """Create a unique timestamped directory rather than overwriting an export."""
    export_root.mkdir(parents=True, exist_ok=True)
    base = f"{label}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    candidate = export_root / base
    suffix = 1
    while candidate.exists():
        candidate = export_root / f"{base}_{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _write_records(destination: Path, stem: str, records: Iterable[dict[str, Any]], columns: Optional[list[str]] = None) -> list[Path]:
    """Write one consistent CSV/JSON pair, including headers for empty sets."""
    normalised = [_value(record) for record in records]
    resolved_columns = columns or (list(normalised[0]) if normalised else [])
    csv_path = destination / f"{stem}.csv"
    json_path = destination / f"{stem}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=resolved_columns)
        if resolved_columns:
            writer.writeheader()
            writer.writerows(normalised)
    with json_path.open("w", encoding="utf-8") as json_file:
        json.dump(normalised, json_file, indent=2, sort_keys=True, ensure_ascii=False)
        json_file.write("\n")
    return [csv_path, json_path]


def export_database_snapshot(
    *,
    mode: Optional[str] = None,
    source: str = "sqlite",
    database_path: Path = DATABASE_PATH,
    export_root: Path = EXPORT_DIRECTORY,
) -> Path:
    """Export every operational table into a unique directory plus manifest.

    ``anomalies`` is exported as a separate exception-data file. No
    ground-truth table is exported or treated as operational engine input.
    """
    destination = _new_export_directory(export_root, "database_snapshot")
    append_audit_event(
        "DATA_EXPORT", {"export_kind": "database_snapshot", "destination": str(destination), "mode": mode, "source": source},
        entity_type="export", entity_id=destination.name, database_path=database_path,
    )
    files: list[str] = []
    counts: dict[str, int] = {}
    with get_connection(database_path) as connection:
        for table in DATABASE_TABLES:
            columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            written = _write_records(destination, table, rows, columns)
            files.extend(path.name for path in written)
            counts[table] = len(rows)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "seed": RANDOM_SEED,
        "mode": mode, "source": source, "database_path": str(database_path),
        "tables": counts, "files": files,
        "exception_data": ["anomalies.csv", "anomalies.json"],
        "ground_truth_included": False,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with get_connection(database_path) as connection:
        with (destination / "database.sql").open("w", encoding="utf-8") as sql_file:
            for statement in connection.iterdump():
                sql_file.write(statement + "\n")
    return destination


def export_operational_results(
    *,
    valuation: Any = None,
    recommendations: Any = None,
    simulation: Any = None,
    proposal_id: Optional[int] = None,
    export_root: Path = EXPORT_DIRECTORY,
    database_path: Path = DATABASE_PATH,
) -> Path:
    """Export valuation/allocation, recommendation, and simulation result objects.

    Calling code supplies already-calculated objects, so this function neither
    generates recommendations nor changes the source portfolio.
    """
    destination = _new_export_directory(export_root, "operational_results")
    files: list[str] = []
    if valuation is not None:
        records = [asdict(item) for item in valuation.holdings]
        files.extend(path.name for path in _write_records(destination, "portfolio_valuation", records))
    if recommendations is not None:
        records = [asdict(item) for item in recommendations.recommendations]
        files.extend(path.name for path in _write_records(destination, "rebalancing_recommendations", records))
    elif proposal_id is not None:
        with get_connection(database_path) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM trades WHERE proposal_id = ? ORDER BY trade_id", (proposal_id,)
            )]
            columns = [row["name"] for row in connection.execute("PRAGMA table_info('trades')")]
        files.extend(path.name for path in _write_records(destination, "rebalancing_recommendations", rows, columns))
    if simulation is not None:
        records = [asdict(item) for item in simulation.holdings]
        files.extend(path.name for path in _write_records(destination, "post_trade_simulation", records))
        trade_records = [asdict(item) for item in (*simulation.accepted_sells, *simulation.accepted_buys, *simulation.blocked_trades)]
        files.extend(path.name for path in _write_records(destination, "simulated_trades", trade_records))
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "seed": RANDOM_SEED, "source": "in_memory_operational_results", "files": files}
    (destination / "manifest.json").write_text(json.dumps(_value(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_audit_event("DATA_EXPORT", {"export_kind": "operational_results", "destination": str(destination), "files": files}, entity_type="export", entity_id=destination.name, portfolio_id=getattr(simulation, "portfolio_id", None), proposal_id=getattr(simulation, "proposal_id", None), database_path=database_path)
    return destination
