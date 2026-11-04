"""Small list projections and durable conditional versions; no data deletion."""
import hashlib
import json
import sqlite3
import time
import uuid

LIST_FIELDS=('id','title','price','address','url','complex_name','area','floor','metro','metro_time',
             'metro_walk_minutes','source','first_seen','last_seen','last_imported','property_id',
             'review_status','review_updated_at','review_updated_by','review_version',
             'realtor_status','realtor_updated_at','realtor_updated_by','realtor_version',
             'cover_image_path','cover_thumb_path')
PROPERTIES=('id','in_our_list','selection_version','visits_version','viewing_documents_ready',
            'family_comment_version','family_comment_updated_at','family_comment_updated_by')
PREVIEW=240


def migrate(database):
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("""CREATE TABLE IF NOT EXISTS list_revision(
            id INTEGER PRIMARY KEY CHECK(id=1),epoch TEXT NOT NULL,revision INTEGER NOT NULL)""")
        conn.execute('INSERT OR IGNORE INTO list_revision VALUES (1,?,0)',(uuid.uuid4().hex,))
        conn.execute('CREATE INDEX IF NOT EXISTS property_visits_latest ON property_visits(property_id,status,completed_at DESC,id DESC)')
        # Auth/session reads do not invalidate the list. No-op collector status
        # writes do not cause downloads every time the admin checks its status.
        for table in ('listings','properties','price_history','property_visits','property_comment_history',
                      'duplicate_candidates','identity_decisions','property_state','users','runs','browser_state'):
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+table+')')]
            if not columns:
                continue
            changed=' OR '.join('NEW."'+c.replace('"','""')+'" IS NOT OLD."'+c.replace('"','""')+'"' for c in columns)
            for event in ('INSERT','UPDATE','DELETE'):
                name='xata_list_revision_'+table+'_'+event.lower()
                condition=' WHEN '+changed if event=='UPDATE' else ''
                conn.execute('CREATE TRIGGER IF NOT EXISTS '+name+' AFTER '+event+' ON '+table+condition+
                             ' BEGIN UPDATE list_revision SET revision=revision+1 WHERE id=1; END')


def version(conn):
    row=conn.execute('SELECT epoch,revision FROM list_revision WHERE id=1').fetchone()
    return row['epoch'],row['revision']


def etag(epoch,revision,view,user,full=False):
    raw=':'.join(map(str,(epoch,revision,view,user['id'],user['role'],bool(full))))
    return 'W/"'+hashlib.sha256(raw.encode()).hexdigest()+'"'


def totals(conn):
    row=conn.execute('''SELECT COUNT(*) AS total,COALESCE(SUM(in_our_list),0) AS ours
        FROM properties p JOIN listings l ON l.id=p.current_listing_id AND l.property_id=p.id
        WHERE p.merged_into IS NULL''').fetchone()
    return {'all':row['total'],'ours':row['ours']}


