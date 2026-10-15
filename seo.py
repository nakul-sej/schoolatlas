"""
SchoolAtlas - SEO & Metadata Engineering Module
Provides deterministic slugging, clean title/description generation,
indexability policy evaluation, and Schema.org JSON-LD generation.
"""
import re
import json
import html
from urllib.parse import urljoin

ACRONYMS = {
    'cbse', 'icse', 'cisce', 'nios', 'ib', 'igcse', 'cie', 'state',
    'bvm', 'dps', 'dav', 'kv', 'jnv', 'army', 'air', 'force', 'aps', 'ssvm',
    'iit', 'nit', 'sr', 'sec', 'jr', 'hsc', 'sslc', 'coed', 'ncr', 'sms',
    'kg', 'lkg', 'ukg', 'pp', 'pri', 'sec', 'hss', 'ghs', 'gps', 'gaps',
    'gms', 'gsss', 'gup', 'gups', 'pums', 'pups', 'adw', 'ms'
}

ROMAN_NUMERALS = {'i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii'}

def clean_text(val):
    """Strips quotes, tabs, newlines, null tokens, and collapses whitespace."""
    if val is None:
        return ''
    s = str(val).strip()
    if s.lower() in ('', 'null', 'none', 'nan', 'n/a', 'na', '-', '#name?', '#n/a', '#value!', '#ref!'):
        return ''
    # Strip wrapping quotes
    s = s.strip('"\'')
    # Replace control characters and tabs/newlines with single spaces
    s = re.sub(r'[\r\n\t]+', ' ', s)
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' "\'')

def format_school_name(raw_name):
    """
    Cleans raw school names (which often come in ALL CAPS with tabs/quotes)
    into clean, readable title casing while preserving recognized acronyms
    and Roman numerals (e.g. CBSE, ICSE, KV, Class I-XII).
    """
    cleaned = clean_text(raw_name)
    if not cleaned:
        return 'School Profile'

    # If already mixed case and not all caps, just clean whitespace
    has_lower = any(c.islower() for c in cleaned)
    has_upper = any(c.isupper() for c in cleaned)

    if has_lower and has_upper:
        # Check if it has obvious ugly tab/quote artifacts already stripped
        return cleaned

    # Otherwise format from uppercase to Title Case with acronym preservation
    words = cleaned.split(' ')
    formatted_words = []
    for w in words:
        if not w:
            continue
        clean_w = w.strip('.,()[]{}:;')
        clean_w_lower = clean_w.lower()

        # Roman numerals (I, II, III, IV, etc.)
        if clean_w_lower in ROMAN_NUMERALS:
            formatted_words.append(w.upper())
        # Acronyms (CBSE, ICSE, DPS, etc.)
        elif clean_w_lower in ACRONYMS:
            # Reconstruct word preserving punctuation
            prefix = w[:len(w) - len(w.lstrip('.,()[]{}:;'))]
            suffix = w[len(w.rstrip('.,()[]{}:;')):]
            formatted_words.append(f"{prefix}{clean_w.upper()}{suffix}")
        # Standard Title Case
        else:
            formatted_words.append(w.capitalize())

    result = ' '.join(formatted_words)
    # Fix small known connectors if in middle of name
    for conn in ('Of', 'And', 'In', 'The', 'For', 'At'):
        result = re.sub(rf'\b{conn}\b', conn.lower(), result)
    # Ensure first character is uppercase
    if result:
        result = result[0].upper() + result[1:]
    return result

def format_location_name(val):
    """Formats district or state name cleanly."""
    c = clean_text(val)
    if not c:
        return ''
    # Special cases for Indian states & UTs
    lower = c.lower()
    if 'andaman' in lower:
        return 'Andaman & Nicobar Islands'
    if 'jammu' in lower:
        return 'Jammu & Kashmir'
    if 'delhi' in lower:
        return 'Delhi'
    if 'tamil' in lower or lower == 'tamilnadu':
        return 'Tamil Nadu'
    if 'uttar pradesh' in lower:
        return 'Uttar Pradesh'
    if 'madhya pradesh' in lower:
        return 'Madhya Pradesh'
    if 'andhra' in lower:
        return 'Andhra Pradesh'
    if 'himachal' in lower:
        return 'Himachal Pradesh'
    if 'west bengal' in lower:
        return 'West Bengal'
    if 'daman' in lower or 'diu' in lower:
        return 'Dadra & Nagar Haveli and Daman & Diu'

    # Default to title case
    return ' '.join(word.capitalize() for word in c.split(' '))

def slugify(value):
    """
    Creates a deterministic, URL-safe slug.
    Replaces non-alphanumeric characters with hyphens, collapses hyphens,
    and lowercases.
    """
    cleaned = clean_text(value)
    if not cleaned:
        return 'school'
    s = cleaned.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:100].strip('-') or 'school'

