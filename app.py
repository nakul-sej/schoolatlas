from pathlib import Path
import os
import sqlite3
from db_backend import get_db_connection, get_stream_connection
import re
import math
import json
from urllib.parse import urlencode
from xml.sax.saxutils import escape
from flask import Flask, g, render_template, request, abort, Response, redirect, url_for

from seo import (
    clean_text,
    format_school_name,
    format_location_name,
    slugify,
    school_key,
    school_slug,
    school_canonical_path,
    generate_school_title,
    generate_school_description,
    generate_location_title,
    generate_location_description,
    is_school_indexable,
    school_structured_data,
)

ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get('DATABASE_URL', os.environ.get('SUPABASE_DB_URL', '')).strip()
DB = Path(os.environ.get('SCHOOLS_DB_PATH', ROOT / 'schools_optimized.db'))
TABLE = os.environ.get('SCHOOLS_TABLE', 'schools')
SITE_URL = os.environ.get('SITE_URL', '').strip().rstrip('/')
SITEMAP_SIZE = 40000  # Max 40,000 URLs per sitemap (well below Google's 50,000 limit)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

ALIASES = {
    'udise_code': ['udise_code', 'udise'],
    'school_id': ['school_id', 'id'],
    'school_name': ['school_name', 'name'],
    'class_frm': ['class_frm', 'class_from', 'from_class'],
    'class_to': ['class_to', 'to_class'],
    'district_name': ['district_name', 'district'],
    'state_name': ['state_name', 'state'],
    'pincode': ['pincode', 'pin_code'],
    'latitude': ['latitude', 'lat'],
    'longitude': ['longitude', 'lng', 'lon'],
    'address': ['address', 'school_address', 'location'],
    'phone_number': ['phone_number', 'phone', 'mobile', 'contact_number'],
    'email': ['email', 'email_address'],
    'website': ['website', 'url', 'school_website'],
    'principal_name': ['principal_name', 'principal'],
    'management_type': ['management_type', 'state_mgmt', 'national_mgmt', 'management'],
    'school_category': ['school_category', 'category'],
    'village_name': ['village_name', 'village'],
    'subdistrict_name': ['subdistrict_name', 'block', 'sub_district'],
    'school_type': ['school_type', 'type'],
    'rural_urban': ['rural_urban', 'area_type', 'location_type'],
    'board_type': ['board_type', 'board', 'board_name'],
    'affiliation_type': ['affiliation_type', 'affiliation'],
    'affiliation_number': ['affiliation_number', 'affiliation_no', 'aff_no'],
    'logo_url': ['logo_url', 'logo'],
    'color_primary': ['color_primary', 'primary_color'],
    'color_secondary': ['color_secondary', 'secondary_color'],
    'color_tertiary': ['color_tertiary', 'tertiary_color'],
    'theme_colors': ['theme_colors'],
    'source': ['source', 'data_source'],
    'duplicate_status': ['duplicate_status'],
    'duplicate_of_udise_code': ['duplicate_of_udise_code'],
    'updated_at': ['updated_at', 'last_updated', 'modified_at'],
    'created_at': ['created_at'],
}

# In-memory cache for location lookup (1,561 valid state-district pairs)
LOCATION_CACHE = None

def qi(s):
    return '"' + str(s).replace('"', '""') + '"'

SCHEMA_CACHE = None

def db():
    if 'db' not in g:
        g.db = get_db_connection()
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    c = g.pop('db', None)
    if c is not None:
        try:
            c.close()
        except Exception as err:
            app.logger.warning('Error returning database connection to pool: %s', err)

