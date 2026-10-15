import os
import re
import sqlite3
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    ROOT = Path(__file__).resolve().parent
    load_dotenv(ROOT / '.env')
except ImportError:
    ROOT = Path(__file__).resolve().parent

DATABASE_URL = os.environ.get('DATABASE_URL', os.environ.get('SUPABASE_DB_URL', '')).strip()
DB_FILE = Path(os.environ.get('SCHOOLS_DB_PATH', ROOT / 'schools.db'))

_pg_pool = None

if DATABASE_URL:
    try:
        import psycopg2
        from psycopg2 import extras, pool
        # Initialize thread-safe connection pool for Supabase Postgres
        _pg_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=20, dsn=DATABASE_URL)
        print('INFO: Connected to Supabase PostgreSQL database.')
    except Exception as e:
        print(f'WARNING: Failed to initialize PostgreSQL pool ({e}). Falling back to SQLite.')
        _pg_pool = None

class PostgresRowDict(dict):
    """Dict subclass supporting column-name access, keys(), and integer indexing."""
    def __init__(self, d, tuple_vals=None):
        super().__init__(d)
        self._tuple = tuple_vals or tuple(d.values())

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._tuple[item]
        return super().__getitem__(item)

    def keys(self):
        return super().keys()

class PostgresCursorWrapper:
    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, params=None):
        pg_sql = sql
        
        # 1. Translate SQLite '?' parameter placeholder to Postgres '%s'
        pg_sql = pg_sql.replace('?', '%s')
        
        # 2. Translate GLOB '[0-9]*' to Postgres regex
        pg_sql = re.sub(r"NOT\s+([a-zA-Z0-9_\(\)\"]+)\s+GLOB\s+'\[0-9\]\*'", r"\1 !~ '^[0-9]'", pg_sql, flags=re.IGNORECASE)
        pg_sql = re.sub(r"([a-zA-Z0-9_\(\)\"]+)\s+GLOB\s+'\[0-9\]\*'", r"\1 ~ '^[0-9]'", pg_sql, flags=re.IGNORECASE)
        
        # 3. Translate GROUP BY and HAVING clauses for Postgres ANSI compliance
        pg_sql = re.sub(r'GROUP\s+BY\s+state,\s*district', 'GROUP BY 1, 2', pg_sql, flags=re.IGNORECASE)
        pg_sql = re.sub(r'HAVING\s+cnt\s*>=', 'HAVING COUNT(*) >=', pg_sql, flags=re.IGNORECASE)
        pg_sql = re.sub(r'ORDER\s+BY\s+v\s+COLLATE\s+NOCASE', 'ORDER BY 1', pg_sql, flags=re.IGNORECASE)

        # 4. Translate sqlite_master table lookup
        if 'sqlite_master' in pg_sql:
            pg_sql = "SELECT table_name AS name FROM information_schema.tables WHERE table_schema='public'"
            params = None
            
        # 5. Translate PRAGMA table_info
        m_pragma = re.search(r'PRAGMA\s+table_info\("?([a-zA-Z0-9_]+)"?\)', pg_sql, re.IGNORECASE)
        if m_pragma:
            table_name = m_pragma.group(1)
            pg_sql = f"SELECT column_name AS name FROM information_schema.columns WHERE table_name = '{table_name}' ORDER BY ordinal_position;"
            params = None

        # 6. Translate PRAGMA index_list
        m_idx = re.search(r'PRAGMA\s+index_list\("?([a-zA-Z0-9_]+)"?\)', pg_sql, re.IGNORECASE)
        if m_idx:
            table_name = m_idx.group(1)
            pg_sql = f"SELECT indexname AS name FROM pg_indexes WHERE tablename = '{table_name}';"
            params = None

        # 7. Ignore SQLite specific pragmas
        if pg_sql.strip().upper().startswith('PRAGMA'):
            return self

        # Execute
        if params is not None:
            self._cur.execute(pg_sql, tuple(params))
        else:
            self._cur.execute(pg_sql)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return PostgresRowDict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        return [PostgresRowDict(r) for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield PostgresRowDict(r)

class PostgresConnectionWrapper:
    def __init__(self, raw_conn, pool_ref=None):
        self._conn = raw_conn
        self._pool = pool_ref

    def cursor(self):
        from psycopg2 import extras
        return PostgresCursorWrapper(self._conn.cursor(cursor_factory=extras.RealDictCursor))

    def execute(self, sql, params=None):
        c = self.cursor()
        c.execute(sql, params)
        return c

    def close(self):
        if self._pool:
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass
        else:
            self._conn.close()

def get_db_connection():
    if _pg_pool:
        raw_conn = _pg_pool.getconn()
        return PostgresConnectionWrapper(raw_conn, pool_ref=_pg_pool)
    
    # SQLite fallback
    if not DB_FILE.exists():
        fallback_db = ROOT / 'schools_optimized.db'
        target_db = fallback_db if fallback_db.exists() else DB_FILE
    else:
        target_db = DB_FILE

    c = sqlite3.connect(str(target_db), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only = ON')
    c.execute('PRAGMA busy_timeout = 30000')
    return c

def get_stream_connection():
    if _pg_pool:
        raw_conn = _pg_pool.getconn()
        return PostgresConnectionWrapper(raw_conn, pool_ref=_pg_pool)
    return get_db_connection()
