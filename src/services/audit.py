"""Append-only audit events backed by the existing ``audit_events`` table."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from src.config import DATABASE_PATH
from src.database.connection import get_connection


def _json_default(value: object) -> str:
    """Serialize financial and date values consistently in event details."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__} into an audit event.")


@dataclass(frozen=True)
class AuditEvent:
    """A read model for an immutable event stored in SQLite."""

    audit_event_id: int
    event_type: str
    event_details: dict[str, Any]
    created_at: str
    portfolio_id: Optional[int] = None
    proposal_id: Optional[int] = None
    trade_id: Optional[int] = None


def append_audit_event(
    event_type: str,
    details: dict[str, Any],
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[int | str] = None,
    portfolio_id: Optional[int] = None,
    proposal_id: Optional[int] = None,
    trade_id: Optional[int] = None,
    database_path: Path = DATABASE_PATH,
) -> int:
    """Append one event and return its ID. Existing events are never updated."""
    payload = dict(details)
    payload["entity_type"] = entity_type
    payload["entity_id"] = entity_id
    payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":"))
    with get_connection(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO audit_events (portfolio_id, proposal_id, trade_id, event_type, event_details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (portfolio_id, proposal_id, trade_id, event_type, serialized),
        )
    return int(cursor.lastrowid)


def list_audit_events(database_path: Path = DATABASE_PATH) -> list[AuditEvent]:
    """Return audit history in insertion order without modifying it."""
    with get_connection(database_path) as connection:
        rows = connection.execute("SELECT * FROM audit_events ORDER BY audit_event_id").fetchall()
    return [
        AuditEvent(
            audit_event_id=row["audit_event_id"], event_type=row["event_type"],
            event_details=json.loads(row["event_details"]), created_at=row["created_at"],
            portfolio_id=row["portfolio_id"], proposal_id=row["proposal_id"], trade_id=row["trade_id"],
        )
        for row in rows
    ]


def record_portfolio_created(portfolio_id: int, *, source: str, database_path: Path = DATABASE_PATH) -> int:
    """Record portfolio creation for callers that create portfolios in future."""
    return append_audit_event(
        "PORTFOLIO_CREATED", {"source": source}, entity_type="portfolio", entity_id=portfolio_id,
        portfolio_id=portfolio_id, database_path=database_path,
    )