def summaries(conn,view,cover_sources):
    """Bounded set of SQL queries; descriptions and historical texts never read."""
    fields=','.join('l.'+name for name in LIST_FIELDS)
    prop_fields=','.join('p.'+name for name in PROPERTIES if name!='id')
    selection=' AND p.in_our_list=1' if view=='ours' else ''
    rows=conn.execute('SELECT '+fields+','+prop_fields+''',
        substr(l.realtor_comment,1,?) AS realtor_comment,
        length(l.realtor_comment)>? AS realtor_comment_truncated,
        substr(p.family_comment,1,?) AS family_comment,
        length(p.family_comment)>? AS family_comment_truncated,
        u.display_name AS review_updated_by_name,u.username AS review_updated_by_username,
        r.display_name AS realtor_updated_by_name,r.username AS realtor_updated_by_username,
        c.display_name AS family_comment_updated_by_name
        FROM properties p JOIN listings l ON l.id=p.current_listing_id AND l.property_id=p.id
        LEFT JOIN users u ON u.id=l.review_updated_by LEFT JOIN users r ON r.id=l.realtor_updated_by
        LEFT JOIN users c ON c.id=p.family_comment_updated_by
        WHERE p.merged_into IS NULL'''+selection,(PREVIEW,PREVIEW,PREVIEW,PREVIEW)).fetchall()
    items={r['property_id']:dict(r) for r in rows}
    if not items:
        return []
    # Joining the scoped properties applies selection inside SQL, before any
    # historical rows/texts are decoded. Only IDs/counts/ranges reach the list.
    scoped=' JOIN properties p ON p.id=l.property_id WHERE p.merged_into IS NULL'+selection
    for row in conn.execute('''SELECT l.property_id,COUNT(*) AS n,MIN(l.first_seen) AS first_seen,
        GROUP_CONCAT(l.id,'|') AS ids FROM listings l'''+scoped+' GROUP BY l.property_id'):
        item=items.get(row['property_id'])
        if item:
            item.update(first_seen=row['first_seen'],listing_ids=row['ids'].split('|'),relist_count=max(0,row['n']-1))
    for row in conn.execute('''SELECT h.property_id,COUNT(*) AS n,MIN(h.price) AS low,MAX(h.price) AS high
        FROM price_history h JOIN properties p ON p.id=h.property_id WHERE p.merged_into IS NULL'''+selection+' GROUP BY h.property_id'):
        item=items.get(row['property_id'])
        if item:
            item.update(price_min=row['low'],price_max=row['high'],price_history_count=row['n'])
    # Fallback to an older local photo of this property, without resolving
    # thousands of filesystem paths while constructing the JSON list.
    photos={}
    for row in conn.execute('''SELECT l.id,l.property_id,l.cover_image_path,l.cover_thumb_path
        FROM listings l'''+scoped+''' AND l.cover_image_path IS NOT NULL AND l.cover_thumb_path IS NOT NULL
        ORDER BY l.first_seen DESC,l.id DESC'''):
        photos.setdefault(row['property_id'],row)
    for row in conn.execute('''SELECT h.property_id,h.kind,COUNT(*) AS n FROM property_comment_history h
        JOIN properties p ON p.id=h.property_id WHERE p.merged_into IS NULL'''+selection+' GROUP BY h.property_id,h.kind'):
        if row['property_id'] in items:
            items[row['property_id']].setdefault('comment_history_count',{})[row['kind']]=row['n']
    # Keep every planned appointment and only the most recent completed trip.
    # The form and history request the complete visit records on opening.
    visit_scope=' AND p.in_our_list=1' if view=='ours' else ''
    for row in conn.execute('''SELECT v.property_id,COUNT(*) AS n FROM property_visits v
        JOIN properties p ON p.id=v.property_id WHERE p.merged_into IS NULL'''+visit_scope+' GROUP BY v.property_id'):
        if row['property_id'] in items:
            items[row['property_id']]['visit_count']=row['n']
    for row in conn.execute('''SELECT v.id,v.property_id,v.scheduled_at,v.status,v.outcome,v.completed_at,
        v.updated_at,v.updated_by,v.version,substr(v.comment,1,160) AS comment,length(v.comment)>160 AS comment_truncated,v.rejection_reasons,
        u.display_name AS updated_by_name,u.username AS updated_by_username
        FROM property_visits v JOIN properties p ON p.id=v.property_id JOIN users u ON u.id=v.updated_by
        WHERE p.merged_into IS NULL'''+visit_scope+''' AND
        (v.status='planned' OR (v.status='completed' AND NOT EXISTS
          (SELECT 1 FROM property_visits newer WHERE newer.property_id=v.property_id AND newer.status='completed'
           AND (newer.completed_at>v.completed_at OR (newer.completed_at=v.completed_at AND newer.id>v.id)))))
        ORDER BY v.id DESC'''):
        item=items.get(row['property_id'])
        if item:
            visit=dict(row);visit['rejection_reasons']=json.loads(visit['rejection_reasons'])
            item.setdefault('visits',[]).append(visit)
    # Exact pending duplicate proposals remain available without the full
    # historical listing data. Alias handling matches the existing root model.
    props={r['id']:dict(r) for r in conn.execute('SELECT id,merged_into,current_listing_id FROM properties')}
    listing_props={r['id']:r['property_id'] for r in conn.execute('SELECT id,property_id FROM listings')}
    def root(pid):
        seen=set()
        while pid in props and props[pid]['merged_into'] is not None and pid not in seen:
            seen.add(pid);pid=props[pid]['merged_into']
        return pid
    negative={tuple(sorted((listing_props.get(r['listing_a'],0),listing_props.get(r['listing_b'],0))))
              for r in conn.execute("SELECT listing_a,listing_b FROM identity_decisions WHERE decision='different'")}
    urls={r['id']:r['url'] for r in conn.execute('SELECT id,url FROM listings')}
    for row in conn.execute("SELECT id,listing_id,candidate_property_id,confidence FROM duplicate_candidates WHERE decision='pending' ORDER BY confidence DESC,id"):
        source=listing_props.get(row['listing_id']);target=root(row['candidate_property_id'])
        item=items.get(source)
        current=props.get(target,{}).get('current_listing_id')
        if not item or source==target or not current or tuple(sorted((source,target))) in negative or item.get('duplicate_candidate'):
            continue
        item['duplicate_candidate']={'id':row['id'],'listing_id':row['listing_id'],'confidence':row['confidence'],
            'source_property_id':source,'candidate_property_id':target,'candidate_listing_id':current,'candidate_url':urls.get(current)}
    from app import viewings
    from app.finance import mortgage_payment
    current_photos={r['property_id']:r for r in rows}
    for pid,item in items.items():
        photo=current_photos[pid]
        if not item['cover_image_path'] or not item['cover_thumb_path']:
            photo=photos.get(pid,photo)
        item.update(cover_sources(photo))
        for field in ('cover_image_path','cover_thumb_path'):
            item.pop(field,None)
        item.update(in_our_list=bool(item['in_our_list']),viewing_documents_ready=bool(item['viewing_documents_ready']),
                    mortgage_payment=mortgage_payment(item['price']),details_loaded=False)
        item.setdefault('visits',[])
        item['viewed_group']=viewings.classification(item['visits'],item['viewing_documents_ready'])
        item.setdefault('visit_count',0)
        item.setdefault('price_history_count',0)
        item.setdefault('comment_history_count',{})
        item.setdefault('duplicate_candidate',None)
    return sorted(items.values(),key=lambda i:i['first_seen'] or '',reverse=True)


class TimedCursor(sqlite3.Cursor):
    def execute(self,*args,**kwargs):
        begin=time.perf_counter()
        try:return super().execute(*args,**kwargs)
        finally:self.connection.sql_seconds+=time.perf_counter()-begin;self.connection.query_count+=1

    def fetchall(self):
        begin=time.perf_counter()
        try:return super().fetchall()
        finally:self.connection.sql_seconds+=time.perf_counter()-begin

    def fetchone(self):
        begin=time.perf_counter()
        try:return super().fetchone()
        finally:self.connection.sql_seconds+=time.perf_counter()-begin

    def __next__(self):
        begin=time.perf_counter()
        try:return super().__next__()
        finally:self.connection.sql_seconds+=time.perf_counter()-begin


class TimedConnection(sqlite3.Connection):
    sql_seconds=0.
    query_count=0
    def cursor(self,*args,**kwargs):
        kwargs.setdefault('factory',TimedCursor)
        return super().cursor(*args,**kwargs)
    def execute(self,*args,**kwargs):
        return self.cursor().execute(*args,**kwargs)
