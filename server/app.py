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
    BortCreateRequest, LogEntryRequest, TrackSegmentRequest,
    TemplateRequest, TemplateApplyRequest, TemplateSummary, Template,
    TemplateTrack, TemplateSegment,
    TemplateTrackRequest, TemplateTrackPatchRequest,
    TemplateSegmentRequest, TemplateSegmentPatchRequest,
    TemplatePatchRequest,
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
        "dept": r["dept"] or "",
        "assignee": r["assignee"] or "",
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
        "priority": r["priority"] or 0,
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


def _propagate_segment_starts(conn, bort_id: str, today_index: int = 0):
    """Пересчитывает start сегментов с depends_on.

    Правило: start = max(естественное место в треке, конец предшественников).
    Естественное место — сразу после предыдущего сегмента того же трека (или
    текущий start, если сегмент первый в треке). Сегменты без depends_on не трогает.

    Это позволяет не только сдвигать ВПЕРЁД, когда предшественник удлинился,
    но и возвращать назад, когда зависимость снята или предшественник укоротился."""
    segs = conn.execute(
        "SELECT s.* FROM segments s JOIN tracks t ON s.track_id = t.id "
        "WHERE t.bort_id = ? ORDER BY s.track_id, s.ord, s.id",
        (bort_id,),
    ).fetchall()
    seg_map = {r["id"]: dict(r) for r in segs}
    if not seg_map:
        return 0

    by_track: dict[int, list[dict]] = {}
    for r in segs:
        by_track.setdefault(r["track_id"], []).append(dict(r))

    def end_of(s: dict) -> int:
        days = max(0, s["days"])
        if s["status"] == "active":
            return max(s["start"] + days, today_index)
        return s["start"] + days

    shifted = 0
    for _round in range(100):
        any_change = False
        for _tid, track_segs in by_track.items():
            prev_end = None
            for s in track_segs:
                if prev_end is None:
                    natural = s["start"]
                else:
                    natural = prev_end
                try:
                    deps = _json.loads(s["depends_on"]) if s["depends_on"] else []
                except (_json.JSONDecodeError, KeyError):
                    deps = []
                if deps:
                    max_end = 0
                    for d in deps:
                        pred = seg_map.get(d)
                        if pred:
                            max_end = max(max_end, end_of(pred))
                    new_start = max(natural, max_end)
                    if new_start != s["start"]:
                        conn.execute("UPDATE segments SET start = ? WHERE id = ?", (new_start, s["id"]))
                        s["start"] = new_start
                        shifted += 1
                        any_change = True
                prev_end = end_of(s)
        if not any_change:
            break
    return shifted


def _natural_start_in_track(conn, track_id: int, seg_id: int, today_index: int) -> int | None:
    """Естественный start сегмента в его треке: конец предыдущего сегмента по ord.
    None — если сегмент первый в треке (естественное место не определено)."""
    prev = conn.execute(
        "SELECT start, days, status FROM segments "
        "WHERE track_id = ? AND id != ? AND (ord < (SELECT ord FROM segments WHERE id = ?) "
        " OR (ord = (SELECT ord FROM segments WHERE id = ?) AND id < ?)) "
        "ORDER BY ord, id DESC LIMIT 1",
        (track_id, seg_id, seg_id, seg_id, seg_id),
    ).fetchone()
    if not prev:
        return None
    days = max(0, prev["days"])
    if prev["status"] == "active":
        return max(prev["start"] + days, today_index)
    return prev["start"] + days


