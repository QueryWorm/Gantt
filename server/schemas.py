"""
Pydantic v2 схемы. Поля соответствуют тому, что шлёт/получает HTML.
"""
from typing import Optional
from pydantic import BaseModel, Field


class Segment(BaseModel):
    id: Optional[int] = None
    kind: str
    label: str
    start: int
    days: int
    status: str
    ord: int = 0
    depends_on: list[int] = Field(default_factory=list)


class Track(BaseModel):
    id: Optional[int] = None
    name: str
    sub: bool = False
    segments: list[Segment] = Field(default_factory=list)


class LogEntry(BaseModel):
    id: Optional[int] = None
    date: str
    stage: str
    text: str
    ts: Optional[str] = None


class Bort(BaseModel):
    id: str
    desc: str = ""
    caseStart: int = 0
    tracks: list[Track] = Field(default_factory=list)
    log: list[LogEntry] = Field(default_factory=list)


class QueueItem(BaseModel):
    id: str
    reason: str


class Snapshot(BaseModel):
    """Полный снимок для GET /api/borts — то, что раньше было DATA + QUEUE."""
    DATA: list[Bort]
    QUEUE: list[QueueItem]


class MutateRequest(BaseModel):
    """POST /api/borts/{id}/mutate — закрыть активный сегмент, открыть новый."""
    track_id: int
    new_kind: str
    text: str
    today_index: int
    session_id: str = ""


class SubtaskRequest(BaseModel):
    """POST /api/borts/{id}/subtasks — добавить параллельную подзадачу."""
    name: str
    today_index: int
    session_id: str = ""


class LogEntryRequest(BaseModel):
    """POST /api/borts/{id}/log — запись в лог без смены статуса."""
    text: str
    stage: str = ""
    track_id: Optional[int] = None
    today_index: int = 0
    session_id: str = ""


class EventRequest(BaseModel):
    """POST /api/events — трекинг действия тестера."""
    session_id: str
    type: str
    target: str = ""
    payload: dict = Field(default_factory=dict)


class SegmentPatchRequest(BaseModel):
    """PATCH /api/borts/{id}/segments/{sid} — изменить kind/status/days/start/depends_on."""
    kind: Optional[str] = None
    status: Optional[str] = None
    days: Optional[int] = None
    start: Optional[int] = None
    depends_on: Optional[list[int]] = None
    session_id: str = ""


class TrackPatchRequest(BaseModel):
    """PATCH /api/borts/{id}/tracks/{tid} — переименовать."""
    name: str
    session_id: str = ""


class BortPatchRequest(BaseModel):
    """PATCH /api/borts/{id} — изменить desc."""
    desc: str
    session_id: str = ""


class BortCreateRequest(BaseModel):
    """POST /api/borts — создать ремонт."""
    id: str = ""
    desc: str = ""
    priority: int = 0
    assignee: str = ""
    case_start: int = 0
    session_id: str = ""


class EventStats(BaseModel):
    total: int
    by_type: dict[str, int]
    last_n: list[dict]
