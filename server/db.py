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
    dept        TEXT NOT NULL DEFAULT '',
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
    depends_on  TEXT NOT NULL DEFAULT '[]',
    dept        TEXT NOT NULL DEFAULT '',
    assignee    TEXT NOT NULL DEFAULT '',
    zero_day    INTEGER NOT NULL DEFAULT 0,
    starts_with TEXT NOT NULL DEFAULT '[]',
    tpl_start   INTEGER,
    tpl_days    INTEGER
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

CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_templates_created ON templates(created_at);

CREATE TABLE IF NOT EXISTS template_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    is_sub      INTEGER NOT NULL DEFAULT 0,
    ord         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tpl_tracks_tpl ON template_tracks(template_id, ord);

CREATE TABLE IF NOT EXISTS template_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL REFERENCES template_tracks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    days        INTEGER NOT NULL DEFAULT 0,
    dept        TEXT NOT NULL DEFAULT '',
    assignee    TEXT NOT NULL DEFAULT '',
    depends_on  TEXT NOT NULL DEFAULT '[]',
    start       INTEGER NOT NULL DEFAULT -1,
    ord         INTEGER NOT NULL DEFAULT 0,
    zero_day    INTEGER NOT NULL DEFAULT 0,
    starts_with TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_tpl_segs_track ON template_segments(track_id, ord);
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
        # миграция: добавить dept в borts если старая БД
        bcols = {r["name"] for r in conn.execute("PRAGMA table_info(borts)").fetchall()}
        if "dept" not in bcols:
            conn.execute("ALTER TABLE borts ADD COLUMN dept TEXT NOT NULL DEFAULT ''")
        # миграция: добавить dept/assignee в segments если старая БД
        scols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)").fetchall()}
        if "dept" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN dept TEXT NOT NULL DEFAULT ''")
        if "assignee" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN assignee TEXT NOT NULL DEFAULT ''")
        # миграция: нулевой день (старт зависимой в день окончания предшественника)
        if "zero_day" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN zero_day INTEGER NOT NULL DEFAULT 0")
        # миграция: параллельный старт (старт в день начала предшественника, SS)
        if "starts_with" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN starts_with TEXT NOT NULL DEFAULT '[]'")
        # миграция: план из шаблона (tpl_start/tpl_days) в segments если старая БД
        if "tpl_start" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN tpl_start INTEGER")
        if "tpl_days" not in scols:
            conn.execute("ALTER TABLE segments ADD COLUMN tpl_days INTEGER")
        # миграция: шаблоны переехали с JSON на нормальные таблицы
        # Если в templates ещё есть колонка definition — сбрасываем (старые шаблоны теряются)
        # В активной разработке это безопасно; в продакшене нужна полноценная миграция
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(templates)").fetchall()}
        if "definition" in tcols:
            conn.execute("DELETE FROM templates")
            tlist = {r["name"] for r in conn.execute("PRAGMA table_info(template_tracks)").fetchall()}
            if "is_sub" not in tlist:
                conn.execute("DROP TABLE IF EXISTS template_segments")
                conn.execute("DROP TABLE IF EXISTS template_tracks")
                conn.execute("ALTER TABLE templates RENAME TO _tpl_old")
                conn.executescript("""
                    CREATE TABLE templates (
                        id          TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    CREATE TABLE template_tracks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        template_id TEXT NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        is_sub INTEGER NOT NULL DEFAULT 0,
                        ord INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE template_segments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        track_id INTEGER NOT NULL REFERENCES template_tracks(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        label TEXT NOT NULL,
                        days INTEGER NOT NULL DEFAULT 0,
                        dept TEXT NOT NULL DEFAULT '',
                        assignee TEXT NOT NULL DEFAULT '',
                        depends_on TEXT NOT NULL DEFAULT '[]',
                        start INTEGER NOT NULL DEFAULT -1,
                        ord INTEGER NOT NULL DEFAULT 0
                    );
                """)
                conn.execute("DROP TABLE _tpl_old")
        # миграция: depends_on для template_segments если старая БД
        tscols = {r["name"] for r in conn.execute("PRAGMA table_info(template_segments)").fetchall()}
        if "depends_on" not in tscols:
            conn.execute("ALTER TABLE template_segments ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
        if "start" not in tscols:
            conn.execute("ALTER TABLE template_segments ADD COLUMN start INTEGER NOT NULL DEFAULT -1")
        # миграция: нулевой день / параллельный старт для template_segments
        if "zero_day" not in tscols:
            conn.execute("ALTER TABLE template_segments ADD COLUMN zero_day INTEGER NOT NULL DEFAULT 0")
        if "starts_with" not in tscols:
            conn.execute("ALTER TABLE template_segments ADD COLUMN starts_with TEXT NOT NULL DEFAULT '[]'")
        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
