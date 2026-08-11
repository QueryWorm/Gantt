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
    dept: str = ""
    assignee: str = ""
    zero_day: int = 0
    starts_with: list[int] = Field(default_factory=list)
    tpl_start: Optional[int] = None
    tpl_days: Optional[int] = None


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
    priority: int = 0


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


class TemplateTrack(BaseModel):
    id: Optional[int] = None
    name: str
    sub: bool = False
    segments: list["TemplateSegment"] = Field(default_factory=list)


class TemplateSegment(BaseModel):
    id: Optional[int] = None
    kind: str
    label: str
    days: int = 0
    dept: str = ""
    assignee: str = ""
    start: int = 0
    status: str = "planned"
    depends_on: list[int] = Field(default_factory=list)


class TemplateRequest(BaseModel):
    """POST /api/templates — создать шаблон."""
    id: str
    name: str
    session_id: str = ""


class TemplateApplyRequest(BaseModel):
    """POST /api/borts/{id}/apply_template — применить шаблон к борту."""
    template_id: str
    today_index: int = 0
    session_id: str = ""


class TemplatePatchRequest(BaseModel):
    """PATCH /api/templates/{id} — переименовать."""
    name: Optional[str] = None
    session_id: str = ""


class TemplateTrackRequest(BaseModel):
    """POST /api/templates/{id}/tracks — добавить трек."""
    name: str
    sub: bool = False
    session_id: str = ""


class TemplateTrackPatchRequest(BaseModel):
    """PATCH /api/templates/{id}/tracks/{tid} — переименовать."""
    name: str
    session_id: str = ""


class TemplateSegmentRequest(BaseModel):
    """POST /api/templates/{id}/tracks/{tid}/segments — добавить сегмент."""
    kind: str
    label: str
    days: int = 0
    dept: str = ""
    assignee: str = ""
    start: int = -1
    session_id: str = ""


class TemplateSegmentPatchRequest(BaseModel):
    """PATCH /api/templates/{id}/tracks/{tid}/segments/{sid} — изменить сегмент."""
    kind: Optional[str] = None
    label: Optional[str] = None
    days: Optional[int] = None
    dept: Optional[str] = None
    assignee: Optional[str] = None
    depends_on: Optional[list[int]] = None
    start: Optional[int] = None
    session_id: str = ""


class EventRequest(BaseModel):
    """POST /api/events — трекинг действия тестера."""
    session_id: str
    type: str
    target: str = ""
    payload: dict = Field(default_factory=dict)


class SegmentPatchRequest(BaseModel):
    """PATCH /api/borts/{id}/segments/{sid} — изменить kind/status/days/start/depends_on/dept/assignee."""
    kind: Optional[str] = None
    status: Optional[str] = None
    days: Optional[int] = None
    start: Optional[int] = None
    depends_on: Optional[list[int]] = None
    dept: Optional[str] = None
    assignee: Optional[str] = None
    zero_day: Optional[int] = None
    starts_with: Optional[list[int]] = None
    today_index: int = 0
    session_id: str = ""


class TrackPatchRequest(BaseModel):
    """PATCH /api/borts/{id}/tracks/{tid} — переименовать."""
    name: str
    session_id: str = ""


class TrackSegmentRequest(BaseModel):
    """POST /api/borts/{id}/tracks/{tid}/segments — добавить сегмент в трек борта."""
    kind: str = "work"
    label: str
    days: int = 0
    start: Optional[int] = None  # позиция клика (день) — вставка перед сегментом с start >= day
    today_index: int = 0
    session_id: str = ""


class BortPatchRequest(BaseModel):
    """PATCH /api/borts/{id} — изменить desc."""
    desc: Optional[str] = None
    session_id: str = ""


class BortCreateRequest(BaseModel):
    """POST /api/borts — создать ремонт."""
    id: str = ""
    desc: str = ""
    priority: int = 0
    case_start: int = 0
    session_id: str = ""


class EventStats(BaseModel):
    total: int
    by_type: dict[str, int]
    last_n: list[dict]


class TemplateSummary(BaseModel):
    id: str
    name: str
    tracks_count: int
    segments_count: int


class Template(BaseModel):
    id: str
    name: str
    tracks: list[TemplateTrack]
