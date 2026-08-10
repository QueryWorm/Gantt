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


class SubtaskRequest(BaseModel):
    """POST /api/borts/{id}/subtasks — добавить параллельную подзадачу."""
    name: str
    today_index: int


class EventRequest(BaseModel):
    """POST /api/events — трекинг действия тестера."""
    session_id: str
    type: str
    target: str = ""
    payload: dict = Field(default_factory=dict)


class EventStats(BaseModel):
    total: int
    by_type: dict[str, int]
    last_n: list[dict]
