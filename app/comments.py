"""Property-owned comments, optimistic writes and lossless merge provenance."""
import re
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FamilyCommentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    property_id: int = Field(ge=1)
    family_comment_version: int = Field(ge=0)
    text: str = Field(max_length=20000)

    @field_validator('text')
    @classmethod
    def plain(cls, value):
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
            raise ValueError('Недопустимые управляющие символы')
        return value


def migrate(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(properties)')}
    for name, kind in {
        'family_comment': "TEXT NOT NULL DEFAULT ''",
        'family_comment_version': 'INTEGER NOT NULL DEFAULT 0',
        'family_comment_updated_at': 'TEXT',
        'family_comment_updated_by': 'INTEGER REFERENCES users(id)',
    }.items():
        if name not in columns:
            conn.execute('ALTER TABLE properties ADD COLUMN '+name+' '+kind)
    conn.execute("""CREATE TABLE IF NOT EXISTS property_comment_history(
        id INTEGER PRIMARY KEY,
        property_id INTEGER NOT NULL REFERENCES properties(id),
        origin_property_id INTEGER NOT NULL REFERENCES properties(id),
        origin_listing_id TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('family','realtor')),
        text TEXT NOT NULL,
        status TEXT,
        changed_at TEXT NOT NULL,
        changed_by INTEGER REFERENCES users(id),
        version INTEGER NOT NULL
    )""")
    conn.execute('CREATE INDEX IF NOT EXISTS property_comment_history_owner ON property_comment_history(property_id,id)')
    conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    marker='property_comments_history_v1'
    if not conn.execute('SELECT 1 FROM schema_migrations WHERE name=?',(marker,)).fetchone():
        # Seed only existing realtor reviews; do not copy old trip notes.
        seen=set()
        for row in conn.execute("SELECT * FROM listings WHERE realtor_version>0 OR realtor_comment<>'' ORDER BY realtor_updated_at,id"):
            signature=(row['property_id'],row['realtor_version'],row['realtor_comment'],
                       row['realtor_status'],row['realtor_updated_at'],row['realtor_updated_by'])
            if signature in seen or row['property_id'] is None:
                continue
            seen.add(signature)
            record(conn,row['property_id'],'realtor',row['realtor_comment'],row['realtor_updated_at'] or row['last_seen'] or '',
                   row['realtor_updated_by'],row['realtor_version'],row['id'],row['realtor_status'])
        conn.execute("INSERT INTO schema_migrations VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",(marker,))


def record(conn,pid,kind,text,now,user,version,listing_id,status=None):
    conn.execute("""INSERT INTO property_comment_history
        (property_id,origin_property_id,origin_listing_id,kind,text,status,changed_at,changed_by,version)
        VALUES (?,?,?,?,?,?,?,?,?)""",(pid,pid,listing_id,kind,text,status,now,user,version))


def context(conn):
    users={r['id']:dict(r) for r in conn.execute('SELECT id,display_name,username FROM users')}
    histories={}
    for row in conn.execute('SELECT * FROM property_comment_history ORDER BY id DESC'):
        item=dict(row)
        author=users.get(item['changed_by'],{})
        item['changed_by_name']=author.get('display_name') or author.get('username')
        histories.setdefault(item['property_id'],[]).append(item)
    return users,histories


def data(prop,context):
    users,histories=context
    author=users.get(prop['family_comment_updated_by'],{})
    return {**{k:prop[k] for k in ('family_comment','family_comment_version','family_comment_updated_at','family_comment_updated_by')},
            'family_comment_updated_by_name':author.get('display_name') or author.get('username'),
            'comment_history':histories.get(prop['id'],[])}


def save(conn,listing_id,payload,user_id,now):
    row=conn.execute('SELECT property_id FROM listings WHERE id=?',(listing_id,)).fetchone()
    if row is None:
        raise HTTPException(404,'Объявление не найдено')
    pid=row['property_id']
    prop=conn.execute('SELECT * FROM properties WHERE id=?',(pid,)).fetchone()
    if pid!=payload.property_id or prop['family_comment_version']!=payload.family_comment_version:
        raise HTTPException(409,'Наш комментарий уже изменён. Черновик сохранён; сверьте его с актуальным текстом.')
    # A removed apartment keeps its comment, but new edits belong to the shortlist.
    if not prop['in_our_list']:
        raise HTTPException(409,'Квартира убрана из нашего списка. Черновик сохранён; сначала верните квартиру в подборку.')
    if prop['family_comment']==payload.text:
        return pid
    version=prop['family_comment_version']+1
    conn.execute("""UPDATE properties SET family_comment=?,family_comment_version=?,
        family_comment_updated_at=?,family_comment_updated_by=? WHERE id=?""",
        (payload.text,version,now,user_id,pid))
    record(conn,pid,'family',payload.text,now,user_id,version,listing_id)
    return pid


def merge(conn,source,target,now):
    a=conn.execute('SELECT * FROM properties WHERE id=?',(source,)).fetchone()
    b=conn.execute('SELECT * FROM properties WHERE id=?',(target,)).fetchone()
    # History keeps both original property and publication IDs after any merges.
    conn.execute('UPDATE property_comment_history SET property_id=? WHERE property_id=?',(target,source))
    left,right=b['family_comment'],a['family_comment']
    text=left or right
    if left and right:
        def labelled(prop,value):
            return 'Квартира ЦИАН '+str(prop['current_listing_id'] or '—')+' (property '+str(prop['id'])+'):\n'+value
        text=labelled(b,left)+'\n\n'+labelled(a,right)
    version=max(a['family_comment_version'],b['family_comment_version'])+1
    owner=b if left else a
    conn.execute("""UPDATE properties SET family_comment=?,family_comment_version=?,
        family_comment_updated_at=?,family_comment_updated_by=? WHERE id=?""",
        (text,version,now if left and right else owner['family_comment_updated_at'],
         None if left and right else owner['family_comment_updated_by'],target))
    if left and right:
        record(conn,target,'family',text,now,None,version,b['current_listing_id'])
