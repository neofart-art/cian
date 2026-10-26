"""Shared family viewing records, attached to a real property, not a Cian ID."""
import re
import hashlib
import json
from datetime import date as CalendarDate, datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REJECTION_REASONS = {
    'noise':'Шум','layout':'Планировка','condition':'Состояние квартиры',
    'building':'Дом / подъезд','location':'Расположение','documents':'Документы','other':'Другое',
}
Reason = Literal['noise','layout','condition','building','location','documents','other']
REQUEST_ID_PATTERN = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'


def migrate(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(properties)')}
    if 'visits_version' not in columns:
        conn.execute('ALTER TABLE properties ADD COLUMN visits_version INTEGER NOT NULL DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS property_visits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        property_id INTEGER NOT NULL REFERENCES properties(id),
        scheduled_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('planned','completed','cancelled')),
        outcome TEXT CHECK(outcome IN ('liked','rejected','thinking')),
        preparation TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        completed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL REFERENCES users(id),
        version INTEGER NOT NULL DEFAULT 1,
        CHECK((status='completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)
           OR (status IN ('planned','cancelled') AND outcome IS NULL AND completed_at IS NULL)),
        CHECK(status!='planned' OR scheduled_at IS NOT NULL)
    )''')
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(property_visits)')}
    for name,definition in {
        'rejection_reasons':"TEXT NOT NULL DEFAULT '[]'",
        'request_id':'TEXT',
        'request_hash':'TEXT',
    }.items():
        if name not in columns:
            conn.execute('ALTER TABLE property_visits ADD COLUMN '+name+' '+definition)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS property_visits_request ON property_visits(request_id) WHERE request_id IS NOT NULL')
    conn.execute('CREATE INDEX IF NOT EXISTS property_visits_property ON property_visits(property_id,id)')


class VisitInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    property_id: int = Field(ge=1)
    version: int = Field(default=0, ge=0)
    scheduled_at: datetime | None = None
    status: Literal['planned','completed','cancelled']
    outcome: Literal['liked','rejected','thinking'] | None = None
    preparation: str = Field(default='', max_length=2000)
    comment: str = Field(default='', max_length=2000)
    completed_at: datetime | None = None
    rejection_reasons: list[Reason] | None = Field(default=None,max_length=7)
    request_id: str | None = Field(default=None,pattern=REQUEST_ID_PATTERN)

    @field_validator('preparation', 'comment')
    @classmethod
    def plain_text(cls, value):
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
            raise ValueError('Недопустимые управляющие символы в заметке')
        return value

    @field_validator('scheduled_at','completed_at')
    @classmethod
    def aware_time(cls, value):
        if value is not None:
            if value.utcoffset() is None or not 2000 <= value.year <= 2100:
                raise ValueError('Укажите дату с часовым поясом, от 2000 до 2100 года')
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode='after')
    def valid_state(self):
        if self.status == 'planned' and self.scheduled_at is None:
            raise ValueError('Для запланированного просмотра нужны дата и время')
        if (self.status == 'completed') != (self.outcome is not None):
            raise ValueError('Результат обязателен только для состоявшегося просмотра')
        if self.status != 'completed' and self.completed_at is not None:
            raise ValueError('Фактическая дата доступна только для состоявшегося просмотра')
        if self.rejection_reasons:
            if self.status != 'completed' or self.outcome != 'rejected':
                raise ValueError('Причины отказа доступны только для результата «Не понравилось»')
            if len(self.rejection_reasons) != len(set(self.rejection_reasons)):
                raise ValueError('Причины отказа не должны повторяться')
        return self


def request_fingerprint(data):
    return hashlib.sha256(json.dumps(data.model_dump(mode='json',exclude={'request_id','version'}),
                                    sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class VisitDateInput(BaseModel):
    """A date-only operation; never accepts outcomes, notes or review fields."""
    model_config = ConfigDict(extra='forbid')
    property_id: int = Field(ge=1)
    visits_version: int = Field(ge=0)
    visit_id: int | None = Field(default=None,ge=1)
    date: datetime | CalendarDate | None
    request_id: str | None = Field(default=None,pattern=REQUEST_ID_PATTERN)

    @field_validator('date',mode='before')
    @classmethod
    def calendar_input(cls,value):
        if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            return CalendarDate.fromisoformat(value)
        return value

    @field_validator('date')
    @classmethod
    def aware_time(cls,value):
        if isinstance(value,CalendarDate) and not isinstance(value,datetime):
            if not 2000 <= value.year <= 2100:
                raise ValueError('Укажите дату от 2000 до 2100 года')
            return value
        return VisitInput.aware_time(value)

    @model_validator(mode='after')
    def creation_key(self):
        if self.visit_id is None and self.date is not None and not self.request_id:
            raise ValueError('Для назначения первой даты нужен ключ повторного запроса')
        return self


def context(conn):
    groups = {}
    for row in conn.execute('''SELECT v.*,u.display_name AS updated_by_name,u.username AS updated_by_username
                               FROM property_visits v JOIN users u ON u.id=v.updated_by ORDER BY v.id DESC'''):
        # Preserve unknown columns in SQLite without publishing them or
        # requiring them to be JSON-serializable (legacy BLOBs, for example).
        fields = ('id','property_id','scheduled_at','status','outcome','preparation','comment',
                  'completed_at','created_at','updated_at','updated_by','version',
                  'updated_by_name','updated_by_username','rejection_reasons')
        item = {field:row[field] for field in fields}
        item['rejection_reasons'] = json.loads(item['rejection_reasons'] or '[]')
        groups.setdefault(row['property_id'], []).append(item)
    return groups


def save(conn, listing_id, data, user_id, visit_id=None):
    """Caller holds BEGIN IMMEDIATE: visit and family status commit together."""
    target = conn.execute('SELECT property_id FROM listings WHERE id=?', (listing_id,)).fetchone()
    if not target:
        raise HTTPException(404, 'Объявление не найдено')
    pid = target['property_id']
    if pid != data.property_id:
        raise HTTPException(409, 'Объекты были объединены. Обновите страницу и повторите действие')
    fingerprint = request_fingerprint(data)
    if visit_id is not None and data.request_id is not None:
        raise HTTPException(422,'Ключ повторного запроса используется только при создании')
    if visit_id is None and data.request_id:
        replay = conn.execute('SELECT property_id,request_hash FROM property_visits WHERE request_id=?',(data.request_id,)).fetchone()
        if replay:
            if replay['property_id'] != pid or replay['request_hash'] != fingerprint:
                raise HTTPException(409,'Этот запрос уже был сохранён с другими данными. Закройте окно и откройте сохранённый просмотр')
            return pid
    old = None
    if visit_id is not None:
        old = conn.execute('SELECT * FROM property_visits WHERE id=? AND property_id=?', (visit_id, pid)).fetchone()
        if old is None:
            raise HTTPException(404, 'Просмотр не найден')
        if data.version != old['version']:
            raise HTTPException(409, 'Просмотр уже изменён другим пользователем. Закройте окно и откройте заново')
        # A completed viewing is history. A repeat needs a separate record.
        if old['status'] == 'completed' and data.status != 'completed':
            raise HTTPException(422, 'Состоявшийся просмотр нельзя отменить или перенести. Назначьте повторный')
    elif data.version:
        raise HTTPException(409, 'Новый просмотр должен иметь версию 0')
    now = datetime.now(timezone.utc).isoformat()
    completed = ((data.completed_at.isoformat() if data.completed_at else
                  old['completed_at'] if old and old['completed_at'] else now)
                 if data.status == 'completed' else None)
    reasons = data.rejection_reasons
    if reasons is None:
        reasons = (json.loads(old['rejection_reasons'] or '[]')
                   if old and old['outcome']=='rejected' and data.outcome=='rejected' else [])
    encoded_reasons = json.dumps(reasons,ensure_ascii=False)
    scheduled = data.scheduled_at.isoformat() if data.scheduled_at else None
    values = (scheduled, data.status, data.outcome, data.preparation, data.comment, completed, now, user_id)
    if old:
        conn.execute('''UPDATE property_visits SET scheduled_at=?,status=?,outcome=?,preparation=?,comment=?,
                        completed_at=?,updated_at=?,updated_by=?,rejection_reasons=?,version=version+1 WHERE id=?''', (*values, encoded_reasons, visit_id))
    else:
        visit_id = conn.execute('''INSERT INTO property_visits(scheduled_at,status,outcome,preparation,comment,
                                  completed_at,updated_at,updated_by,property_id,created_at,rejection_reasons,request_id,request_hash)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (*values, pid, now, encoded_reasons, data.request_id, fingerprint)).lastrowid
    conn.execute('UPDATE properties SET visits_version=visits_version+1 WHERE id=?', (pid,))
    # Saving a note, scheduling, rescheduling or cancelling must not undo a
    # later independent family decision. Nor may an older viewing override a newer one.
    if data.status == 'completed' and (old is None or old['outcome'] != data.outcome):
        latest = conn.execute("SELECT id FROM property_visits WHERE property_id=? AND status='completed' ORDER BY completed_at DESC,id DESC LIMIT 1", (pid,)).fetchone()
        if latest['id'] == visit_id:
            status = {'liked':'liked','rejected':'rejected','thinking':'viewed'}[data.outcome]
            version = conn.execute('SELECT MAX(review_version)+1 FROM listings WHERE property_id=?', (pid,)).fetchone()[0]
            conn.execute('''UPDATE listings SET review_status=?,review_updated_at=?,review_updated_by=?,review_version=?
                            WHERE property_id=?''', (status, now, user_id, version, pid))
    return pid

def calendar_timestamp(value,previous=None):
    """Keep timestamp API/storage, interpreting a calendar day only in Moscow.

    Existing wall-clock time (including microseconds) survives a day change.
    A new calendar-only appointment uses Moscow midnight as its storage anchor.
    """
    if value is None or isinstance(value,datetime):
        return value
    zone = ZoneInfo('Europe/Moscow')
    field = ('completed_at' if previous['status']=='completed' else 'scheduled_at') if previous is not None else None
    if previous is not None and previous[field]:
        original = datetime.fromisoformat(previous[field]).astimezone(zone)
        result = original.replace(year=value.year,month=value.month,day=value.day)
    else:
        result = datetime.combine(value,time.min,tzinfo=zone)
    return result.astimezone(timezone.utc)


def save_date(conn, listing_id, data, user_id):
    """Caller owns BEGIN IMMEDIATE and enforces admin/realtor authorization."""
    target = conn.execute('SELECT property_id FROM listings WHERE id=?',(listing_id,)).fetchone()
    if target is None:
        raise HTTPException(404,'Объявление не найдено')
    pid = target['property_id']
    if pid != data.property_id:
        raise HTTPException(409,'Объект изменился. Обновите страницу перед редактированием даты')
    planned = None
    if data.visit_id is None and data.date is not None:
        stamp = calendar_timestamp(data.date)
        if not isinstance(data.date,datetime):
            # Validate the calendar year in Moscow before UTC crosses into
            # the previous day/year at the lower supported boundary.
            stamp = stamp.astimezone(ZoneInfo('Europe/Moscow'))
        planned = VisitInput(property_id=pid,status='planned',scheduled_at=stamp,request_id=data.request_id)
        replay = conn.execute('SELECT property_id,request_hash FROM property_visits WHERE request_id=?',(data.request_id,)).fetchone()
        if replay:
            if replay['property_id'] != pid or replay['request_hash'] != request_fingerprint(planned):
                raise HTTPException(409,'Этот запрос уже сохранён с другой датой. Обновите страницу')
            return pid
    # An identical retry acknowledges the old operation without another write,
    # even if its date no longer wins the nearest-appointment selection.
    previous = conn.execute('SELECT * FROM property_visits WHERE id=? AND property_id=?',
                            (data.visit_id,pid)).fetchone() if data.visit_id else None
    normalized = calendar_timestamp(data.date,previous)
    value = normalized.isoformat() if normalized else None
    if previous:
        field = 'completed_at' if previous['status']=='completed' else 'scheduled_at'
        if ((value is not None and previous['status'] in ('planned','completed') and previous[field]==value)
                or (value is None and previous['status']=='cancelled')):
            return pid
    revision = conn.execute('SELECT visits_version FROM properties WHERE id=?',(pid,)).fetchone()[0]
    if revision != data.visits_version:
        raise HTTPException(409,'Дата или просмотр уже изменены. Обновите страницу и повторите действие')
    shown = conn.execute("""SELECT * FROM property_visits WHERE property_id=? AND status='planned'
                            ORDER BY scheduled_at,id LIMIT 1""",(pid,)).fetchone()
    if shown is None:
        shown = conn.execute("""SELECT * FROM property_visits WHERE property_id=? AND status='completed'
                                ORDER BY completed_at DESC,id DESC LIMIT 1""",(pid,)).fetchone()
    if (shown['id'] if shown else None) != data.visit_id:
        raise HTTPException(409,'В колонке уже другой просмотр. Обновите страницу перед изменением даты')
    if shown is None:
        return save(conn,listing_id,planned,user_id) if planned else pid
    if value is None and shown['status']=='completed':
        raise HTTPException(422,'Нельзя удалить дату состоявшегося просмотра')
    now = datetime.now(timezone.utc).isoformat()
    if value is None:
        # Preserve the previous scheduled date in the cancelled history entry.
        conn.execute("""UPDATE property_visits SET status='cancelled',updated_at=?,updated_by=?,version=version+1
                        WHERE id=?""",(now,user_id,shown['id']))
    else:
        field = 'scheduled_at' if shown['status']=='planned' else 'completed_at'
        conn.execute('UPDATE property_visits SET '+field+'=?,updated_at=?,updated_by=?,version=version+1 WHERE id=?',
                     (value,now,user_id,shown['id']))
    conn.execute('UPDATE properties SET visits_version=visits_version+1 WHERE id=?',(pid,))
    return pid