def school_key(s):
    """
    Extracts the stable identifier for a school record.
    Prefers udise_code, falls back to school_id, then internal rowid.
    """
    udise = clean_text(s.get('udise_code') if isinstance(s, dict) else s['udise_code'])
    if udise and len(udise) >= 4 and not udise.lower().startswith('pri.'):
        return udise
    sid = clean_text(s.get('school_id') if isinstance(s, dict) else s['school_id'])
    if sid:
        return sid
    rid = str(s.get('_rowid', '') if isinstance(s, dict) else (s['_rowid'] if '_rowid' in s.keys() else ''))
    return rid or '0'

def school_slug(s):
    """
    Generates deterministic slug matching preferred SEO format:
    e.g. 'diamond-stone-international-school-33311507105'
    """
    name = format_school_name(s.get('school_name') if isinstance(s, dict) else s['school_name'])
    name_slug = slugify(name)
    key = school_key(s)
    key_slug = slugify(key)
    return f"{name_slug}-{key_slug}"

def school_canonical_path(s):
    """Canonical relative URL with trailing slash."""
    return f"/school/{school_slug(s)}/"

def generate_school_title(s):
    """
    Creates unique, descriptive title:
    'Diamond Stone International School, Krishnagiri, Tamil Nadu | SchoolAtlas'
    """
    name = format_school_name(s.get('school_name', ''))
    district = format_location_name(s.get('district_name', ''))
    state = format_location_name(s.get('state_name', ''))

    parts = [name]
    # Add district if not already part of school name
    if district and district.lower() not in name.lower():
        parts.append(district)
    # Add state if available and not redundant
    if state and (state.lower() not in name.lower()) and (state.lower() != district.lower()):
        parts.append(state)

    title_core = ', '.join(parts)
    return f"{title_core} | SchoolAtlas"

def generate_school_description(s):
    """
    Creates a concise, truthful meta description based only on verified fields:
    'View available details for Diamond Stone International School in Krishnagiri, Tamil Nadu, including its recorded school category and location information.'
    """
    name = format_school_name(s.get('school_name', ''))
    district = format_location_name(s.get('district_name', ''))
    state = format_location_name(s.get('state_name', ''))

    loc_str = ''
    if district and state:
        loc_str = f" in {district}, {state}"
    elif district:
        loc_str = f" in {district}"
    elif state:
        loc_str = f" in {state}"

    category = clean_text(s.get('school_category', ''))
    board = clean_text(s.get('board_type', ''))
    class_frm = clean_text(s.get('class_frm', ''))
    class_to = clean_text(s.get('class_to', ''))

    details_list = []
    if category:
        details_list.append(f"recorded school category ({category.lower()})")
    if board:
        details_list.append(f"board ({board})")
    elif class_frm and class_to:
        details_list.append(f"classes {class_frm} to {class_to}")

    details_list.append("location information")
    details_str = ', '.join(details_list)

    desc = f"View available details for {name}{loc_str}, including its {details_str}."
    if len(desc) > 160:
        desc = f"View available details for {name}{loc_str}, including recorded school category and location information."
    if len(desc) > 160:
        desc = f"View available details for {name}{loc_str}, including school category and location details."
    if len(desc) > 160:
        desc = f"View school details and location for {name}{loc_str}."
    if len(desc) > 160:
        desc = f"View details for {name}{loc_str} on SchoolAtlas."[:157] + '...'
    return desc

def generate_location_title(state, district):
    """Browser title for state/district directory."""
    d = format_location_name(district)
    s = format_location_name(state)
    return f"Schools in {d}, {s} | SchoolAtlas"

def generate_location_description(state, district, total=None):
    """Meta description for location directory page."""
    d = format_location_name(district)
    s = format_location_name(state)
    if total and total > 0:
        return f"Browse {total:,} verified school records listed for {d}, {s}. View school names, categories, boards, and location profiles on SchoolAtlas."
    return f"Browse school directory listings for {d}, {s}, with verified school profiles and location details on SchoolAtlas."

