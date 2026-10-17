"""Transactional upgrades of production CHECK constraints, preserving all columns."""
import re
from datetime import datetime, timezone

from app.csv_source import parse_card_text, is_publication_time


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
        }.items():
            if name not in columns:
                conn.execute('ALTER TABLE listings ADD COLUMN ' + name + ' ' + definition)
        conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
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
