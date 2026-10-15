import os
import re
import sqlite3
import sys
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

def normalize_database_url(url: str) -> str:
    """
    Ensure Supabase connection strings work across IPv4-only environments (like Vercel / AWS Lambda).
    Direct host 'db.<ref>.supabase.co' only has IPv6 (AAAA) DNS records.
    Route through Supavisor IPv4 Pooler when a direct host is detected.
    """
    if not url:
        return url
    
    # Check if this is a direct Supabase host db.<ref>.supabase.co
    m = re.match(r'^(postgres(?:ql)?://)([^:]+):([^@]+)@db\.([a-z0-9]+)\.supabase\.co(?::(\d+))?/(.*)$', url)
    if m:
        protocol, user, pwd, ref, port, dbname = m.groups()
        port = port or '5432'
        # Supavisor pooler requires user format: <user>.<project_ref>
        if '.' not in user:
            user = f'{user}.{ref}'
        # Route to the project's IPv4 Supabase pooler (ap-northeast-1)
        pooler_host = 'aws-0-ap-northeast-1.pooler.supabase.com'
        url = f'{protocol}{user}:{pwd}@{pooler_host}:{port}/{dbname}'
        print(f'INFO: Automatically routed direct Supabase host to IPv4 pooler ({pooler_host}:{port}).')

    # Ensure sslmode is present if using Supabase
    if 'supabase.com' in url and 'sslmode=' not in url:
        sep = '&' if '?' in url else '?'
        url = f'{url}{sep}sslmode=require'

    return url

EFFECTIVE_DATABASE_URL = normalize_database_url(DATABASE_URL)

_pg_pool = None

def get_pg_pool():
    global _pg_pool
    if not EFFECTIVE_DATABASE_URL:
        return None
    if _pg_pool is not None:
        return _pg_pool
    try:
        import psycopg2
        from psycopg2 import pool
        # Serverless-friendly pool sizing
        _pg_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=5, dsn=EFFECTIVE_DATABASE_URL)
        print('INFO: Connected to Supabase PostgreSQL database.')
        return _pg_pool
    except Exception as e:
        print(f'ERROR: Failed to connect to PostgreSQL ({e}).', file=sys.stderr)
        _pg_pool = None
        return None

# Attempt early init
if EFFECTIVE_DATABASE_URL:
    get_pg_pool()

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
    global _pg_pool
    if EFFECTIVE_DATABASE_URL:
        pool = get_pg_pool()
        if pool:
            try:
                raw_conn = pool.getconn()
                if raw_conn.closed:
                    _pg_pool = None
                    pool = get_pg_pool()
                    raw_conn = pool.getconn()
                return PostgresConnectionWrapper(raw_conn, pool_ref=pool)
            except Exception as e:
                print(f'ERROR: Connection checkout failed: {e}', file=sys.stderr)
                # Attempt one reconnection
                try:
                    _pg_pool = None
                    pool = get_pg_pool()
                    if pool:
                        raw_conn = pool.getconn()
                        return PostgresConnectionWrapper(raw_conn, pool_ref=pool)
                except Exception:
                    pass

    # SQLite fallback
    fallback_db = ROOT / 'schools_optimized.db'
    target_db = DB_FILE if DB_FILE.exists() else (fallback_db if fallback_db.exists() else None)

    if not target_db:
        if EFFECTIVE_DATABASE_URL:
            raise RuntimeError("Database connection to Supabase failed. Please verify DATABASE_URL and network access.")
        raise RuntimeError(f"Database not found. Please set DATABASE_URL or provide schools.db at {DB_FILE}.")

    c = sqlite3.connect(str(target_db), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only = ON')
    c.execute('PRAGMA busy_timeout = 30000')
    return c

def get_stream_connection():
    return get_db_connection()
