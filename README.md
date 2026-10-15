# SchoolAtlas — Production-Ready SEO Engineering Upgrade

A high-performance Flask directory application powered by SQLite with 1.58M+ school records, automated technical SEO, deterministic canonical URLs, Schema.org structured data, chunked XML sitemaps, and indexing quality controls.

```text
schoolatlas/
├── app.py                     # Main Flask web application
├── seo.py                     # Dedicated SEO, slugging, metadata & JSON-LD module
├── schools.db                 # SQLite database (1.58M records)
├── requirements.txt           # Dependencies
├── migrations/
│   ├── add_seo_indexes.py     # Non-destructive indexed query acceleration
│   └── rollback_seo_indexes.py# Safe rollback script
├── tests/
│   └── test_schoolatlas.py    # Automated test suite (24 unit & integration tests)
├── templates/
│   ├── base.html              # Base HTML template with canonical, OG, Twitter & meta tags
│   ├── index.html             # Homepage & directory explorer
│   ├── school.html            # School profile with H1, breadcrumbs, facts & JSON-LD
│   ├── location.html          # State & district landing directory
│   └── error.html             # 404 & 500 error templates
└── static/
    └── style.css              # Design system stylesheet
```

---

## 1. Local Setup & Execution (Windows PowerShell)

```powershell
# 1. Activate your virtual environment
.\my_env\Scripts\Activate.ps1

# 2. Install requirements
python -m pip install -r requirements.txt

# 3. Apply non-destructive performance indexes (one-time, idempotent)
python migrations/add_seo_indexes.py

# 4. Run automated test suite
python -m unittest tests/test_schoolatlas.py

# 5. Start the application
python app.py
```

Open `http://127.0.0.1:5000` in your web browser.

---

## 2. Production Configuration

Set your public HTTPS domain and database environment variables before deploying:

```env
SITE_URL=https://schoolatlas.in
SCHOOLS_DB_PATH=schools.db
SCHOOLS_TABLE=schools
HOST=0.0.0.0
PORT=5000
```

> **Important**: Never deploy with `SITE_URL` pointing to `localhost` or left empty. Canonical URLs, Open Graph tags, Breadcrumb JSON-LD, and sitemaps will use this absolute public domain.

---

## 3. SEO Architecture & Key Features

### Stable, Search-Friendly URLs
- **Canonical School URL Format**: `/school/<slug>-<identifier>/`
  - Example: `/school/diamond-stone-international-school-33311507105/`
  - Slugs are normalized, deterministic, lowercase, and collision-resistant.
  - Strict trailing slash enforcement (missing trailing slashes 301 redirect).
  - Case normalization (uppercase or mixed-case requests 301 redirect).
- **Legacy URL Backwards Compatibility**:
  - Legacy format `/school/<slug>/<key>` permanently redirects (301) to canonical.
  - Legacy format `/school/<key>` permanently redirects (301) to canonical.
  - If a school name is updated in the database, old slugs still resolve via the identifier and 301 redirect to the new canonical URL.
  - Unknown identifiers return a genuine HTTP 404 response (never a soft 404 or redirect to home).
  - Duplicate records (e.g. Cambridge duplicates) 301 redirect to the master canonical record.

### Dynamic On-Page SEO
- **Unique HTML Titles**: Generated from verified database fields:
  - Example: `Diamond Stone International School, Krishnagiri, Tamil Nadu | SchoolAtlas`
- **Concise Meta Descriptions**: Truthful, non-promotional descriptions strictly <= 160 characters:
  - Example: `View available details for Diamond Stone International School in Krishnagiri, Tamil Nadu, including recorded school category and location information.`
- **Heading Hierarchy**: One prominent `<h1>` containing the cleaned school name, followed by semantic `<h2>` and `<h3>` tags.
- **Accessible Breadcrumbs**: Semantic `<nav class="breadcrumbs">` linking Home > State/District > School Profile.
- **Social Metadata**: Open Graph tags (`og:title`, `og:description`, `og:url`, `og:site_name`, `og:locale`) and Twitter Cards (`summary_large_image`).

### Schema.org Structured Data
- Injected as `<script type="application/ld+json">` with script-injection safeguards (`</` escaped as `<\/`).
- Includes `@type: School` with verified fields: name, identifier, address, geo coordinates (when valid), telephone, email, and website.
- Includes `@type: BreadcrumbList` linking Home > District Directory > School Profile.

### Technical SEO & Sitemaps
- **`/robots.txt`**: Clean policy allowing public profiles and static assets while disallowing query-string search combinations (`/?*`, `/*?*`) and `/health`. Links to sitemap index.
- **`/sitemap.xml`**: Sitemap index pointing to chunked sub-sitemaps:
  - `/sitemap-static.xml`: Homepage (`/`).
  - `/sitemap-locations.xml`: 907 unique state/district directory pages.
  - `/sitemap-schools-1.xml` to `sitemap-schools-40.xml`: Chunked at 40,000 URLs per sitemap (well below Google's 50,000 URL / 50MB limit).
  - Sitemaps are stream-generated in batches of 1,000 using dedicated connections to maintain O(1) memory usage.
  - Trustworthy `<lastmod>` extracted from `updated_at` without artificial timestamps.

### Large-Scale Indexing Quality Policy
- Total records audited: 1,583,850.
- Indexable records: 1,583,142.
- Excluded records: 708 (702 Cambridge duplicates, 5 invalid formula errors like `#NAME?`, 1 corrupted row).
- Excluded records return `<meta name="robots" content="noindex,follow">` and are omitted from sitemaps.
- Filter combinations (`?q=...`, `?state=...`, `?page=2`) serve `<meta name="robots" content="noindex,follow">`.

---

## 4. Database Migrations & Rollback

### Apply Indexes
```powershell
python migrations/add_seo_indexes.py
```
- Adds `idx_schools_school_id` ON `schools(school_id)`.
- Adds `idx_schools_state_district` ON `schools(state_name, district_name)`.

### Rollback Indexes (Safe & Reversible)
```powershell
python migrations/rollback_seo_indexes.py
```

---

## 5. Verification URLs

- Homepage: `http://127.0.0.1:5000/`
- Target School: `http://127.0.0.1:5000/school/diamond-stone-international-school-33311507105/`
- Legacy URL redirect: `http://127.0.0.1:5000/school/33311507105`
- Location Page: `http://127.0.0.1:5000/schools/tamil-nadu/krishnagiri/`
- Robots.txt: `http://127.0.0.1:5000/robots.txt`
- Sitemap Index: `http://127.0.0.1:5000/sitemap.xml`
- Location Sitemap: `http://127.0.0.1:5000/sitemap-locations.xml`
- School Sitemap Chunk: `http://127.0.0.1:5000/sitemap-schools-1.xml`
- Health Endpoint: `http://127.0.0.1:5000/health`