def is_school_indexable(s):
    """
    Phase 5 Indexability Policy:
    Evaluates whether a school profile qualifies for public search indexing.
    Returns (is_indexable: bool, reason: str)
    """
    raw_name = s.get('school_name', '') if isinstance(s, dict) else s['school_name']
    name = clean_text(raw_name)
    if not name or len(name) < 3:
        return False, "Empty or too short school name"
    if name.startswith('#') or name.lower() in ('ms', 'school', 'primary school'):
        if len(name) < 4:
            return False, "Invalid or placeholder school name"

    # Identifier validation
    raw_udise = s.get('udise_code', '') if isinstance(s, dict) else s['udise_code']
    udise = clean_text(raw_udise)
    if not udise or len(udise) < 4 or udise.lower().startswith('pri.'):
        return False, "Missing or corrupted school identifier"

    # Flagged duplicate check
    raw_status = s.get('duplicate_status', '') if isinstance(s, dict) else s['duplicate_status']
    status = clean_text(raw_status).lower()
    if status == 'duplicate':
        return False, "Flagged duplicate record"

    # Location check
    raw_state = s.get('state_name', '') if isinstance(s, dict) else s['state_name']
    raw_dist = s.get('district_name', '') if isinstance(s, dict) else s['district_name']
    state = clean_text(raw_state)
    dist = clean_text(raw_dist)
    if not state or not dist:
        return False, "Missing state or district location"
    if re.match(r'^[0-9.]+$', state) or re.match(r'^[0-9.]+$', dist):
        return False, "Corrupted location coordinates in state/district fields"

    # Substantive content check: must have at least one informative verified attribute
    has_substance = any([
        s.get('class_frm') is not None or s.get('class_to') is not None,
        clean_text(s.get('school_category')),
        clean_text(s.get('management_type')),
        clean_text(s.get('board_type')),
        clean_text(s.get('affiliation_type')),
        clean_text(s.get('address')),
        s.get('latitude') is not None and s.get('longitude') is not None,
        clean_text(s.get('phone_number')),
        clean_text(s.get('email')),
        clean_text(s.get('website')),
    ])
    if not has_substance:
        return False, "Thin record with no substantive school attributes"

    return True, "Indexable"

def school_structured_data(s, canonical_url, site_root):
    """
    Generates Schema.org School and BreadcrumbList JSON-LD.
    Uses only verified, non-empty database fields.
    Guards against script injection.
    """
    name = format_school_name(s.get('school_name', ''))
    school_obj = {
        '@context': 'https://schema.org',
        '@type': 'School',
        'name': name,
        'url': canonical_url,
    }

    udise = clean_text(s.get('udise_code'))
    if udise:
        school_obj['identifier'] = udise

    # Address
    address = {'@type': 'PostalAddress', 'addressCountry': 'IN'}
    addr = clean_text(s.get('address'))
    if addr:
        address['streetAddress'] = addr
    village = clean_text(s.get('village_name'))
    district = format_location_name(s.get('district_name'))
    if village or district:
        address['addressLocality'] = village or district
    state = format_location_name(s.get('state_name'))
    if state:
        address['addressRegion'] = state
    pincode = clean_text(s.get('pincode'))
    if pincode and re.match(r'^\d{6}$', pincode):
        address['postalCode'] = pincode

    if len(address) > 2:
        school_obj['address'] = address

    # Contact info
    phone = clean_text(s.get('phone_number'))
    if phone:
        school_obj['telephone'] = phone
    email = clean_text(s.get('email'))
    if email and '@' in email:
        school_obj['email'] = email
    website = clean_text(s.get('website'))
    if website:
        if not website.startswith(('http://', 'https://')):
            website = f'https://{website}'
        school_obj['sameAs'] = [website]

    # Coordinates
    lat_val = s.get('latitude')
    lon_val = s.get('longitude')
    if lat_val is not None and lon_val is not None:
        try:
            lat = float(lat_val)
            lon = float(lon_val)
            if -90 <= lat <= 90 and -180 <= lon <= 180 and lat != 0 and lon != 0:
                school_obj['geo'] = {
                    '@type': 'GeoCoordinates',
                    'latitude': round(lat, 6),
                    'longitude': round(lon, 6)
                }
        except (ValueError, TypeError):
            pass

    # Breadcrumbs
    breadcrumbs = {
        '@context': 'https://schema.org',
        '@type': 'BreadcrumbList',
        'itemListElement': [
            {
                '@type': 'ListItem',
                'position': 1,
                'name': 'Home',
                'item': site_root + '/'
            }
        ]
    }

    if state and district:
        loc_path = f"/schools/{slugify(state)}/{slugify(district)}/"
        breadcrumbs['itemListElement'].append({
            '@type': 'ListItem',
            'position': 2,
            'name': f"Schools in {district}",
            'item': site_root + loc_path
        })
        breadcrumbs['itemListElement'].append({
            '@type': 'ListItem',
            'position': 3,
            'name': name,
            'item': canonical_url
        })
    else:
        breadcrumbs['itemListElement'].append({
            '@type': 'ListItem',
            'position': 2,
            'name': name,
            'item': canonical_url
        })

    graph = {
        '@context': 'https://schema.org',
        '@graph': [school_obj, breadcrumbs]
    }

    # Safe JSON dump preventing </script> injection
    return json.dumps(graph, ensure_ascii=False).replace('</', '<\\/')