def _shift_template_starts(out_segments: list[dict]) -> None:
    """Для шаблонов: сдвигает синтезированные start по depends_on (все planned)."""
    by_id = {s["id"]: s for s in out_segments}
    rounds = 0
    while rounds < 100:
        any_change = False
        for s in out_segments:
            deps = s.get("depends_on") or []
            if not deps:
                continue
            max_end = 0
            for d in deps:
                pred = by_id.get(d)
                if pred:
                    max_end = max(max_end, pred["start"] + max(0, pred["days"]))
            new_start = max(s["start"], max_end)
            if new_start != s["start"]:
                s["start"] = new_start
                any_change = True
        if not any_change:
            break
        rounds += 1


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
            "INSERT INTO segments (track_id, kind, label, start, days, status, ord, dept, assignee) "
            "VALUES (?, ?, ?, ?, 0, 'active', ?, '', '')",
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

        _write_event(conn, req.session_id, "mutation", bort_id, {
            "track_id": req.track_id,
            "track_name": track["name"],
            "new_kind": req.new_kind,
            "text_len": len(req.text),
            "closed_active": active is not None,
        })
        shifted_deps = _propagate_segment_starts(conn, bort_id, req.today_index)
        if shifted_deps:
            _write_event(conn, req.session_id, "dep_propagate", bort_id, {"shifted": shifted_deps})
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
            "INSERT INTO segments (track_id, kind, label, start, days, status, ord, dept, assignee) "
            "VALUES (?, 'work', 'Работа', ?, 0, 'active', 0, '', '')",
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
        if req.dept is not None:
            updates.append("dept = ?"); params.append(req.dept)
            changes["dept"] = req.dept
        if req.assignee is not None:
            updates.append("assignee = ?"); params.append(req.assignee)
            changes["assignee"] = req.assignee

        if not updates:
            raise HTTPException(400, "nothing to update")

        params.append(seg_id)
        conn.execute(f"UPDATE segments SET {', '.join(updates)} WHERE id = ?", params)

        # сняли все зависимости → сегмент возвращается к "естественному" месту в треке
        if req.depends_on is not None and not req.depends_on and req.start is None:
            natural = _natural_start_in_track(conn, seg["track_id"], seg_id, req.today_index)
            if natural is not None and seg["start"] != natural:
                conn.execute("UPDATE segments SET start = ? WHERE id = ?", (natural, seg_id))
                changes["start"] = natural

        shifted_deps = _propagate_segment_starts(conn, bort_id, req.today_index)
        changes["shifted_by_deps"] = shifted_deps
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


@app.post("/api/borts/{bort_id}/tracks/{track_id}/segments")
def add_bort_segment(bort_id: str, track_id: int, req: TrackSegmentRequest):
    """Добавить сегмент в трек борта. Старт: после последнего сегмента трека,
    либо с today_index для пустого трека."""
    if not req.label.strip():
        raise HTTPException(400, "label is required")
    with get_conn() as conn:
        tr = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND bort_id = ?",
            (track_id, bort_id),
        ).fetchone()
        if not tr:
            raise HTTPException(404, f"track {track_id} not in bort {bort_id}")
        b = conn.execute("SELECT case_start FROM borts WHERE id = ?", (bort_id,)).fetchone()
        case_start = b["case_start"] if b else 0
        last = conn.execute(
            "SELECT start, days FROM segments WHERE track_id = ? ORDER BY ord, id DESC LIMIT 1",
            (track_id,),
        ).fetchone()
        if last:
            start = last["start"] + max(0, last["days"])
        else:
            start = req.today_index - case_start
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM segments WHERE track_id = ?", (track_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO segments (track_id, kind, label, start, days, status, ord) "
            "VALUES (?, ?, ?, ?, ?, 'planned', ?)",
            (track_id, req.kind, req.label, start, max(0, req.days), max_ord + 1),
        )
        seg_id = cur.lastrowid
        _write_event(conn, req.session_id, "segment_add", bort_id, {
            "track_id": track_id, "seg_id": seg_id, "label": req.label, "start": start,
        })
        _propagate_segment_starts(conn, bort_id, req.today_index)
        conn.commit()
    return {"ok": True, "seg_id": seg_id, "start": start}


@app.patch("/api/borts/{bort_id}")
def patch_bort(bort_id: str, req: BortPatchRequest):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM borts WHERE id = ?", (bort_id,)).fetchone()
        if not b:
            raise HTTPException(404, f"bort {bort_id} not found")
        updates = []
        params = []
        changes = {}
        if req.desc is not None:
            updates.append("desc = ?"); params.append(req.desc)
            changes["old_desc"] = b["desc"]; changes["new_desc"] = req.desc
        if not updates:
            raise HTTPException(400, "nothing to update")
        params.append(bort_id)
        conn.execute(f"UPDATE borts SET {', '.join(updates)} WHERE id = ?", params)
        _write_event(conn, req.session_id, "bort_patch", bort_id, changes)
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
            "INSERT INTO borts (id, desc, priority, case_start) "
            "VALUES (?, ?, ?, ?)",
            (bort_id, req.desc, req.priority, req.case_start),
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


# ---- Шаблоны пайплайна ----

def _load_template_full(conn, template_id: str) -> dict | None:
    """Загружает шаблон из нормализованных таблиц."""
    tpl = conn.execute("SELECT id, name FROM templates WHERE id = ?", (template_id,)).fetchone()
    if not tpl:
        return None
    tracks = conn.execute(
        "SELECT * FROM template_tracks WHERE template_id = ? ORDER BY ord, id",
        (template_id,),
    ).fetchall()
    out_tracks = []
    for t in tracks:
        segs = conn.execute(
            "SELECT * FROM template_segments WHERE track_id = ? ORDER BY ord, id",
            (t["id"],),
        ).fetchall()
        track_cursor = 0
        out_segments = []
        for s in segs:
            try:
                depends_on = _json.loads(s["depends_on"]) if s["depends_on"] else []
            except (_json.JSONDecodeError, KeyError):
                depends_on = []
            if s["start"] >= 0:
                seg_start = s["start"]
            else:
                seg_start = track_cursor
            out_segments.append({
                "id": s["id"],
                "kind": s["kind"],
                "label": s["label"],
                "start": seg_start,
                "days": s["days"],
                "status": "planned",
                "depends_on": depends_on,
                "dept": s["dept"] or "",
                "assignee": s["assignee"] or "",
            })
            track_cursor = seg_start + max(0, s["days"])
        out_tracks.append({
            "id": t["id"],
            "name": t["name"],
            "sub": bool(t["is_sub"]),
            "segments": out_segments,
        })
    # сдвиг по зависимостям на уровне ВСЕГО шаблона (зависимости могут быть между треками)
    all_segments = [s for t in out_tracks for s in t["segments"]]
    _shift_template_starts(all_segments)
    return {"id": tpl["id"], "name": tpl["name"], "tracks": out_tracks}


@app.get("/api/templates", response_model=list[TemplateSummary])
def list_templates():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM templates ORDER BY created_at").fetchall()
        result = []
        for r in rows:
            tracks_count = conn.execute(
                "SELECT COUNT(*) AS c FROM template_tracks WHERE template_id = ?", (r["id"],),
            ).fetchone()["c"]
            segs_count = conn.execute(
                "SELECT COUNT(*) AS c FROM template_segments s JOIN template_tracks t ON s.track_id = t.id WHERE t.template_id = ?",
                (r["id"],),
            ).fetchone()["c"]
            result.append(TemplateSummary(
                id=r["id"], name=r["name"],
                tracks_count=tracks_count, segments_count=segs_count,
            ))
        return result


@app.get("/api/templates/{template_id}", response_model=Template)
def get_template(template_id: str):
    with get_conn() as conn:
        full = _load_template_full(conn, template_id)
        if not full:
            raise HTTPException(404, f"template {template_id} not found")
        return Template(id=full["id"], name=full["name"], tracks=full["tracks"])


@app.post("/api/templates")
def create_template(req: TemplateRequest):
    tid = req.id.strip()
    if not tid:
        raise HTTPException(400, "id is required")
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM templates WHERE id = ?", (tid,)).fetchone():
            raise HTTPException(409, f"template {tid} already exists")
        conn.execute(
            "INSERT INTO templates (id, name) VALUES (?, ?)",
            (tid, req.name.strip()),
        )
        # дефолтный главный трек с одним пустым сегментом
        conn.execute(
            "INSERT INTO template_tracks (template_id, name, is_sub, ord) VALUES (?, ?, 0, 0)",
            (tid, "Главный"),
        )
        _write_event(conn, req.session_id, "template_create", tid, {"name": req.name})
        conn.commit()
    return {"ok": True, "id": tid}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, session_id: str = ""):
    with get_conn() as conn:
        r = conn.execute("SELECT id, name FROM templates WHERE id = ?", (template_id,)).fetchone()
        if not r:
            raise HTTPException(404, f"template {template_id} not found")
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        _write_event(conn, session_id, "template_delete", template_id, {"name": r["name"]})
        conn.commit()
    return {"ok": True}


@app.patch("/api/templates/{template_id}")
def patch_template(template_id: str, req: TemplatePatchRequest):
    with get_conn() as conn:
        r = conn.execute("SELECT id, name FROM templates WHERE id = ?", (template_id,)).fetchone()
        if not r:
            raise HTTPException(404, f"template {template_id} not found")
        if req.name is not None:
            conn.execute("UPDATE templates SET name = ? WHERE id = ?", (req.name, template_id))
        _write_event(conn, req.session_id, "template_patch", template_id, {"new_name": req.name})
        conn.commit()
    return {"ok": True}


@app.post("/api/templates/{template_id}/tracks")
def add_template_track(template_id: str, req: TemplateTrackRequest):
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM templates WHERE id = ?", (template_id,)).fetchone():
            raise HTTPException(404, f"template {template_id} not found")
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM template_tracks WHERE template_id = ?", (template_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO template_tracks (template_id, name, is_sub, ord) VALUES (?, ?, ?, ?)",
            (template_id, req.name.strip(), int(req.sub), max_ord + 1),
        )
        track_id = cur.lastrowid
        _write_event(conn, req.session_id, "template_track_add", template_id, {
            "track_id": track_id, "name": req.name, "sub": req.sub,
        })
        conn.commit()
    return {"ok": True, "track_id": track_id}


@app.patch("/api/templates/{template_id}/tracks/{track_id}")
def patch_template_track(template_id: str, track_id: int, req: TemplateTrackPatchRequest):
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    with get_conn() as conn:
        t = conn.execute(
            "SELECT * FROM template_tracks WHERE id = ? AND template_id = ?",
            (track_id, template_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, f"track {track_id} not in template {template_id}")
        conn.execute("UPDATE template_tracks SET name = ? WHERE id = ?", (req.name, track_id))
        _write_event(conn, req.session_id, "template_track_patch", template_id, {
            "track_id": track_id, "old_name": t["name"], "new_name": req.name,
        })
        conn.commit()
    return {"ok": True}


@app.delete("/api/templates/{template_id}/tracks/{track_id}")
def delete_template_track(template_id: str, track_id: int, session_id: str = ""):
    with get_conn() as conn:
        t = conn.execute(
            "SELECT * FROM template_tracks WHERE id = ? AND template_id = ?",
            (track_id, template_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, f"track {track_id} not in template {template_id}")
        seg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM template_segments WHERE track_id = ?", (track_id,),
        ).fetchone()["c"]
        conn.execute("DELETE FROM template_tracks WHERE id = ?", (track_id,))
        _write_event(conn, session_id, "template_track_delete", template_id, {
            "track_id": track_id, "name": t["name"], "segments_deleted": seg_count,
        })
        conn.commit()
    return {"ok": True, "segments_deleted": seg_count}


@app.post("/api/templates/{template_id}/tracks/{track_id}/segments")
def add_template_segment(template_id: str, track_id: int, req: TemplateSegmentRequest):
    if not req.label.strip():
        raise HTTPException(400, "label is required")
    with get_conn() as conn:
        t = conn.execute(
            "SELECT * FROM template_tracks WHERE id = ? AND template_id = ?",
            (track_id, template_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, f"track {track_id} not in template {template_id}")
        max_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM template_segments WHERE track_id = ?", (track_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO template_segments (track_id, kind, label, days, dept, assignee, start, ord) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (track_id, req.kind, req.label, max(0, req.days), req.dept, req.assignee, req.start, max_ord + 1),
        )
        seg_id = cur.lastrowid
        _write_event(conn, req.session_id, "template_segment_add", template_id, {
            "track_id": track_id, "seg_id": seg_id, "kind": req.kind, "label": req.label,
        })
        conn.commit()
    return {"ok": True, "seg_id": seg_id}


@app.patch("/api/templates/{template_id}/tracks/{track_id}/segments/{seg_id}")
def patch_template_segment(template_id: str, track_id: int, seg_id: int, req: TemplateSegmentPatchRequest):
    with get_conn() as conn:
        s = conn.execute(
            "SELECT s.* FROM template_segments s JOIN template_tracks t ON s.track_id = t.id "
            "WHERE s.id = ? AND t.id = ? AND t.template_id = ?",
            (seg_id, track_id, template_id),
        ).fetchone()
        if not s:
            raise HTTPException(404, f"segment {seg_id} not found")
        updates, params, changes = [], [], {}
        if req.kind is not None:
            updates.append("kind = ?"); params.append(req.kind); changes["kind"] = req.kind
        if req.label is not None:
            updates.append("label = ?"); params.append(req.label); changes["label"] = req.label
        if req.days is not None:
            updates.append("days = ?"); params.append(max(0, req.days)); changes["days"] = req.days
        if req.dept is not None:
            updates.append("dept = ?"); params.append(req.dept); changes["dept"] = req.dept
        if req.assignee is not None:
            updates.append("assignee = ?"); params.append(req.assignee); changes["assignee"] = req.assignee
        if req.depends_on is not None:
            updates.append("depends_on = ?")
            params.append(_json.dumps(req.depends_on, ensure_ascii=False))
            changes["depends_on"] = req.depends_on
        if req.start is not None:
            updates.append("start = ?"); params.append(max(-1, req.start)); changes["start"] = req.start
        if not updates:
            raise HTTPException(400, "nothing to update")
        params.append(seg_id)
        conn.execute(f"UPDATE template_segments SET {', '.join(updates)} WHERE id = ?", params)
        _write_event(conn, req.session_id, "template_segment_patch", template_id, changes)
        conn.commit()
    return {"ok": True}


@app.delete("/api/templates/{template_id}/tracks/{track_id}/segments/{seg_id}")
def delete_template_segment(template_id: str, track_id: int, seg_id: int, session_id: str = ""):
    with get_conn() as conn:
        s = conn.execute(
            "SELECT s.* FROM template_segments s JOIN template_tracks t ON s.track_id = t.id "
            "WHERE s.id = ? AND t.id = ? AND t.template_id = ?",
            (seg_id, track_id, template_id),
        ).fetchone()
        if not s:
            raise HTTPException(404, f"segment {seg_id} not found")
        conn.execute("DELETE FROM template_segments WHERE id = ?", (seg_id,))
        _write_event(conn, session_id, "template_segment_delete", template_id, {
            "seg_id": seg_id, "kind": s["kind"], "label": s["label"],
        })
        conn.commit()
    return {"ok": True}


@app.post("/api/borts/{bort_id}/apply_template")
def apply_template(bort_id: str, req: TemplateApplyRequest):
    """Применяет шаблон к борту: добавляет tracks и segments.
    Сегменты становятся planned, начиная с req.today_index."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM templates WHERE id = ?", (req.template_id,)).fetchone():
            raise HTTPException(404, f"template {req.template_id} not found")
        if not conn.execute("SELECT 1 FROM borts WHERE id = ?", (bort_id,)).fetchone():
            raise HTTPException(404, f"bort {bort_id} not found")

        tracks_added = 0
        segs_added = 0
        max_track_ord = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM tracks WHERE bort_id = ?", (bort_id,),
        ).fetchone()[0]
        tracks = conn.execute(
            "SELECT * FROM template_tracks WHERE template_id = ? ORDER BY ord, id",
            (req.template_id,),
        ).fetchall()
        seg_id_map = {}
        pending_deps = []
        for t in tracks:
            max_track_ord += 1
            cur = conn.execute(
                "INSERT INTO tracks (bort_id, name, is_sub, ord) VALUES (?, ?, ?, ?)",
                (bort_id, t["name"], int(t["is_sub"]), max_track_ord),
            )
            track_id = cur.lastrowid
            tracks_added += 1
            # каждый трек стартует параллельно с req.today_index;
            # внутри трека сегменты идут последовательно (как в шаблоне)
            track_cursor = req.today_index
            segs = conn.execute(
                "SELECT * FROM template_segments WHERE track_id = ? ORDER BY ord, id",
                (t["id"],),
            ).fetchall()
            for s_idx, s in enumerate(segs):
                if s["start"] >= 0:
                    start = req.today_index + s["start"]
                else:
                    start = track_cursor
                days = max(0, int(s["days"]))
                scur = conn.execute(
                    "INSERT INTO segments (track_id, kind, label, start, days, status, ord, dept, assignee) "
                    "VALUES (?, ?, ?, ?, ?, 'planned', ?, ?, ?)",
                    (track_id, s["kind"], s["label"], start, days, s_idx,
                     s["dept"] or "", s["assignee"] or ""),
                )
                seg_id_map[s["id"]] = scur.lastrowid
                try:
                    deps = _json.loads(s["depends_on"]) if s["depends_on"] else []
                except (_json.JSONDecodeError, KeyError):
                    deps = []
                if deps:
                    pending_deps.append((scur.lastrowid, deps))
                segs_added += 1
                track_cursor += days
        # проставить зависимости с маппингом template_segment.id -> segments.id
        for new_seg_id, deps in pending_deps:
            mapped = [seg_id_map.get(d, d) for d in deps if d in seg_id_map]
            if mapped:
                conn.execute(
                    "UPDATE segments SET depends_on = ? WHERE id = ?",
                    (_json.dumps(mapped), new_seg_id),
                )
        # авто-сдвиг: зависимые сегменты стартуют после конца предшественников
        shifted_deps = _propagate_segment_starts(conn, bort_id, req.today_index)

        _write_event(conn, req.session_id, "template_apply", bort_id, {
            "template_id": req.template_id,
            "tracks_added": tracks_added,
            "segments_added": segs_added,
            "shifted_by_deps": shifted_deps,
        })
        conn.commit()
    return {"ok": True, "tracks_added": tracks_added, "segments_added": segs_added}


# ---- Тестовые данные ----

_TEST_DATA = [
    {
        "id": "FPV-01", "desc": "нет телеметрии, протокол не MAVLink — разбираемся",
        "case_start": 20, "priority": 1,
        "tracks": [
            {"name": "Диагностика телеметрии", "sub": False, "segments": [
                {"kind": "think", "label": "Разбор", "start": 20, "days": 1, "status": "active"},
            ]},
        ],
        "log": [
            {"date": "09.08", "stage": "Разбор", "text": "Сняли дамп UART, протокол не MAVLink, похоже на проприетарный."},
        ],
    },
    {
        "id": "FPV-03", "desc": "интеграция Hikvision PTZ — кронштейн + сборка",
        "case_start": 0, "priority": 2,
        "tracks": [
            {"name": "Прошивка электроники", "sub": False, "segments": [
                {"kind": "think", "label": "Разбор", "start": 0, "days": 1, "status": "done", "dept": "телеметрия", "assignee": "Саша"},
                {"kind": "work", "label": "Прошивка", "start": 1, "days": 3, "status": "done", "dept": "телеметрия", "assignee": "Саша"},
                {"kind": "hold-people", "label": "Холд", "start": 6, "days": 15, "status": "active", "dept": "механика", "assignee": "Коля"},
                {"kind": "test", "label": "Тест (план)", "start": 21, "days": 2, "status": "planned", "dept": "телеметрия", "assignee": "Саша"},
            ]},
            {"name": "Разработка кронштейна", "sub": True, "segments": [
                {"kind": "work", "label": "Разработка", "start": 1, "days": 3, "status": "done", "dept": "механика", "assignee": "Коля"},
            ]},
            {"name": "Сварка кронштейна", "sub": True, "segments": [
                {"kind": "work", "label": "Сварка", "start": 4, "days": 2, "status": "done", "dept": "механика", "assignee": "Коля"},
            ]},
        ],
        "log": [
            {"date": "09.08", "stage": "Холд", "text": "Кронштейн сварен и прошивка готова ещё с 25.07 — сборщик всё это время занят на других бортах."},
            {"date": "25.07", "stage": "Сварка → Холд", "text": "Сварка кронштейна закончена, сошлись на сборке — и тут же встали."},
            {"date": "23.07", "stage": "Разработка → Сварка", "text": "Чертежи кронштейна утверждены, сварка стартовала следом."},
            {"date": "20.07", "stage": "Разбор → Работа", "text": "Начали прошивку и разработку кронштейна параллельно."},
        ],
    },
    {
        "id": "FPV-05", "desc": "нестабильное видео, подозрение на Majestic",
        "case_start": 15, "priority": 1,
        "tracks": [
            {"name": "Работа над видео", "sub": False, "segments": [
                {"kind": "work", "label": "Замена антенны", "start": 15, "days": 2, "status": "done", "dept": "видео", "assignee": "Миша"},
                {"kind": "test", "label": "Тест №1", "start": 17, "days": 1, "status": "done", "dept": "видео", "assignee": "Миша"},
                {"kind": "work", "label": "Доработка", "start": 18, "days": 2, "status": "done", "dept": "телеметрия", "assignee": "Дима"},
                {"kind": "test", "label": "Тест №2", "start": 20, "days": 1, "status": "active", "dept": "видео", "assignee": "Миша"},
                {"kind": "work", "label": "Возврат в строй (план)", "start": 21, "days": 1, "status": "planned"},
            ]},
        ],
        "log": [
            {"date": "09.08", "stage": "Тест №2", "text": "Второй облёт, видео стабильно — ждём подтверждения на дистанции."},
            {"date": "06.08", "stage": "Доработка", "text": "Тест №1 показал зависания на 300м+, откатили прошивку Majestic."},
            {"date": "05.08", "stage": "Тест №1", "text": "Первый тест после замены антенны."},
        ],
    },
    {
        "id": "FPV-07", "desc": "замена регуля после краша",
        "case_start": 18, "priority": 2,
        "tracks": [
            {"name": "Замена ESC", "sub": False, "segments": [
                {"kind": "work", "label": "Демонтаж", "start": 18, "days": 1, "status": "done"},
                {"kind": "hold-parts", "label": "Ожидание ESC", "start": 19, "days": 4, "status": "active"},
                {"kind": "work", "label": "Установка", "start": 23, "days": 1, "status": "planned"},
                {"kind": "test", "label": "Тест", "start": 24, "days": 1, "status": "planned"},
            ]},
        ],
        "log": [
            {"date": "07.08", "stage": "Холд · нет запчастей", "text": "Заказали ESC с Ali, доставка 4-5 дней."},
            {"date": "06.08", "stage": "Демонтаж", "text": "Сняли сгоревший регуль, обмотка в порядке."},
        ],
    },
    {
        "id": "FPV-09", "desc": "прошивка под управление через ELRS",
        "case_start": 22, "priority": 3,
        "tracks": [
            {"name": "Переход на ELRS", "sub": False, "segments": [
                {"kind": "think", "label": "Изучение", "start": 22, "days": 2, "status": "active"},
                {"kind": "work", "label": "Прошивка TX", "start": 24, "days": 1, "status": "planned"},
                {"kind": "work", "label": "Прошивка RX", "start": 25, "days": 1, "status": "planned"},
                {"kind": "test", "label": "Полевой тест", "start": 26, "days": 2, "status": "planned"},
            ]},
        ],
        "log": [],
    },
    {
        "id": "FPV-11", "desc": "командная задача — нужен электронщик, но не критично кто",
        "case_start": 22, "priority": 4,
        "tracks": [
            {"name": "Ревизия платы", "sub": False, "segments": [
                {"kind": "think", "label": "Осмотр", "start": 22, "days": 1, "status": "active"},
                {"kind": "work", "label": "Прозвонка", "start": 23, "days": 2, "status": "planned"},
            ]},
        ],
        "log": [
            {"date": "09.08", "stage": "Осмотр", "text": "Назначено отделу, конкретный исполнитель определится при осмотре."},
        ],
    },
]


@app.post("/api/seed-test")
def seed_test_data():
    """Заполняет БД расширенным набором тестовых бортов с исполнителями.
    Перезаписывает существующие (id-шники совпадают с затрагиваемыми)."""
    created = 0
    with get_conn() as conn:
        for b in _TEST_DATA:
            existing = conn.execute("SELECT id FROM borts WHERE id = ?", (b["id"],)).fetchone()
            if existing:
                conn.execute("DELETE FROM borts WHERE id = ?", (b["id"],))
            conn.execute(
                "INSERT INTO borts (id, desc, priority, case_start) "
                "VALUES (?, ?, ?, ?)",
                (b["id"], b["desc"], b["priority"], b["case_start"]),
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
            created += 1
        _write_event(conn, "seed-test", "seed", "seed-test", {"created": created})
        conn.commit()
    return {"ok": True, "created": created}


# ---- static: отдаём тот же HTML ----

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"hint": "положи fleet-repair-board_file.html в server/static/"}