def schema():
    global SCHEMA_CACHE
    if SCHEMA_CACHE is not None:
        return SCHEMA_CACHE

    c = db()
    tables = [x['name'] for x in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
    if TABLE not in tables:
        raise RuntimeError(f"Table '{TABLE}' not found. Available tables: {', '.join(tables)}. Set SCHOOLS_TABLE if needed.")
    cols = [x['name'] for x in c.execute(f'PRAGMA table_info({qi(TABLE)})')]
    lookup = {x.lower(): x for x in cols}
    mapping = {}
    for key, opts in ALIASES.items():
        for opt in opts:
            if opt.lower() in lookup:
                mapping[key] = lookup[opt.lower()]
                break
    SCHEMA_CACHE = (cols, mapping)
    return SCHEMA_CACHE

def getv(row, m, key):
    col = m.get(key)
    v = row[col] if col and col in row.keys() else None
    return clean_text(v)

def school(row, m):
    d = {k: getv(row, m, k) for k in ALIASES}
    d['_raw'] = dict(row)
    # Use formatted school name for clean display
    d['school_name'] = format_school_name(d['school_name'])
    d['district_name'] = format_location_name(d['district_name'])
    d['state_name'] = format_location_name(d['state_name'])
    d['display_location'] = ', '.join(x for x in [d['address'], d['village_name'], d['subdistrict_name'], d['district_name'], d['state_name'], d['pincode']] if x)
    d['class_range'] = f"{d['class_frm']} – {d['class_to']}" if d['class_frm'] and d['class_to'] else d['class_frm'] or d['class_to']
    d['_rowid'] = str(row['_rowid']) if '_rowid' in row.keys() else (str(row['rowid']) if 'rowid' in row.keys() else '')
    return d

def canonical_root():
    if SITE_URL:
        return SITE_URL
    return request.url_root.rstrip('/')

def absolute_url(path):
    root = canonical_root()
    if not path.startswith('/'):
        path = '/' + path
    return root + path

def school_url(s):
    """Returns canonical relative path with trailing slash: /school/<slug>-<key>/"""
    return school_canonical_path(s)

app.jinja_env.filters['slugify'] = slugify
app.jinja_env.globals['school_url'] = school_url
app.jinja_env.globals['school_canonical_path'] = school_canonical_path

@app.context_processor
def inject_seo_globals():
    return {'site_root': canonical_root()}

# --- Fast Indexed Identifier Resolution ---

def find_school_by_key(c, m, key):
    """
    Looks up a school record by exact identifier.
    Uses indexed queries directly:
    1. udise_code = ? (primary key index)
    2. udise_code = ? COLLATE NOCASE
    3. school_id = ? (idx_schools_school_id)
    4. rowid = ?
    """
    k = str(key).strip()
    if not k:
        return None

    udise_col = m.get('udise_code')
    if udise_col:
        # Fast PK lookup
        row = c.execute(f"SELECT rowid AS _rowid, * FROM {qi(TABLE)} WHERE {qi(udise_col)} = ? LIMIT 1", (k,)).fetchone()
        if row:
            return row
        # Case-insensitive for Cambridge/text codes
        if not k.isdigit():
            row = c.execute(f"SELECT rowid AS _rowid, * FROM {qi(TABLE)} WHERE {qi(udise_col)} = ? COLLATE NOCASE LIMIT 1", (k,)).fetchone()
            if row:
                return row

    school_id_col = m.get('school_id')
    if school_id_col:
        row = c.execute(f"SELECT rowid AS _rowid, * FROM {qi(TABLE)} WHERE {qi(school_id_col)} = ? LIMIT 1", (k,)).fetchone()
        if row:
            return row

    if k.isdigit():
        row = c.execute(f"SELECT rowid AS _rowid, * FROM {qi(TABLE)} WHERE rowid = ? LIMIT 1", (int(k),)).fetchone()
        if row:
            return row

    return None

def resolve_school_slug(c, m, raw_path):
    """
    Deterministic resolution of incoming school URL parameter.
    Supports:
    - Canonical: /school/<slug>-<identifier>/
    - Legacy /school/<slug>/<key>
    - Direct /school/<identifier>
    - Handles Cambridge codes (cambind-XXXX)
    """
    clean_p = raw_path.strip('/')
    if not clean_p:
        return None

    # Check legacy /school/<slug>/<key>
    if '/' in clean_p:
        parts = clean_p.split('/')
        return find_school_by_key(c, m, parts[-1])

    # Check direct identifier
    row = find_school_by_key(c, m, clean_p)
    if row:
        return row

    # Check Cambridge pattern e.g. ...-cambind-0005
    m_cam = re.search(r'-(cambind-\d+)$', clean_p, re.IGNORECASE)
    if m_cam:
        row = find_school_by_key(c, m, m_cam.group(1))
        if row:
            return row

    # Check standard trailing identifier: ...-33311507105
    m_id = re.search(r'-([a-zA-Z0-9]+)$', clean_p)
    if m_id:
        row = find_school_by_key(c, m, m_id.group(1))
        if row:
            return row

    return None

# --- Location Lookup Cache ---

def get_location_lookup(c, m):
    """
    Caches valid state and district pairs in memory.
    1,561 rows cached once to eliminate table scans on location routes.
    """
    global LOCATION_CACHE
    if LOCATION_CACHE is not None:
        return LOCATION_CACHE

    st_col = m.get('state_name')
    dt_col = m.get('district_name')
    if not st_col or not dt_col:
        LOCATION_CACHE = {}
        return LOCATION_CACHE

    query = f"""
        SELECT TRIM(CAST({qi(st_col)} AS TEXT)) AS state,
               TRIM(CAST({qi(dt_col)} AS TEXT)) AS district,
               COUNT(*) AS cnt
        FROM {qi(TABLE)}
        WHERE {qi(st_col)} IS NOT NULL AND TRIM(CAST({qi(st_col)} AS TEXT)) != ''
          AND NOT {qi(st_col)} GLOB '[0-9]*'
          AND {qi(dt_col)} IS NOT NULL AND TRIM(CAST({qi(dt_col)} AS TEXT)) != ''
          AND NOT {qi(dt_col)} GLOB '[0-9]*'
        GROUP BY 1, 2
        HAVING COUNT(*) >= 1
    """
    rows = c.execute(query).fetchall()
    lookup = {}
    for r in rows:
        st_val = str(r['state']).strip()
        dt_val = str(r['district']).strip()
        st_formatted = format_location_name(st_val)
        dt_formatted = format_location_name(dt_val)
        can_st_slug = slugify(st_formatted)
        can_dt_slug = slugify(dt_formatted)

        entry = {
            'state': st_val,
            'district': dt_val,
            'state_formatted': st_formatted,
            'district_formatted': dt_formatted,
            'canonical_state_slug': can_st_slug,
            'canonical_district_slug': can_dt_slug,
            'count': r['cnt']
        }
        # Map primary canonical pair
        lookup[(can_st_slug, can_dt_slug)] = entry
        # Map raw slug pair if different
        raw_st_slug = slugify(st_val)
        raw_dt_slug = slugify(dt_val)
        if (raw_st_slug, raw_dt_slug) != (can_st_slug, can_dt_slug):
            lookup[(raw_st_slug, raw_dt_slug)] = entry
    LOCATION_CACHE = lookup
    return LOCATION_CACHE

def distinct(c, m, key, limit=1000):
    col = m.get(key)
    if not col:
        return []
    # Filter out empty or numeric corrupted tokens
    return [r['v'] for r in c.execute(
        f"SELECT DISTINCT TRIM(CAST({qi(col)} AS TEXT)) v FROM {qi(TABLE)} "
        f"WHERE {qi(col)} IS NOT NULL AND TRIM(CAST({qi(col)} AS TEXT)) != '' "
        f"AND NOT TRIM(CAST({qi(col)} AS TEXT)) GLOB '[0-9]*' "
        f"ORDER BY v COLLATE NOCASE LIMIT ?", (limit,)) if r['v']]

def search_clause(q, m):
    cols = [m[k] for k in ['school_name', 'udise_code', 'district_name', 'state_name', 'village_name', 'subdistrict_name', 'pincode', 'board_type'] if m.get(k)]
    return ('(' + ' OR '.join(f'{qi(x)} LIKE ?' for x in cols) + ')', [f'%{q}%'] * len(cols)) if cols else ('1=1', [])

# --- Error Handlers ---

@app.errorhandler(404)
def not_found_error(e):
    return render_template('error.html', title='School Not Found', message="The requested school profile or directory page could not be found. It may have been moved or the link may be outdated.", is_404=True), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', title='System Notice', message="A temporary error occurred while retrieving directory information. Please try again in a few moments.", is_404=False), 500

@app.errorhandler(RuntimeError)
def db_error(e):
    return render_template('error.html', title='Database Configuration Needed', message=str(e), is_404=False), 500

# --- Routes ---

@app.route('/')
def home():
    c = db()
    cols, m = schema()
    q = request.args.get('q', '').strip()[:160]
    state = request.args.get('state', '').strip()[:160]
    district = request.args.get('district', '').strip()[:160]
    board = request.args.get('board', '').strip()[:160]
    try:
        page = max(1, min(1000000, int(request.args.get('page', 1))))
    except ValueError:
        page = 1
    size = 24
    conditions = []
    params = []
    if q:
        w, p = search_clause(q, m)
        conditions.append(w)
        params += p
    for key, value in [('state_name', state), ('district_name', district), ('board_type', board)]:
        if value and m.get(key):
            conditions.append(f"{qi(m[key])} = ?")
            params.append(value)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    total = c.execute(f'SELECT COUNT(*) n FROM {qi(TABLE)}{where}', params).fetchone()['n']
    order = m.get('school_name') or m.get('udise_code') or cols[0]
    rows = c.execute(f'SELECT rowid AS _rowid, * FROM {qi(TABLE)}{where} ORDER BY {qi(order)} COLLATE NOCASE LIMIT ? OFFSET ?', params + [size, (page-1)*size]).fetchall()
    items = [school(r, m) for r in rows]
    stats = {
        'total': c.execute(f'SELECT COUNT(*) n FROM {qi(TABLE)}').fetchone()['n'],
        'states': len(distinct(c, m, 'state_name')),
        'districts': len(distinct(c, m, 'district_name'))
    }
    noindex = bool(q or state or district or board or page > 1)
    return render_template(
        'index.html',
        schools=items,
        stats=stats,
        q=q,
        state=state,
        district=district,
        board=board,
        states=distinct(c, m, 'state_name'),
        districts=distinct(c, m, 'district_name'),
        boards=distinct(c, m, 'board_type'),
        total=total,
        page=page,
        pages=max(1, (total+size-1)//size),
        canonical=absolute_url('/'),
        noindex=noindex
    )

# Legacy 2-segment route: /school/<slug>/<key> -> permanent 301 to canonical /school/<slug>-<key>/
@app.route('/school/<slug>/<key>')
@app.route('/school/<slug>/<key>/')
def legacy_two_segment_detail(slug, key):
    c = db()
    _, m = schema()
    row = find_school_by_key(c, m, key)
    if not row:
        abort(404)
    s = school(row, m)
    return redirect(school_url(s), code=301)

# Canonical and flexible detail route
@app.route('/school/<path:slug_or_key>/')
@app.route('/school/<path:slug_or_key>')
def detail(slug_or_key):
    c = db()
    cols, m = schema()

    # 1. Resolve school by identifier or slug
    row = resolve_school_slug(c, m, slug_or_key)
    if not row:
        abort(404)

    s = school(row, m)

    # 2. Check duplicate status with master redirection
    dupe_target = s.get('duplicate_of_udise_code')
    if s.get('duplicate_status') == 'duplicate' and dupe_target:
        master_row = find_school_by_key(c, m, dupe_target)
        if master_row:
            master_s = school(master_row, m)
            return redirect(school_url(master_s), code=301)

    canonical_rel_url = school_url(s)  # e.g. /school/diamond-stone-international-school-33311507105/
    canonical_full_url = absolute_url(canonical_rel_url)

    # 3. Canonical URL Enforcement:
    # If URL lacks trailing slash, or slug doesn't match canonical slug exactly, issue 301
    requested_path = request.path
    if not requested_path.endswith('/'):
        return redirect(canonical_rel_url, code=301)
    if requested_path != canonical_rel_url:
        return redirect(canonical_rel_url, code=301)

    # 4. Related schools in the same district or state
    related = []
    identcol = m.get('udise_code') or m.get('school_id')
    for field in ['district_name', 'state_name']:
        col = m.get(field)
        val = row[col] if col and col in row.keys() else None
        if not col or not val:
            continue
        ordercol = m.get('school_name') or identcol or cols[0]
        cur_rowid = int(s['_rowid']) if s['_rowid'].isdigit() else -1
        rows = c.execute(
            f'SELECT rowid AS _rowid, * FROM {qi(TABLE)} '
            f'WHERE {qi(col)} = ? AND rowid != ? '
            f'ORDER BY {qi(ordercol)} COLLATE NOCASE LIMIT 8',
            (val, cur_rowid)
        ).fetchall()
        for r in rows:
            item = school(r, m)
            if item.get('udise_code') != s.get('udise_code') and all(item.get('udise_code') != x.get('udise_code') for x in related):
                is_rel_idx, _ = is_school_indexable(item)
                if is_rel_idx:
                    related.append(item)
            if len(related) >= 4:
                break
        if len(related) >= 4:
            break

    # 5. Map URL
    map_url = ''
    try:
        lat, lon = float(s['latitude']), float(s['longitude'])
        if -90 <= lat <= 90 and -180 <= lon <= 180 and lat != 0 and lon != 0:
            map_url = f'https://www.google.com/maps?q={lat},{lon}'
    except (ValueError, TypeError):
        pass
    if not map_url and s['display_location']:
        map_url = 'https://www.google.com/maps/search/?' + urlencode({'api': '1', 'query': s['display_location']})

    # 6. Colors
    primary = s['color_primary'] if re.fullmatch(r'#[0-9a-fA-F]{6}', s['color_primary'] or '') else '#0b376d'
    secondary = s['color_secondary'] if re.fullmatch(r'#[0-9a-fA-F]{6}', s['color_secondary'] or '') else '#f8c84b'

    # 7. Raw fields for drawer
    raw = [(k, v) for k, v in s['_raw'].items() if v is not None and str(v).strip() and str(v).strip().lower() not in ('null', 'none', 'nan', 'n/a', '-')]

    # 8. Indexability Policy
    is_indexable, _ = is_school_indexable(s)
    noindex = not is_indexable

    # 9. Dynamic On-Page SEO
    title = generate_school_title(s)
    description = generate_school_description(s)
    structured_data = school_structured_data(s, canonical_full_url, canonical_root())

    return render_template(
        'school.html',
        s=s,
        related=related,
        map_url=map_url,
        primary=primary,
        secondary=secondary,
        raw=raw,
        canonical=canonical_full_url,
        noindex=noindex,
        title=title,
        description=description,
        structured_data=structured_data
    )

@app.route('/schools/<state_slug>/<district_slug>/')
@app.route('/schools/<state_slug>/<district_slug>')
def location_page(state_slug, district_slug):
    c = db()
    cols, m = schema()
    if not m.get('state_name') or not m.get('district_name'):
        abort(404)

    # Trailing slash enforcement
    if not request.path.endswith('/'):
        return redirect(url_for('location_page', state_slug=state_slug, district_slug=district_slug), code=301)

    lookup = get_location_lookup(c, m)
    match = lookup.get((state_slug, district_slug))
    if not match:
        abort(404)

    # 301 redirect alias slugs to canonical formatted slugs
    can_st = match['canonical_state_slug']
    can_dt = match['canonical_district_slug']
    if (state_slug, district_slug) != (can_st, can_dt):
        return redirect(url_for('location_page', state_slug=can_st, district_slug=can_dt), code=301)

    state_value = match['state']
    district_value = match['district']
    total = match['count']

    try:
        page = max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        page = 1

    size = 36
    cond = f"{qi(m['state_name'])} = ? AND {qi(m['district_name'])} = ?"
    order = m.get('school_name') or m.get('udise_code') or cols[0]
    rows = c.execute(
        f'SELECT rowid AS _rowid, * FROM {qi(TABLE)} WHERE {cond} ORDER BY {qi(order)} COLLATE NOCASE LIMIT ? OFFSET ?',
        (state_value, district_value, size, (page-1)*size)
    ).fetchall()
    items = [school(r, m) for r in rows]
    pages = max(1, math.ceil(total / size))
    canonical = absolute_url(f"/schools/{can_st}/{can_dt}/")

    title = generate_location_title(state_value, district_value)
    description = generate_location_description(state_value, district_value, total)

    return render_template(
        'location.html',
        schools=items,
        state=state_value,
        district=district_value,
        total=total,
        page=page,
        pages=pages,
        canonical=canonical,
        noindex=(page > 1),
        title=title,
        description=description
    )

# --- XML Sitemaps ---

@app.route('/sitemap.xml')
def sitemap_index():
    c = db()
    _, m = schema()
    # Count indexable schools
    count_sql = f"""
        SELECT COUNT(*) n FROM {qi(TABLE)}
        WHERE ({qi(m['school_name'])} IS NOT NULL AND TRIM({qi(m['school_name'])}) != '' AND {qi(m['school_name'])} NOT LIKE '#%' AND LENGTH(TRIM({qi(m['school_name'])})) >= 3)
          AND ({qi(m['udise_code'])} IS NOT NULL AND TRIM(CAST({qi(m['udise_code'])} AS TEXT)) != '' AND {qi(m['udise_code'])} NOT LIKE '%Pri.%')
          AND ({qi(m.get('duplicate_status', 'duplicate_status'))} IS NULL OR {qi(m.get('duplicate_status', 'duplicate_status'))} != 'duplicate')
    """
    total = c.execute(count_sql).fetchone()['n']
    count = max(1, math.ceil(total / SITEMAP_SIZE))
    root = canonical_root()

    body = f'<sitemap><loc>{escape(root + url_for("sitemap_static"))}</loc></sitemap>'
    body += f'<sitemap><loc>{escape(root + url_for("sitemap_locations"))}</loc></sitemap>'
    body += ''.join(f'<sitemap><loc>{escape(root + url_for("sitemap_schools", part=i))}</loc></sitemap>' for i in range(1, count + 1))
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'
    return Response(xml, mimetype='application/xml')

@app.route('/sitemap-static.xml')
def sitemap_static():
    root = canonical_root()
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{escape(root + "/")}</loc><changefreq>daily</changefreq><priority>1.0</priority></url></urlset>'
    return Response(xml, mimetype='application/xml')

@app.route('/sitemap-schools-<int:part>.xml')
def sitemap_schools(part):
    c = db()
    _, m = schema()
    name_col = m['school_name']
    udise_col = m['udise_code']
    upd_col = m.get('updated_at')

    total_sql = f"""
        SELECT COUNT(*) n FROM {qi(TABLE)}
        WHERE ({qi(name_col)} IS NOT NULL AND TRIM({qi(name_col)}) != '' AND {qi(name_col)} NOT LIKE '#%' AND LENGTH(TRIM({qi(name_col)})) >= 3)
          AND ({qi(udise_col)} IS NOT NULL AND TRIM(CAST({qi(udise_col)} AS TEXT)) != '' AND {qi(udise_col)} NOT LIKE '%Pri.%')
          AND ({qi(m.get('duplicate_status', 'duplicate_status'))} IS NULL OR {qi(m.get('duplicate_status', 'duplicate_status'))} != 'duplicate')
    """
    total = c.execute(total_sql).fetchone()['n']
    count = max(1, math.ceil(total / SITEMAP_SIZE))
    if part < 1 or part > count:
        abort(404)

    root = canonical_root()
    offset = (part - 1) * SITEMAP_SIZE
    udise_col = m['udise_code']
    name_col = m['school_name']
    upd_col = m.get('updated_at')

    # Stream generation in batches of 1,000 rows to keep memory at O(1)
    def generate():
        yield '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        batch_size = 1000
        cur_offset = offset
        end_offset = offset + SITEMAP_SIZE

        try:
            while cur_offset < end_offset:
                limit = min(batch_size, end_offset - cur_offset)
                query = f"""
                    SELECT rowid AS _rowid, * FROM {qi(TABLE)}
                    WHERE ({qi(name_col)} IS NOT NULL AND TRIM({qi(name_col)}) != '' AND {qi(name_col)} NOT LIKE '#%' AND LENGTH(TRIM({qi(name_col)})) >= 3)
                      AND ({qi(udise_col)} IS NOT NULL AND TRIM(CAST({qi(udise_col)} AS TEXT)) != '' AND {qi(udise_col)} NOT LIKE '%Pri.%')
                      AND ({qi(m.get('duplicate_status', 'duplicate_status'))} IS NULL OR {qi(m.get('duplicate_status', 'duplicate_status'))} != 'duplicate')
                    ORDER BY {qi(udise_col)}
                    LIMIT {limit} OFFSET {cur_offset}
                """
                batch = c.execute(query).fetchall()
                if not batch:
                    break

                for r in batch:
                    s = school(r, m)
                    idx, _ = is_school_indexable(s)
                    if not idx:
                        continue
                    loc = root + school_url(s)
                    lastmod = ''
                    if upd_col and upd_col in r.keys() and r[upd_col]:
                        raw_dt = str(r[upd_col]).strip()
                        if len(raw_dt) >= 10:
                            lastmod = raw_dt[:10]

                    if lastmod:
                        yield f'<url><loc>{escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>\n'
                    else:
                        yield f'<url><loc>{escape(loc)}</loc></url>\n'

                cur_offset += len(batch)
                if len(batch) < limit:
                    break
        finally:
            pass

        yield '</urlset>'

    return Response(generate(), mimetype='application/xml')

@app.route('/sitemap-locations.xml')
def sitemap_locations():
    c = db()
    _, m = schema()
    lookup = get_location_lookup(c, m)
    root = canonical_root()

    seen = set()
    entries = []
    for loc_data in sorted(lookup.values(), key=lambda x: (x['canonical_state_slug'], x['canonical_district_slug'])):
        pair = (loc_data['canonical_state_slug'], loc_data['canonical_district_slug'])
        if pair in seen:
            continue
        seen.add(pair)
        path = url_for('location_page', state_slug=pair[0], district_slug=pair[1])
        entries.append(f'<url><loc>{escape(root + path)}</loc><changefreq>weekly</changefreq></url>')

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(entries) + '\n</urlset>'
    return Response(xml, mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    root = canonical_root()
    txt = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /static/\n"
        "Disallow: /health\n"
        "Disallow: /?*\n"
        "Disallow: /*?*\n"
        f"Sitemap: {root}/sitemap.xml\n"
    )
    return Response(txt, mimetype='text/plain')

@app.route('/health')
def health():
    c = db()
    cols, m = schema()
    total = c.execute(f'SELECT COUNT(*) n FROM {qi(TABLE)}').fetchone()['n']
    indexes = [r['name'] for r in c.execute(f'PRAGMA index_list({qi(TABLE)})').fetchall()]
    return {
        'ok': True,
        'database_type': ('Supabase PostgreSQL' if DATABASE_URL else 'SQLite'),
        'database': ('db.leghkaeuqhlxrbdxpsij.supabase.co' if DATABASE_URL else DB.name),
        'table': TABLE,
        'rows': total,
        'columns': len(cols),
        'mapped_fields': list(m),
        'indexes': indexes,
        'site_url_configured': bool(SITE_URL),
        'site_url': SITE_URL or 'http://127.0.0.1:5000'
    }

if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', '5000')),
        debug=False
    )
