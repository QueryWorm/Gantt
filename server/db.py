"""
SQLite-схема для fleet-repair-board.

Таблицы:
  borts         — борта (id, desc, priority, assignee, case_start)
  tracks        — треки внутри борта (id, bort_id, name, is_sub, ord)
  segments      — сегменты (id, track_id, kind, label, start, days, status, ord)
  log_entries   — лог ремонта (id, bort_id, date, stage, text, ts)
  events        — события юзабилити (id, ts, session_id, type, target, payload)
  queue         — очередь (id, reason, ord)
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "fleet.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS borts (
    id          TEXT PRIMARY KEY,
    desc        TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 0,
    assignee    TEXT NOT NULL DEFAULT '',
    case_start  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tracks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bort_id TEXT NOT NULL REFERENCES borts(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    is_sub  INTEGER NOT NULL DEFAULT 0,
    ord     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_bort ON tracks(bort_id, ord);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    start       INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    status      TEXT NOT NULL,
    ord         INTEGER NOT NULL DEFAULT 0,
    depends_on  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_segments_track ON segments(track_id, ord);

CREATE TABLE IF NOT EXISTS log_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bort_id TEXT NOT NULL REFERENCES borts(id) ON DELETE CASCADE,
    date    TEXT NOT NULL,
    stage   TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_log_bort ON log_entries(bort_id, ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    session_id  TEXT NOT NULL,
    type        TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);

CREATE TABLE IF NOT EXISTS queue (
    id     TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    ord    INTEGER NOT NULL DEFAULT 0
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # миграция: добавить depends_on если старая БД
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)").fetchall()}
        if "depends_on" not in cols:
            conn.execute("ALTER TABLE segments ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
