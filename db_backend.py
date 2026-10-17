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

def sanitize_db_url(url: str) -> str:
    """Mask credentials for safe diagnostic logging."""
    if not url:
        return ""
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', url)

def normalize_database_url(url: str) -> str:
    """
    Ensure Supabase connection strings work across IPv4-only environments (like Vercel / AWS Lambda)
    and use the Transaction Pooler (port 6543) to eliminate (EMAXCONNSESSION) errors.
    """
    if not url:
        return url
    
    # 1. Check if direct Supabase host db.<ref>.supabase.co
    # Direct host only has IPv6 (AAAA) DNS and exhausts backend connections.
    m_direct = re.match(r'^(postgres(?:ql)?://)([^:]+):(.*)@db\.([a-z0-9]+)\.supabase\.co(?::(\d+))?(/.*)$', url)
    if m_direct:
        protocol, user, pwd, ref, port, rest = m_direct.groups()
        if '.' not in user:
            user = f'{user}.{ref}'
        pooler_host = 'aws-0-ap-northeast-1.pooler.supabase.com'
        port = '6543'
        url = f'{protocol}{user}:{pwd}@{pooler_host}:{port}{rest}'
        print(f'INFO: Routed direct Supabase host to IPv4 Transaction Pooler ({pooler_host}:{port}).')

    # 2. Check if pooler host on port 5432 (Session mode) -> upgrade to port 6543 (Transaction mode)
    # Session mode has a tight limit (pool_size: 15) leading to (EMAXCONNSESSION).
    m_pool = re.match(r'^(postgres(?:ql)?://)([^:]+):(.*)@([^:/]+\.pooler\.supabase\.com)(?::(\d+))?(/.*)$', url)
    if m_pool:
        protocol, user, pwd, pool_host, port, rest = m_pool.groups()
        if port != '6543':
            port = '6543'
            url = f'{protocol}{user}:{pwd}@{pool_host}:{port}{rest}'
            print(f'INFO: Upgraded Supabase pooler to Transaction mode ({pool_host}:6543) for serverless scalability.')

    # 3. Ensure sslmode=require for Supabase
    if 'supabase.com' in url and 'sslmode=' not in url:
        sep = '&' if '?' in url else '?'
        url = f'{url}{sep}sslmode=require'

    return url

EFFECTIVE_DATABASE_URL = normalize_database_url(DATABASE_URL)

_pg_pool = None

def _create_raw_connection(dsn: str):
    """Create a single raw psycopg2 connection with autocommit enabled."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    # Autocommit is CRITICAL for transaction pooling (port 6543).
    # It ensures SELECT statements do not leave open transactions hanging,
    # allowing the transaction pooler to instantly release the backend.
    conn.autocommit = True
    return conn

def is_connection_alive(raw_conn) -> bool:
    """Pre-ping to verify connection health before serving a request."""
    if raw_conn is None or getattr(raw_conn, 'closed', 1) != 0:
        return False
    try:
        with raw_conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return True
    except Exception:
        return False

def get_pg_pool():
    """Lazy initialization of thread-safe pool sized for serverless (1 connection per container)."""
    global _pg_pool
    if not EFFECTIVE_DATABASE_URL:
        return None
    if _pg_pool is not None:
        return _pg_pool
    try:
        from psycopg2 import pool
        # Serverless per-container pool: minconn=1, maxconn=1 avoids connection hogging
        # across multiple scaled instances.
        _pg_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=1,
            dsn=EFFECTIVE_DATABASE_URL
        )
        print(f'INFO: Initialized serverless connection pool (target: {sanitize_db_url(EFFECTIVE_DATABASE_URL)}).')
        return _pg_pool
    except Exception as e:
        print(f'ERROR: Failed to initialize connection pool ({e}).', file=sys.stderr)
        _pg_pool = None
        return None

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._cur.close()
        except Exception:
            pass

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
        self._closed = False

    def cursor(self):
        from psycopg2 import extras
        return PostgresCursorWrapper(self._conn.cursor(cursor_factory=extras.RealDictCursor))

    def execute(self, sql, params=None):
        c = self.cursor()
        c.execute(sql, params)
        return c

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self._conn:
            return

        # Rollback any uncommitted transaction to ensure clean return to pool
        try:
            if not getattr(self._conn, 'closed', 1) and getattr(self._conn, 'status', 0) != 0:
                self._conn.rollback()
        except Exception:
            pass

        if self._pool:
            try:
                self._pool.putconn(self._conn)
            except Exception as e:
                print(f"WARN: Error returning connection to pool: {e}", file=sys.stderr)
                try:
                    self._conn.close()
                except Exception:
                    pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass

def get_db_connection():
    """
    Acquire a verified database connection.
    In serverless mode, uses a single-connection pool with active pre-ping.
    """
    global _pg_pool
    if EFFECTIVE_DATABASE_URL:
        pool = get_pg_pool()
        if pool:
            raw_conn = None
            try:
                raw_conn = pool.getconn()
                # Verify connection health (handles idle disconnects after container pause)
                if not is_connection_alive(raw_conn):
                    try:
                        raw_conn.close()
                    except Exception:
                        pass
                    _pg_pool = None
                    pool = get_pg_pool()
                    raw_conn = pool.getconn()
                
                # Ensure autocommit is on
                if not raw_conn.autocommit:
                    raw_conn.autocommit = True

                return PostgresConnectionWrapper(raw_conn, pool_ref=pool)
            except Exception as e:
                print(f'ERROR: Connection checkout failed: {e}', file=sys.stderr)
                # Retry once with a clean pool
                try:
                    if raw_conn:
                        try:
                            raw_conn.close()
                        except Exception:
                            pass
                    _pg_pool = None
                    pool = get_pg_pool()
                    if pool:
                        raw_conn = pool.getconn()
                        raw_conn.autocommit = True
                        return PostgresConnectionWrapper(raw_conn, pool_ref=pool)
                except Exception as retry_err:
                    print(f'ERROR: Connection retry failed: {retry_err}', file=sys.stderr)

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
