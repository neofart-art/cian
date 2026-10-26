"""Shared family viewing records, attached to a real property, not a Cian ID."""
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

    @field_validator('preparation', 'comment')
    @classmethod
    def plain_text(cls, value):
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
            raise ValueError('Недопустимые управляющие символы в заметке')
        return value

    @field_validator('scheduled_at')
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
        return self


def context(conn):
    groups = {}
    for row in conn.execute('''SELECT v.*,u.display_name AS updated_by_name,u.username AS updated_by_username
                               FROM property_visits v JOIN users u ON u.id=v.updated_by ORDER BY v.id DESC'''):
        groups.setdefault(row['property_id'], []).append(dict(row))
    return groups


def save(conn, listing_id, data, user_id, visit_id=None):
    """Caller holds BEGIN IMMEDIATE: visit and family status commit together."""
    target = conn.execute('SELECT property_id FROM listings WHERE id=?', (listing_id,)).fetchone()
    if not target:
        raise HTTPException(404, 'Объявление не найдено')
    pid = target['property_id']
    if pid != data.property_id:
        raise HTTPException(409, 'Объекты были объединены. Обновите страницу и повторите действие')
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
    completed = ((old['completed_at'] if old and old['completed_at'] else now)
                 if data.status == 'completed' else None)
    scheduled = data.scheduled_at.isoformat() if data.scheduled_at else None
    values = (scheduled, data.status, data.outcome, data.preparation, data.comment, completed, now, user_id)
    if old:
        conn.execute('''UPDATE property_visits SET scheduled_at=?,status=?,outcome=?,preparation=?,comment=?,
                        completed_at=?,updated_at=?,updated_by=?,version=version+1 WHERE id=?''', (*values, visit_id))
    else:
        visit_id = conn.execute('''INSERT INTO property_visits(scheduled_at,status,outcome,preparation,comment,
                                  completed_at,updated_at,updated_by,property_id,created_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?)''', (*values, pid, now)).lastrowid
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
