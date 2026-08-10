"""
Сид: читает server/seed_data.json и заливает в SQLite.
Идемпотентный — перезаписывает по bort.id.

Запуск:  python -m server.seed
"""
import json
from pathlib import Path

from .db import init_db, get_conn

SEED_PATH = Path(__file__).parent / "seed_data.json"


def seed() -> None:
    init_db()
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    with get_conn() as conn:
        # чистим
        conn.execute("DELETE FROM segments")
        conn.execute("DELETE FROM tracks")
        conn.execute("DELETE FROM log_entries")
        conn.execute("DELETE FROM borts")
        conn.execute("DELETE FROM queue")

        for b in data["DATA"]:
            conn.execute(
                "INSERT INTO borts (id, desc, priority, assignee, dept, case_start) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (b["id"], b.get("desc", ""), 0, "", "", b.get("caseStart", 0)),
            )
            for t_idx, t in enumerate(b["tracks"]):
                cur = conn.execute(
                    "INSERT INTO tracks (bort_id, name, is_sub, ord) VALUES (?, ?, ?, ?)",
                    (b["id"], t["name"], int(t.get("sub", False)), t_idx),
                )
                track_id = cur.lastrowid
                for s_idx, s in enumerate(t["segments"]):
                    conn.execute(
                        "INSERT INTO segments (track_id, kind, label, start, days, status, ord, dept, assignee) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (track_id, s["kind"], s["label"], s["start"], s["days"], s["status"], s_idx,
                         s.get("dept", ""), s.get("assignee", "")),
                    )
            for l in b.get("log", []):
                conn.execute(
                    "INSERT INTO log_entries (bort_id, date, stage, text) VALUES (?, ?, ?, ?)",
                    (b["id"], l["date"], l["stage"], l["text"]),
                )

        for q_idx, q in enumerate(data.get("QUEUE", [])):
            conn.execute(
                "INSERT INTO queue (id, reason, ord) VALUES (?, ?, ?)",
                (q["id"], q["reason"], q_idx),
            )

        conn.commit()

    n_borts = len(data["DATA"])
    n_queue = len(data.get("QUEUE", []))
    print(f"Seeded {n_borts} borts + {n_queue} queue items")


if __name__ == "__main__":
    seed()
