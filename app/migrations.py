"""Transactional upgrades of production CHECK constraints, preserving all columns."""
import re
from datetime import datetime, timezone

from app.csv_source import parse_card_text, is_publication_time
from app.property_match import create_property, record_price, root_property, save_decision, bump_identity
from app.viewings import migrate as migrate_viewings


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def expand_check(conn, table, column, values):
    """SQLite's table-rebuild procedure, with FK enforcement suspended.

    Keep original DDL (including unknown columns/defaults/constraints), every
    writable column, all explicit indexes and triggers. Never use writable_schema.
    The caller owns the transaction and verifies foreign_key_check before commit.
    """
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    pattern = re.compile(r"CHECK\s*\(\s*" + column + r"\s+IN\s*\(([^)]+)\)\s*\)", re.I)
    match = pattern.search(ddl)
    if not match:
        raise RuntimeError('Expected production CHECK constraint missing: ' + table + '.' + column)
    current = re.findall(r"'([^']*)'", match[1])
    if all(value in current for value in values):
        return False
    allowed = list(dict.fromkeys([*current, *values]))
    ddl = pattern.sub("CHECK(" + column + " IN (" + ",".join("'" + v + "'" for v in allowed) + "))", ddl, count=1)
    temporary = table + '_phase2_upgrade'
    create = re.sub(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"[^"]+"|\x60[^\x60]+\x60|\[[^\]]+\]|\w+)',
                    'CREATE TABLE ' + quote(temporary), ddl, count=1, flags=re.I)
    objects = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
    columns = ','.join(quote(row['name']) for row in conn.execute('PRAGMA table_xinfo(' + quote(table) + ')') if row['hidden'] == 0)
    conn.execute(create)
    conn.execute('INSERT INTO ' + quote(temporary) + '(' + columns + ') SELECT ' + columns + ' FROM ' + quote(table))
    conn.execute('DROP TABLE ' + quote(table))
    conn.execute('ALTER TABLE ' + quote(temporary) + ' RENAME TO ' + quote(table))
    for sql in objects:
        conn.execute(sql)
    return True


