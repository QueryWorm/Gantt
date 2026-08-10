"""
Главный модуль FastAPI: lifecycle, эндпоинты.

Запуск: ../venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8765
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .db import init_db, get_conn
from .schemas import (
    Snapshot, Bort, Track, Segment, LogEntry, QueueItem,
    MutateRequest, SubtaskRequest, EventRequest, EventStats,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Fleet Repair Board API", lifespan=lifespan)

# CORS: разрешаем всё для локальной разработки (HTML открывается через file://)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- helpers ----

def _row_to_segment(r) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "label": r["label"],
        "start": r["start"],
        "days": r["days"],
        "status": r["status"],
        "ord": r["ord"],
    }


def _row_to_track(r, segments: list[dict]) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "sub": bool(r["is_sub"]),
        "segments": segments,
    }


def _row_to_bort(r, tracks: list[dict], logs: list[dict]) -> dict:
    return {
        "id": r["id"],
        "desc": r["desc"],
        "caseStart": r["case_start"],
        "tracks": tracks,
        "log": logs,
    }


def _load_snapshot() -> Snapshot:
    with get_conn() as conn:
        borts_rows = conn.execute("SELECT * FROM borts ORDER BY id").fetchall()
        borts: list[Bort] = []
        for br in borts_rows:
            tracks_rows = conn.execute(
                "SELECT * FROM tracks WHERE bort_id = ? ORDER BY ord, id",
                (br["id"],),
            ).fetchall()
            tracks: list[Track] = []
            for tr in tracks_rows:
                segs_rows = conn.execute(
                    "SELECT * FROM segments WHERE track_id = ? ORDER BY ord, id",
                    (tr["id"],),
                ).fetchall()
                segs = [Segment(**_row_to_segment(s)) for s in segs_rows]
                tracks.append(Track(**_row_to_track(tr, [s.model_dump() for s in segs])))
            log_rows = conn.execute(
                "SELECT * FROM log_entries WHERE bort_id = ? ORDER BY ts DESC, id DESC",
                (br["id"],),
            ).fetchall()
            logs = [LogEntry(
                id=r["id"], date=r["date"], stage=r["stage"], text=r["text"], ts=r["ts"]
            ) for r in log_rows]
            borts.append(Bort(**_row_to_bort(br, [t.model_dump() for t in tracks],
                                              [l.model_dump() for l in logs])))
        queue_rows = conn.execute("SELECT * FROM queue ORDER BY ord, id").fetchall()
        queue = [QueueItem(id=r["id"], reason=r["reason"]) for r in queue_rows]
    return Snapshot(DATA=borts, QUEUE=queue)


def _write_event(conn, session_id: str, type_: str, target: str, payload: dict) -> None:
    import json
    conn.execute(
        "INSERT INTO events (session_id, type, target, payload) VALUES (?, ?, ?, ?)",
        (session_id or "server", type_, target, json.dumps(payload, ensure_ascii=False)),
    )


# ---- endpoints ----

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/borts", response_model=Snapshot)
def get_borts():
    return _load_snapshot()


@app.post("/api/borts/{bort_id}/mutate")
def mutate_bort(bort_id: str, req: MutateRequest):
    """Закрыть активный сегмент на треке, открыть новый с today_index.
    Логика продублирована из HTML edit-save (lines 592-625)."""
    if not req.text.strip():
        raise HTTPException(400, "text is required")

    with get_conn() as conn:
        track = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND bort_id = ?",
            (req.track_id, bort_id),
        ).fetchone()
        if not track:
            raise HTTPException(404, f"track {req.track_id} not in bort {bort_id}")

        segs = conn.execute(
            "SELECT * FROM segments WHERE track_id = ? ORDER BY ord, id",
            (req.track_id,),
        ).fetchall()
        active = next((s for s in segs if s["status"] == "active"), None)

        if active:
            elapsed = req.today_index - active["start"]
            if elapsed > 0:
                conn.execute(
                    "UPDATE segments SET days = ?, status = 'done' WHERE id = ?",
                    (elapsed, active["id"]),
                )
            else:
                conn.execute("DELETE FROM segments WHERE id = ?", (active["id"],))

        # убираем устаревшие planned до today
        conn.execute(
            "DELETE FROM segments WHERE track_id = ? AND status = 'planned' AND start <= ?",
            (req.track_id, req.today_index),
        )

        # новый активный сегмент — ord в конец
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM segments WHERE track_id = ?",
            (req.track_id,),
        ).fetchone()[0]
        kind_label_map = {
            "work": "Работа", "test": "Тест", "think": "Разбор",
            "hold-parts": "Холд", "hold-people": "Холд", "hold-approve": "Холд",
        }
        conn.execute(
            "INSERT INTO segments (track_id, kind, label, start, days, status, ord) "
            "VALUES (?, ?, ?, ?, 0, 'active', ?)",
            (req.track_id, req.new_kind, kind_label_map.get(req.new_kind, req.new_kind),
             req.today_index, max_ord + 1),
        )

        # лог
        from datetime import datetime, timedelta, timezone
        EPOCH = datetime(2026, 6, 19)
        d = EPOCH + timedelta(days=req.today_index)
        date_str = f"{d.day:02d}.{d.month:02d}"
        status_label_map = {
            "work": "В работе", "test": "Тест", "think": "Разбор задачи",
            "hold-parts": "Холд · нет запчастей",
            "hold-people": "Холд · нет человека",
            "hold-approve": "Холд · нет решения",
        }
        conn.execute(
            "INSERT INTO log_entries (bort_id, date, stage, text) VALUES (?, ?, ?, ?)",
            (bort_id, date_str, f"{track['name']} → {status_label_map.get(req.new_kind, req.new_kind)}",
             req.text),
        )
        _write_event(conn, req.session_id, "mutation", bort_id, {
            "track_id": req.track_id,
            "track_name": track["name"],
            "new_kind": req.new_kind,
            "text_len": len(req.text),
            "closed_active": active is not None,
        })
        conn.commit()
    return {"ok": True}


@app.post("/api/borts/{bort_id}/subtasks")
def add_subtask(bort_id: str, req: SubtaskRequest):
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM borts WHERE id = ?", (bort_id,)).fetchone():
            raise HTTPException(404, f"bort {bort_id} not found")
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM tracks WHERE bort_id = ?",
            (bort_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO tracks (bort_id, name, is_sub, ord) VALUES (?, ?, 1, ?)",
            (bort_id, req.name, max_ord + 1),
        )
        track_id = cur.lastrowid
        conn.execute(
            "INSERT INTO segments (track_id, kind, label, start, days, status, ord) "
            "VALUES (?, 'work', 'Работа', ?, 0, 'active', 0)",
            (track_id, req.today_index),
        )
        from datetime import datetime, timedelta
        EPOCH = datetime(2026, 6, 19)
        d = EPOCH + timedelta(days=req.today_index)
        date_str = f"{d.day:02d}.{d.month:02d}"
        conn.execute(
            "INSERT INTO log_entries (bort_id, date, stage, text) VALUES (?, ?, ?, ?)",
            (bort_id, date_str, "Новая подзадача", f"Добавлена подзадача: {req.name}"),
        )
        _write_event(conn, req.session_id, "subtask_add", bort_id, {
            "track_id": track_id,
            "name": req.name,
        })
        conn.commit()
    return {"ok": True, "track_id": track_id}


@app.post("/api/events")
def post_event(req: EventRequest):
    import json
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (session_id, type, target, payload) VALUES (?, ?, ?, ?)",
            (req.session_id, req.type, req.target, json.dumps(req.payload, ensure_ascii=False)),
        )
        conn.commit()
    return {"ok": True}


@app.get("/api/events/stats", response_model=EventStats)
def events_stats(n: int = Query(20, ge=1, le=500)):
    import json
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        by_type_rows = conn.execute(
            "SELECT type, COUNT(*) AS c FROM events GROUP BY type ORDER BY c DESC"
        ).fetchall()
        last_rows = conn.execute(
            "SELECT ts, session_id, type, target, payload FROM events "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (n,),
        ).fetchall()
        last = [
            {
                "ts": r["ts"],
                "session_id": r["session_id"],
                "type": r["type"],
                "target": r["target"],
                "payload": json.loads(r["payload"]) if r["payload"] else {},
            }
            for r in last_rows
        ]
    return EventStats(
        total=total,
        by_type={r["type"]: r["c"] for r in by_type_rows},
        last_n=last,
    )


# ---- static: отдаём тот же HTML ----

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"hint": "положи fleet-repair-board_file.html в server/static/"}
