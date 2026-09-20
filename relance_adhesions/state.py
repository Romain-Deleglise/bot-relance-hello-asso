"""Anti-doublon : mémorisation des relances déjà envoyées (SQLite, stdlib)."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS relances (
    dedup_key   TEXT PRIMARY KEY,
    item_id     INTEGER NOT NULL,
    email       TEXT NOT NULL,
    end_date    TEXT,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relances_email ON relances(email);
CREATE TABLE IF NOT EXISTS executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    analysed    INTEGER DEFAULT 0,
    selected    INTEGER DEFAULT 0,
    sent        INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    dry_run     INTEGER DEFAULT 0
);
"""


class ReminderStore:
    """Historique persistant des relances, pour ne jamais relancer deux fois."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        with closing(self._conn.cursor()) as cursor:
            cursor.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReminderStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Relances -----------------------------------------------------------

    def already_sent(self, dedup_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM relances WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return row is not None

    def mark_sent(
        self, dedup_key: str, item_id: int, email: str, end_date: date | None
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO relances "
            "(dedup_key, item_id, email, end_date, sent_at) VALUES (?, ?, ?, ?, ?)",
            (
                dedup_key,
                item_id,
                email,
                end_date.isoformat() if end_date else None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    # -- Journal d'exécution -------------------------------------------------

    def start_run(self, dry_run: bool) -> int:
        cursor = self._conn.execute(
            "INSERT INTO executions (started_at, dry_run) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), int(dry_run)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, analysed: int, selected: int, sent: int, errors: int
    ) -> None:
        self._conn.execute(
            "UPDATE executions SET finished_at = ?, analysed = ?, selected = ?, "
            "sent = ?, errors = ? WHERE id = ?",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                analysed,
                selected,
                sent,
                errors,
                run_id,
            ),
        )
        self._conn.commit()
