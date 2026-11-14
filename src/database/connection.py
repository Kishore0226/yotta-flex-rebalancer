"""Safe SQLite connection helpers.

SQLite does not enforce foreign keys unless each connection explicitly enables
them. `get_connection` performs that setup every time.
"""

import sqlite3
from pathlib import Path

from src.config import DATABASE_PATH, DATA_DIRECTORY, EXPORT_DIRECTORY


def get_connection(database_path: Path = DATABASE_PATH) -> sqlite3.Connection:
    """Return a configured SQLite connection to the application database."""
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    EXPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection
