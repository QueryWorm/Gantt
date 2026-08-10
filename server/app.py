"""
Главный модуль FastAPI: lifecycle, эндпоинты.

Запуск: ../venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8765
"""
import json as _json
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
    SegmentPatchRequest, TrackPatchRequest, BortPatchRequest,
    BortCreateRequest, LogEntryRequest,
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
    import json
    try:
        depends_on = json.loads(r["depends_on"]) if r["depends_on"] else []
    except (json.JSONDecodeError, KeyError):
        depends_on = []
    return {
        "id": r["id"],
        "kind": r["kind"],
        "label": r["label"],
        "start": r["start"],
        "days": r["days"],
        "status": r["status"],
        "ord": r["ord"],
        "depends_on": depends_on,
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
        EPOCH = datetime(2026, 7, 19)
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

        # каскадный сдвиг downstream planned по dependencies
        shifted = []
        if active is not None:
            delta = elapsed - active["days"]
            if delta > 0:
                shifted = _cascade_shift_downstream(conn, active["id"], delta, set())
                if shifted:
                    _write_event(conn, req.session_id, "cascade_shift", bort_id, {
                        "trigger_seg_id": active["id"],
                        "delta": delta,
                        "shifted": shifted,
                    })

        _write_event(conn, req.session_id, "mutation", bort_id, {
            "track_id": req.track_id,
            "track_name": track["name"],
            "new_kind": req.new_kind,
            "text_len": len(req.text),
            "closed_active": active is not None,
        })
        conn.commit()
    return {"ok": True}


def _cascade_shift_downstream(conn, pred_id: int, delta: int, visited: set) -> list[dict]:
    """Сдвигает planned-сегменты, у которых pred_id в depends_on.
    Затем рекурсивно проходит по их downstream. Возвращает список сдвигов."""
    if pred_id in visited:
        return []
    visited.add(pred_id)
    rows = conn.execute("SELECT id, depends_on, start, status FROM segments").fetchall()
    shifted = []
    for s in rows:
        if s["status"] != "planned":
            continue
        try:
            deps = _json.loads(s["depends_on"] or "[]")
        except _json.JSONDecodeError:
            continue
        if pred_id not in deps:
            continue
        new_start = s["start"] + delta
        conn.execute("UPDATE segments SET start = ? WHERE id = ?", (new_start, s["id"]))
        shifted.append({"seg_id": s["id"], "old_start": s["start"], "new_start": new_start})
        shifted.extend(_cascade_shift_downstream(conn, s["id"], delta, visited))
    return shifted


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
        EPOCH = datetime(2026, 7, 19)
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


# ---- PATCH / DELETE для контекстного меню ----

_VALID_KINDS = {"work", "test", "think", "hold-parts", "hold-people", "hold-approve"}
_VALID_STATUSES = {"active", "done", "planned"}
_KIND_LABELS = {
    "work": "Работа", "test": "Тест", "think": "Разбор",
    "hold-parts": "Холд", "hold-people": "Холд", "hold-approve": "Холд",
}


@app.patch("/api/borts/{bort_id}/segments/{seg_id}")
def patch_segment(bort_id: str, seg_id: int, req: SegmentPatchRequest):
    with get_conn() as conn:
        seg = conn.execute(
            "SELECT s.* FROM segments s JOIN tracks t ON s.track_id = t.id "
            "WHERE s.id = ? AND t.bort_id = ?",
            (seg_id, bort_id),
        ).fetchone()
        if not seg:
            raise HTTPException(404, f"segment {seg_id} not in bort {bort_id}")

        updates: list[str] = []
        params: list = []
        changes: dict = {"seg_id": seg_id}

        if req.kind is not None:
            if req.kind not in _VALID_KINDS:
                raise HTTPException(400, f"invalid kind: {req.kind}")
            updates.append("kind = ?"); params.append(req.kind)
            updates.append("label = ?"); params.append(_KIND_LABELS.get(req.kind, req.kind))
            changes["kind"] = req.kind
        if req.status is not None:
            if req.status not in _VALID_STATUSES:
                raise HTTPException(400, f"invalid status: {req.status}")
            updates.append("status = ?"); params.append(req.status)
            changes["status"] = req.status
        if req.days is not None:
            if req.days < 0:
                raise HTTPException(400, "days must be >= 0")
            updates.append("days = ?"); params.append(req.days)
            changes["days"] = req.days
        if req.start is not None:
            updates.append("start = ?"); params.append(req.start)
            changes["start"] = req.start
        if req.depends_on is not None:
            updates.append("depends_on = ?"); params.append(_json.dumps(req.depends_on))
            changes["depends_on"] = req.depends_on

        if not updates:
            raise HTTPException(400, "nothing to update")

        params.append(seg_id)
        conn.execute(f"UPDATE segments SET {', '.join(updates)} WHERE id = ?", params)
        _write_event(conn, req.session_id, "segment_patch", bort_id, changes)
        conn.commit()
    return {"ok": True}


@app.post("/api/borts/{bort_id}/segments/{seg_id}/activate")
def activate_segment(bort_id: str, seg_id: int, session_id: str = ""):
    """Переводит planned в active. Проверяет, что все зависимости done.
    Возвращает 409 если блокировано, 400 если уже не planned."""
    with get_conn() as conn:
        seg = conn.execute(
            "SELECT s.* FROM segments s JOIN tracks t ON s.track_id = t.id "
            "WHERE s.id = ? AND t.bort_id = ?",
            (seg_id, bort_id),
        ).fetchone()
        if not seg:
            raise HTTPException(404, f"segment {seg_id} not in bort {bort_id}")
        if seg["status"] != "planned":
            raise HTTPException(400, f"segment is {seg['status']}, not planned")

        # зависимости
        try:
            deps = _json.loads(seg["depends_on"] or "[]")
        except _json.JSONDecodeError:
            deps = []
        if deps:
            placeholders = ",".join("?" * len(deps))
            deps_rows = conn.execute(
                f"SELECT id, status FROM segments WHERE id IN ({placeholders})",
                deps,
            ).fetchall()
            blocked = [r["id"] for r in deps_rows if r["status"] != "done"]
            if blocked:
                raise HTTPException(409, detail={
                    "error": "blocked_by_dependencies",
                    "blocked": blocked,
                    "message": "не все предшественники закрыты",
                })

        conn.execute("UPDATE segments SET status = 'active', days = 0 WHERE id = ?", (seg_id,))
        _write_event(conn, session_id, "segment_activate", bort_id, {"seg_id": seg_id})
        conn.commit()
    return {"ok": True}


@app.delete("/api/borts/{bort_id}/segments/{seg_id}")
def delete_segment(bort_id: str, seg_id: int, session_id: str = ""):
    with get_conn() as conn:
        seg = conn.execute(
            "SELECT s.* FROM segments s JOIN tracks t ON s.track_id = t.id "
            "WHERE s.id = ? AND t.bort_id = ?",
            (seg_id, bort_id),
        ).fetchone()
        if not seg:
            raise HTTPException(404, f"segment {seg_id} not in bort {bort_id}")
        conn.execute("DELETE FROM segments WHERE id = ?", (seg_id,))
        _write_event(conn, session_id, "segment_delete", bort_id, {
            "seg_id": seg_id, "kind": seg["kind"], "status": seg["status"],
        })
        conn.commit()
    return {"ok": True}


@app.patch("/api/borts/{bort_id}/tracks/{track_id}")
def patch_track(bort_id: str, track_id: int, req: TrackPatchRequest):
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    with get_conn() as conn:
        tr = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND bort_id = ?",
            (track_id, bort_id),
        ).fetchone()
        if not tr:
            raise HTTPException(404, f"track {track_id} not in bort {bort_id}")
        conn.execute("UPDATE tracks SET name = ? WHERE id = ?", (req.name, track_id))
        _write_event(conn, req.session_id, "track_patch", bort_id, {
            "track_id": track_id, "old_name": tr["name"], "new_name": req.name,
        })
        conn.commit()
    return {"ok": True}


@app.delete("/api/borts/{bort_id}/tracks/{track_id}")
def delete_track(bort_id: str, track_id: int, session_id: str = ""):
    with get_conn() as conn:
        tr = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND bort_id = ?",
            (track_id, bort_id),
        ).fetchone()
        if not tr:
            raise HTTPException(404, f"track {track_id} not in bort {bort_id}")
        seg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM segments WHERE track_id = ?", (track_id,),
        ).fetchone()["c"]
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        _write_event(conn, session_id, "track_delete", bort_id, {
            "track_id": track_id, "name": tr["name"], "segments_deleted": seg_count,
        })
        conn.commit()
    return {"ok": True, "segments_deleted": seg_count}


@app.patch("/api/borts/{bort_id}")
def patch_bort(bort_id: str, req: BortPatchRequest):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM borts WHERE id = ?", (bort_id,)).fetchone()
        if not b:
            raise HTTPException(404, f"bort {bort_id} not found")
        conn.execute("UPDATE borts SET desc = ? WHERE id = ?", (req.desc, bort_id))
        _write_event(conn, req.session_id, "bort_patch", bort_id, {
            "old_desc": b["desc"], "new_desc": req.desc,
        })
        conn.commit()
    return {"ok": True}


@app.delete("/api/borts/{bort_id}")
def delete_bort(bort_id: str, session_id: str = ""):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM borts WHERE id = ?", (bort_id,)).fetchone()
        if not b:
            raise HTTPException(404, f"bort {bort_id} not found")
        track_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tracks WHERE bort_id = ?", (bort_id,),
        ).fetchone()["c"]
        conn.execute("DELETE FROM borts WHERE id = ?", (bort_id,))
        _write_event(conn, session_id, "bort_delete", bort_id, {
            "desc": b["desc"], "tracks_deleted": track_count,
        })
        conn.commit()
    return {"ok": True, "tracks_deleted": track_count}


@app.post("/api/borts")
def create_bort(req: BortCreateRequest):
    bort_id = req.id.strip()
    if not bort_id:
        raise HTTPException(400, "id is required")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM borts WHERE id = ?", (bort_id,)).fetchone():
            raise HTTPException(409, f"bort {bort_id} already exists")
        conn.execute(
            "INSERT INTO borts (id, desc, priority, assignee, case_start) "
            "VALUES (?, ?, ?, ?, ?)",
            (bort_id, req.desc, req.priority, req.assignee, req.case_start),
        )
        _write_event(conn, req.session_id, "bort_create", bort_id, {
            "desc": req.desc, "case_start": req.case_start,
        })
        conn.commit()
    return {"ok": True, "id": bort_id}


@app.post("/api/borts/{bort_id}/log")
def add_log_entry(bort_id: str, req: LogEntryRequest):
    """Запись в лог без смены статуса. Каждый день можно оставлять записи."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    from datetime import datetime, timedelta
    epoch = datetime(2026, 7, 19)
    d = epoch + timedelta(days=req.today_index)
    date_str = f"{d.day:02d}.{d.month:02d}"

    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM borts WHERE id = ?", (bort_id,)).fetchone():
            raise HTTPException(404, f"bort {bort_id} not found")
        stage = req.stage.strip()
        if not stage and req.track_id:
            tr = conn.execute("SELECT name FROM tracks WHERE id = ?", (req.track_id,)).fetchone()
            if tr:
                stage = f"Лог · {tr['name']}"
        if not stage:
            stage = "Лог"
        conn.execute(
            "INSERT INTO log_entries (bort_id, date, stage, text) VALUES (?, ?, ?, ?)",
            (bort_id, date_str, stage, text),
        )
        _write_event(conn, req.session_id, "log_entry", bort_id, {
            "stage": stage, "date": date_str, "text_len": len(text),
            "track_id": req.track_id,
        })
        conn.commit()
    return {"ok": True, "date": date_str, "stage": stage}


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