def transaction(database, action):
    with database() as conn:
        # PRAGMA foreign_keys must change outside a transaction when rebuilding
        # users referenced by sessions/listings.
        conn.execute('PRAGMA foreign_keys=OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
            action(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('Foreign key check failed during Phase 2 migration')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA foreign_keys=ON')


def migrate_user_roles(database):
    def upgrade(conn):
        expand_check(conn, 'users', 'role', ['admin', 'reviewer', 'realtor'])
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
        if 'password_length' not in columns:
            # Unknown for legacy hashes: logins stay valid; promotion from an
            # unknown legacy password requires a fresh admin password.
            conn.execute('ALTER TABLE users ADD COLUMN password_length INTEGER')
    transaction(database, upgrade)


def migrate_listings(database):
    def upgrade(conn):
        expand_check(conn, 'listings', 'review_status', ['new', 'liked', 'viewed', 'rejected'])
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(listings)')}
        for name, definition in {
            'realtor_status': "TEXT NOT NULL DEFAULT 'pending' CHECK(realtor_status IN ('pending','approved','rejected'))",
            'realtor_comment': "TEXT NOT NULL DEFAULT ''",
            'realtor_updated_at': 'TEXT',
            'realtor_updated_by': 'INTEGER REFERENCES users(id)',
            'realtor_version': 'INTEGER NOT NULL DEFAULT 0',
            'cover_image_url': 'TEXT',
            'cover_image_path': 'TEXT',
            'cover_thumb_path': 'TEXT',
            'cover_image_cached_url': 'TEXT',
        }.items():
            if name not in columns:
                conn.execute('ALTER TABLE listings ADD COLUMN ' + name + ' ' + definition)
        conn.execute("""CREATE TABLE IF NOT EXISTS properties(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            canonical_address TEXT,
            canonical_area REAL,
            canonical_floor TEXT,
            canonical_complex_name TEXT
        )""")
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(listings)')}
        if 'property_id' not in columns:
            conn.execute('ALTER TABLE listings ADD COLUMN property_id INTEGER REFERENCES properties(id)')
        conn.execute("""CREATE TABLE IF NOT EXISTS duplicate_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            candidate_property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
            reasons TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','confirmed','rejected')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by INTEGER REFERENCES users(id),
            UNIQUE(listing_id,candidate_property_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS price_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            price INTEGER NOT NULL
        )""")
        conn.execute('CREATE INDEX IF NOT EXISTS listings_property_idx ON listings(property_id,last_seen)')
        conn.execute('CREATE INDEX IF NOT EXISTS duplicate_candidates_listing_idx ON duplicate_candidates(listing_id,decision)')
        conn.execute('CREATE INDEX IF NOT EXISTS price_history_property_idx ON price_history(property_id,id)')

        property_columns = {row['name'] for row in conn.execute('PRAGMA table_info(properties)')}
        for name, kind in {'current_listing_id':'TEXT REFERENCES listings(id)',
                           'merged_into':'INTEGER REFERENCES properties(id)'}.items():
            if name not in property_columns:
                conn.execute('ALTER TABLE properties ADD COLUMN ' + name + ' ' + kind)
        conn.execute('CREATE TABLE IF NOT EXISTS property_state(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO property_state VALUES (1,0)')
        conn.execute('''CREATE TABLE IF NOT EXISTS listing_price_state(
            listing_id TEXT PRIMARY KEY REFERENCES listings(id),price INTEGER NOT NULL,observed_at TEXT NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS identity_decisions(
            id INTEGER PRIMARY KEY,listing_a TEXT NOT NULL REFERENCES listings(id),listing_b TEXT NOT NULL REFERENCES listings(id),
            decision TEXT NOT NULL CHECK(decision IN ('same','different')),decided_at TEXT NOT NULL,
            decided_by INTEGER REFERENCES users(id))''')
        conn.execute('CREATE INDEX IF NOT EXISTS identity_decisions_pair ON identity_decisions(listing_a,listing_b,decision)')
        conn.execute('''CREATE TABLE IF NOT EXISTS property_merge_audit(
            id INTEGER PRIMARY KEY,source_property_id INTEGER NOT NULL REFERENCES properties(id),
            target_property_id INTEGER NOT NULL REFERENCES properties(id),listing_id TEXT NOT NULL REFERENCES listings(id),
            merged_at TEXT NOT NULL,review_snapshot TEXT NOT NULL)''')

        now = datetime.now(timezone.utc).isoformat()
        migrate_viewings(conn)
        for row in conn.execute('SELECT * FROM listings WHERE property_id IS NULL ORDER BY COALESCE(first_seen,last_seen),id').fetchall():
            property_id = create_property(conn, dict(row), row['first_seen'] or row['last_seen'] or now)
            conn.execute('UPDATE listings SET property_id=? WHERE id=?', (property_id, row['id']))

        conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
        # Existing reviewed rows must not be matched destructively at startup.
        # Give each legacy listing its own property; only future, unambiguous
        # relistings can auto-join. Already-confirmed Phase 3 groups stay intact.
        conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?)', ('phase3_property_backfill', now))
        for prop in conn.execute('SELECT id FROM properties WHERE current_listing_id IS NULL AND merged_into IS NULL').fetchall():
            row = conn.execute('SELECT id FROM listings WHERE property_id=? ORDER BY first_seen DESC,rowid DESC LIMIT 1',
                               (prop['id'],)).fetchone()
            if row:
                conn.execute('UPDATE properties SET current_listing_id=? WHERE id=?',(row['id'],prop['id']))

        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name='phase3_price_seed'").fetchone():
            for row in conn.execute(
                'SELECT id,property_id,first_seen,last_seen,price FROM listings WHERE property_id IS NOT NULL AND price IS NOT NULL ORDER BY COALESCE(first_seen,last_seen),id'
            ).fetchall():
                record_price(conn, row['property_id'], row['id'], row['last_seen'] or row['first_seen'] or now, row['price'])
            conn.execute('INSERT INTO schema_migrations VALUES (?,?)',
                         ('phase3_price_seed', now))
        # Populate the per-listing observation baseline without replaying prices
        # as new events on every refresh or restart.
        conn.execute('''INSERT OR IGNORE INTO listing_price_state(listing_id,price,observed_at)
                        SELECT id,price,COALESCE(last_seen,first_seen,?) FROM listings WHERE price IS NOT NULL''', (now,))
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name='phase3_safe_identity_v1'").fetchone():
            # Preserve old manual overrides using stable Cian IDs. Property IDs
            # may subsequently become aliases; no decision is deleted.
            for decision in conn.execute("SELECT * FROM duplicate_candidates WHERE decision IN ('confirmed','rejected')").fetchall():
                target = root_property(conn, decision['candidate_property_id'])
                anchor = conn.execute('SELECT id FROM listings WHERE property_id=? ORDER BY first_seen DESC,rowid DESC LIMIT 1',
                                      (target,)).fetchone()
                if anchor:
                    save_decision(conn, decision['listing_id'], anchor['id'],
                                  'same' if decision['decision']=='confirmed' else 'different',
                                  decision['decided_at'] or now, decision['decided_by'])
            bump_identity(conn)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?)',('phase3_safe_identity_v1',now))
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name='phase2_metro_cleanup'").fetchone():
            for row in conn.execute('SELECT id,metro_time,card_text FROM listings').fetchall():
                if is_publication_time(row['metro_time']):
                    parsed = parse_card_text(row['card_text'] or '')
                    conn.execute('UPDATE listings SET metro_time=?,metro_walk_minutes=? WHERE id=?',
                                 (parsed.get('metro_time'), parsed.get('metro_walk_minutes'), row['id']))
            conn.execute('INSERT INTO schema_migrations VALUES (?,?)',
                         ('phase2_metro_cleanup', datetime.now(timezone.utc).isoformat()))
        conn.execute('DROP INDEX IF EXISTS listings_review_order')
        conn.execute("""CREATE INDEX listings_review_order ON listings(
            CASE review_status WHEN 'new' THEN 0 WHEN 'liked' THEN 1 WHEN 'viewed' THEN 2 ELSE 3 END,first_seen DESC,id)""")
    transaction(database, upgrade)
