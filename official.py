"""Extracts location data from a prospect's official website.

Tries, in order, for each URL:
1. schema.org structured data (LocalBusiness / ItemList in
   <script type="application/ld+json">) on the raw HTML — free, reliable.
2. A hand-written heuristic HTML scraper (`_extract_heuristic`) that looks
   for postal-code+locality patterns as anchors and delimits a "location
   block" around each one — for sites that show real name/address/phone in
   their server-rendered HTML but don't mark it up as schema.org (e.g.
   tiendas.movistar.es).
3. One level of same-domain link crawling (`_crawl_candidate_links`), for
   store-locator index pages that are just a directory of links to
   individual store pages with no address data of their own (e.g. Zara's
   nationwide list of city links) — steps 1 and 2 are retried on every
   same-domain link found on the page, and any that resolve to a real store
   page (schema.org or heuristic match) get folded into this URL's result.
4. Playwright (headless Chromium) rendering, as a last resort for JS-only
   pages where none of the above found anything — then steps 1 and 2 are
   retried on the rendered HTML (crawling is NOT retried here, to bound
   cost — an index page that needs JS just to reveal its own store links is
   an edge case left for later).

Explicitly NOT attempted: bypassing bot protection. If the initial request
itself fails (network error, non-2xx — e.g. Zara returns 403 via Akamai),
that's `'inaccessible'` and none of the above is tried; no scraper can get
past deliberate anti-bot measures without infrastructure this tool doesn't
have, and going further down that road is out of scope by design. Also not
attempted: multi-level crawling (an index → region → city → store
hierarchy) — a previous Firecrawl-based version of this tool tried exactly
that and kept breaking on new sites (see docs/context.md); one level, tried
against every same-domain link and keeping only what actually resolves to a
store page, is deliberately simpler and doesn't need to understand any
site's specific hierarchy.

Every outcome becomes a site-level finding (not tied to any one location):
- moderate: the page was reachable but genuinely has no schema.org markup —
  a real, actionable local-SEO/GEO gap worth flagging to the prospect. Kept
  even when the heuristic scraper, crawl, or Playwright *did* recover the
  data — schema.org's absence is the finding, regardless of whether this
  tool worked around it (and JS-injected JSON-LD isn't reliably seen by
  every crawler/AI assistant either, so that case keeps the finding too).
  This applies per-page: a crawled store page gets its own finding, exactly
  like a URL the user pasted in directly.
- minor: couldn't even access the page — a limitation of this scan, not a
  proven fact about the prospect's site, so it's downgraded and suggests a
  CSV upload instead.

A CSV upload (parse_official_csv) is the other way to supply official data,
for whenever a store locator can't be read this way at all.
"""
import csv
import heapq
import io
import json
import os
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from normalize import address_norm, make_record, name_norm

_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ProspectAuditBot/1.0; internal Localistico tool)'}

CSV_TEMPLATE_HEADER = ['name', 'address', 'phone', 'opening_hours', 'url']
CSV_TEMPLATE_EXAMPLE = ['Zara Pelayo', 'Calle Pelai 58, 08001 Barcelona', '932123456',
                        'Lunes: 10:00–22:00; Martes: 10:00–22:00', '']

_BUSINESS_TYPE_WHITELIST = {
    'LocalBusiness', 'Restaurant', 'Store', 'FoodEstablishment', 'CafeOrCoffeeShop',
    'BarOrPub', 'ClothingStore', 'GroceryStore', 'Bakery', 'Pharmacy', 'Bank',
    'GasStation', 'Hotel', 'ShoppingCenter', 'Supermarket', 'FastFoodRestaurant',
    'AutoDealer', 'HairSalon', 'Gym', 'MedicalClinic',
}
_BUSINESS_TYPE_HINTS = ('business', 'store', 'shop', 'restaurant', 'cafe', 'hotel', 'clinic')

_DAY_MAP = {
    'monday': 'Lunes', 'tuesday': 'Martes', 'wednesday': 'Miércoles',
    'thursday': 'Jueves', 'friday': 'Viernes', 'saturday': 'Sábado', 'sunday': 'Domingo',
}


def parse_official_csv(file_obj):
    """Parses an uploaded CSV of official locations into normalized records.

    Expected columns (case-insensitive, order-independent): name, address
    (or formatted_address), phone (optional), opening_hours (optional — free
    text; only feeds the hours-comparison rules if it matches the
    "Día: HH:MM–HH:MM" format the rest of the app uses), url (optional — a
    link to check this row live, e.g. the prospect's own store page).

    Returns {"locations": [...], "errors": [{"row", "reason"}]}.
    """
    text = file_obj.read()
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig', errors='replace')

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]

    locations, errors = [], []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        name = (row.get('name') or '').strip()
        address = (row.get('address') or row.get('formatted_address') or '').strip()
        if not name or not address:
            errors.append({'url': f'CSV fila {i}', 'reason': 'falta "name" o "address"'})
            continue

        hours = (row.get('opening_hours') or '').strip()
        row_url = (row.get('url') or '').strip() or None
        locations.append(make_record(
            'official', f'csv-row-{i}#{name_norm(name)}',
            name=name, formatted_address=address,
            phone=(row.get('phone') or '').strip() or None,
            opening_hours=[h.strip() for h in hours.split(';') if h.strip()] if hours else None,
            verify_url=row_url,
            raw={'_via': 'csv', '_row': i},
        ))

    return {'locations': _dedupe(locations), 'errors': errors}


def _fold(s):
    """Accent-fold + lowercase WITHOUT stripping punctuation — unlike
    address_norm, this keeps the "(Provincia)" parentheses that let us tell
    the city apart from the province suffix."""
    return unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode('ascii').lower()


# Spanish address shape "... 17001 Girona (Girona)" — capture the locality
# that sits between the 5-digit postal code and the "(Provincia)" suffix.
# That locality is the actual CITY; matching the raw address instead would
# wrongly catch the province suffix (every store in Girona province ends in
# "(Girona)") and street names like "Carrer de Girona" in another city.
_POSTAL_CITY_RE = re.compile(r'\b\d{5}\s+(.+?)\s*\(')


def _location_matches_city(location, city_key):
    """True if `city_key` (an already-normalized city name) is the locality
    of this location. A store locator dumps every store nationwide
    (Movistar's root yields ~800), but an audit is scoped to one city — this
    keeps the official source in the same scope as the Google/Apple/Bing
    city searches."""
    raw = _fold(location.get('formatted_address') or '')
    localities = _POSTAL_CITY_RE.findall(raw)
    if localities:
        return any(re.search(r'\b' + re.escape(city_key) + r'\b', loc) for loc in localities)
    # No "postal City (Province)" shape to anchor on — fall back to a
    # whole-address + name word match (looser, but the province/street
    # false positives above only arise with that Spanish suffix format).
    haystack = f"{address_norm(location.get('formatted_address') or '')} {name_norm(location.get('name') or '')}"
    return re.search(r'\b' + re.escape(city_key) + r'\b', haystack) is not None


def _filter_by_city(locations, city):
    city_key = name_norm(city)
    if not city_key:
        return locations
    return [loc for loc in locations if _location_matches_city(loc, city_key)]


def extract_official(urls, city=None):
    """Fetches each URL and extracts schema.org business locations from it.

    If `city` is given, the returned `locations` are filtered to that city
    (see `_filter_by_city`) so the official source stays scoped to the same
    city as the Google/Apple/Bing searches — without it, a store locator's
    root page dumps every store nationwide. `findings`/`site_analysis` are
    left unfiltered: they describe the store locator's structured-data
    quality (a site-level property), not per-city locations.

    Returns {"locations": [...], "errors": [{"url", "reason"}],
             "findings": [{"url", "severity", "message"}],
             "site_analysis": [{"url", "status", "location_count", "page_type"}]}
    — `findings` are site-level (not tied to any specific location), see
    module docstring. `site_analysis` has one entry per URL the user pasted
    in, PLUS one entry per store page discovered via crawling from it
    (`page_type: 'store_page'`, known for certain, not inferred) — a crawled
    page is only ever included here if it actually resolved to a real
    location (schema.org or heuristic match); links that turned out
    irrelevant (nav, legal, blog...) are silently dropped rather than
    reported as "missing schema," which would just be noise. For URLs that
    weren't crawled, `page_type` is still inferred from result count
    ('index' if it yielded more than one location, 'store_page' otherwise).
    """
    locations, errors, findings, site_analysis = [], [], [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = pool.map(lambda u: _extract_one(u, city=city), urls)
        for url, (found, error, status, sub_analyses) in zip(urls, results):
            if error:
                errors.append({'url': url, 'reason': error})
            locations.extend(found)

            # A URL is an index/listing page — not an individual store page —
            # either when it needed crawling to find any data at all
            # (sub_analyses non-empty) or when it directly listed more than
            # one location itself. Its own "missing schema.org" finding
            # would be misleading in that case (e.g. a province-level list of
            # cities is not itself a location page); each real store page
            # found via crawling already gets its own finding below.
            is_index_page = bool(sub_analyses) or len(found) > 1
            site_analysis.append({
                'url': url, 'status': status, 'location_count': len(found),
                'page_type': 'index' if is_index_page else 'store_page',
            })
            site_analysis.extend(sub_analyses)

            if not is_index_page:
                findings.append(_finding_for(url, status))
            # Only crawled sub-pages that are individual store pages get a
            # finding — a crawled sub-page that is itself a listing/index
            # (page_type 'index', e.g. a per-province page listing many
            # stores) is not a location page and must not be flagged.
            for sub in sub_analyses:
                if sub.get('page_type') != 'index':
                    findings.append(_finding_for(sub['url'], sub['status']))

    findings = [f for f in findings if f is not None]
    deduped = _dedupe(locations)
    scoped = _filter_by_city(deduped, city) if city else deduped
    return {'locations': scoped, 'errors': errors, 'findings': findings,
            'site_analysis': site_analysis,
            'locator_report': build_locator_report(list(urls), site_analysis, scoped)}


# --- Informe del store locator (los 4 checks de la tarjeta del mockup) ------

def build_locator_report(urls, site_analysis, locations):
    """Resume la calidad del store locator en los 4 checks que muestra la UI:
    JSON-LD/schema LocalBusiness, completitud del marcado (horario/teléfono/geo),
    cobertura de páginas individuales por sede, y presencia de sitemap. Se
    deriva de lo ya extraído (site_analysis + locations) + una comprobación de
    sitemap best-effort. `has_data=False` cuando no se aportó ninguna URL."""
    if not urls:
        return {'has_data': False, 'root_url': None, 'checks': [], 'optimized': None}

    root = urls[0]
    reachable = [s for s in site_analysis if s.get('status') != 'inaccessible']
    any_schema = any(s.get('status') == 'found' for s in site_analysis)

    # Sedes con página individual propia (URL distinta de las raíz pegadas) vs
    # total de sedes con dato oficial en esta ciudad.
    root_set = set(urls)
    individual = [l for l in locations if l.get('verify_url') and l['verify_url'] not in root_set]
    coverage_x, coverage_n = len(individual), len(locations)

    completeness = _schema_completeness(
        [l for l in locations if (l.get('raw') or {}).get('_via') not in ('heuristic', 'csv')])
    sitemap = _analyze_sitemap(root)

    def check(key, label, status, detail):
        return {'key': key, 'label': label, 'status': status, 'detail': detail}

    checks = [
        check('jsonld_localbusiness', 'Datos estructurados LocalBusiness en las store pages',
              'ok' if any_schema else ('bad' if reachable else 'na'),
              'presentes' if any_schema else ('no encontrados' if reachable else 'no analizado')),
        check('schema_completeness', 'Marcado schema.org completo (horario, teléfono, geo)',
              completeness['status'], completeness['detail']),
        check('store_pages_coverage', 'Página individual por sede (indexable)',
              _coverage_status(coverage_x, coverage_n),
              f'{coverage_x} de {coverage_n}' if coverage_n else 'sin sedes'),
        check('sitemap_present', 'Sitemap con las store pages',
              'ok' if sitemap['present'] else 'bad',
              (f"sí ({sitemap['store_like']} URLs de sede)" if sitemap['store_like']
               else 'sí') if sitemap['present'] else 'no encontrado'),
    ]
    optimized = all(c['status'] == 'ok' for c in checks)
    # `analyzable` = se pudo acceder al menos a una página del locator. Si no
    # (todo inaccesible), la UI/PDF muestran "No se puede analizar" en vez de
    # "No optimizado" (no es que esté mal optimizado, es que no lo pudimos leer).
    return {'has_data': True, 'root_url': root, 'checks': checks,
            'optimized': optimized, 'analyzable': bool(reachable)}


def _coverage_status(x, n):
    if not n:
        return 'na'
    ratio = x / n
    return 'ok' if ratio >= 0.9 else ('warn' if ratio >= 0.5 else 'bad')


def _schema_completeness(schema_locations):
    """¿El schema.org recuperado trae horario/teléfono/geo? 'ok' si la mayoría
    de las sedes con schema tienen los tres, 'warn' si alguno, 'bad' si ninguno,
    'na' si no hay ninguna sede con schema."""
    n = len(schema_locations)
    if not n:
        return {'status': 'na', 'detail': 'sin schema para evaluar'}
    have_hours = sum(1 for l in schema_locations if l.get('opening_hours'))
    have_phone = sum(1 for l in schema_locations if l.get('phone'))
    have_geo = sum(1 for l in schema_locations if l.get('lat') is not None and l.get('lng') is not None)
    fields_ok = sum(1 for c in (have_hours, have_phone, have_geo) if c >= n * 0.5)
    status = 'ok' if fields_ok == 3 else ('warn' if fields_ok >= 1 else 'bad')
    missing = [name for name, c in (('horario', have_hours), ('teléfono', have_phone), ('geo', have_geo))
               if c < n * 0.5]
    detail = 'completo' if not missing else 'falta ' + ', '.join(missing)
    return {'status': status, 'detail': detail}


_SITEMAP_FETCH_TIMEOUT = 10
_SITEMAP_MAX_CHILDREN = 3


def _analyze_sitemap(root_url):
    """Best-effort: ¿hay sitemap.xml (vía robots.txt o rutas habituales) y
    cuántas URLs parecen fichas de sede? Nunca lanza — degrada a
    {'present': False}."""
    result = {'present': False, 'url': None, 'total_urls': 0, 'store_like': 0}
    if os.environ.get('DISABLE_SITEMAP_FETCH', '').strip() == '1':
        return result
    try:
        parsed = urlparse(root_url)
        base = f'{parsed.scheme}://{parsed.netloc}'
        candidates = _sitemap_candidates(base)
        for sm_url in candidates:
            locs = _fetch_sitemap_locs(sm_url, depth=0)
            if locs:
                result['present'] = True
                result['url'] = sm_url
                result['total_urls'] = len(locs)
                result['store_like'] = sum(1 for u in locs if _looks_like_store_url(u))
                break
    except Exception:
        pass
    return result


def _sitemap_candidates(base):
    candidates = []
    try:
        r = requests.get(urljoin(base + '/', 'robots.txt'), headers=_HEADERS, timeout=_SITEMAP_FETCH_TIMEOUT)
        if r.ok:
            for line in r.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    candidates.append(line.split(':', 1)[1].strip())
    except requests.RequestException:
        pass
    candidates.append(urljoin(base + '/', 'sitemap.xml'))
    candidates.append(urljoin(base + '/', 'sitemap_index.xml'))
    # de-dupe preservando orden
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_sitemap_locs(sm_url, depth):
    """URLs <loc> de un sitemap; si es un índice de sitemaps, sigue hasta
    _SITEMAP_MAX_CHILDREN hijos (un solo nivel)."""
    try:
        r = requests.get(sm_url, headers=_HEADERS, timeout=_SITEMAP_FETCH_TIMEOUT)
    except requests.RequestException:
        return []
    if not r.ok or '<' not in r.text:
        return []
    locs = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', r.text, flags=re.IGNORECASE)
    is_index = '<sitemapindex' in r.text.lower()
    if is_index and depth == 0:
        child_locs = []
        for child in locs[:_SITEMAP_MAX_CHILDREN]:
            child_locs.extend(_fetch_sitemap_locs(child, depth + 1))
        return child_locs
    return locs


def _looks_like_store_url(url):
    """Heurística: una ficha de sede suele colgar de una ruta con >=2 segmentos
    (p.ej. /tiendas/barcelona-pelayo), no la home ni una sección de primer nivel."""
    path = urlparse(url).path.strip('/')
    if not path:
        return False
    return len([seg for seg in path.split('/') if seg]) >= 2


def _finding_for(url, status):
    if status in ('no_schema', 'found_heuristic'):
        return {'url': url, 'severity': 'moderate',
            'message': 'El store locator es accesible pero no tiene datos estructurados schema.org '
                       '(LocalBusiness) — recomendable implementarlo para mejorar la visibilidad en '
                       'buscadores y asistentes de IA (SEO local / GEO).'}
    if status == 'inaccessible':
        return {'url': url, 'severity': 'minor',
            'message': 'No hemos podido acceder automáticamente a este store locator (bloqueo o '
                       'error de acceso). Prueba a subir un CSV con las sedes en su lugar.'}
    return None


def _extract_one(url, city=None):
    """Returns (locations, error, status, sub_analyses). `status` is one of:
    - 'found': real schema.org markup was present in the server-rendered HTML.
    - 'found_heuristic': recovered via the heuristic HTML scraper, crawling
      same-domain links, or Playwright (its own heuristic pass, or schema.org
      that only appeared after JS execution) — still a real finding, see
      module docstring.
    - 'no_schema': page reachable, nothing usable found by any method.
    - 'inaccessible': couldn't reach the page at all — no fallback attempted.

    When `city` is given, crawling is the city-guided deep crawl
    (`_crawl_for_city_stores`) that walks down to individual store detail
    pages for that city; without it, the single-level `_crawl_candidate_links`
    is used. `sub_analyses` carries one entry per crawled page that resolved
    to a location, for extract_official() to report schema status per page.
    """
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
    except requests.RequestException as e:
        return [], f'no se pudo acceder a la URL ({e})', 'inaccessible', []

    if not r.ok:
        return [], f'no se pudo acceder a la URL (HTTP {r.status_code})', 'inaccessible', []

    businesses = _schema_businesses_from_html(r.text)
    if businesses:
        return [_map_node(n, url) for n in businesses], None, 'found', []

    heuristic = _extract_heuristic(r.text, url)
    if heuristic:
        return heuristic, None, 'found_heuristic', []

    crawled_locations, sub_analyses = (
        _crawl_for_city_stores(r.text, url, city) if city else _crawl_candidate_links(r.text, url))
    if crawled_locations:
        return crawled_locations, None, 'found_heuristic', sub_analyses

    rendered_html, _render_error = _render_with_playwright(url)
    if rendered_html:
        businesses = _schema_businesses_from_html(rendered_html)
        if businesses:
            return [_map_node(n, url) for n in businesses], None, 'found_heuristic', []
        heuristic = _extract_heuristic(rendered_html, url)
        if heuristic:
            return heuristic, None, 'found_heuristic', []

    return [], 'sin datos estructurados schema.org (LocalBusiness/ItemList) en el HTML', 'no_schema', []


def _schema_businesses_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    nodes = []
    for tag in soup.find_all('script', {'type': 'application/ld+json'}):
        if not tag.string:
            continue
        try:
            parsed = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        nodes.extend(_flatten_jsonld(parsed))
    # Además de JSON-LD, recoge negocios marcados como microdata/RDFa (extruct)
    # — muchos sitios usan itemscope/itemtype en lugar de JSON-LD, y sin esto se
    # reportarían como "sin schema.org" cuando sí lo tienen en otro formato.
    nodes.extend(_extruct_extra_nodes(html))
    return [n for n in nodes if _looks_like_business(n)]


def _extruct_extra_nodes(html):
    """LocalBusiness marcado como microdata o RDFa, vía extruct, normalizado a
    la misma forma que un nodo JSON-LD (uniform=True da @type + propiedades
    como claves de nivel superior). Best-effort: si extruct no está o falla,
    devuelve [] y solo se usa JSON-LD."""
    try:
        import extruct
    except ImportError:
        return []
    try:
        data = extruct.extract(html, syntaxes=['microdata', 'rdfa'], uniform=True)
    except Exception:
        return []
    nodes = []
    for syntax in ('microdata', 'rdfa'):
        for item in data.get(syntax) or []:
            nodes.extend(_flatten_jsonld(item))
    return nodes


def _render_with_playwright(url):
    """Last-resort rendering for JS-only pages. Any failure (Chromium not
    installed, timeout, crash) degrades to (None, message) — this is an
    optional final step in _extract_one and must never raise."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, 'Playwright no está instalado'

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            try:
                page = browser.new_page(user_agent=_HEADERS['User-Agent'])
                try:
                    page.goto(url, wait_until='networkidle', timeout=20000)
                except PlaywrightTimeoutError:
                    page.goto(url, wait_until='load', timeout=10000)
                html = page.content()
            finally:
                browser.close()
        return html, None
    except Exception as e:
        return None, f'no se pudo renderizar con navegador ({e})'


_MAX_CANDIDATE_LINKS = 80
_CRAWL_NAV_LINK_TEXT = {
    'inicio', 'home', 'contacto', 'contact', 'nosotros', 'sobre nosotros', 'about', 'about us',
    'legal', 'aviso legal', 'privacidad', 'privacy', 'cookies', 'blog', 'noticias', 'news',
    'empleo', 'careers', 'trabaja con nosotros', 'ayuda', 'help', 'faq', 'preguntas frecuentes',
    'iniciar sesión', 'login', 'registro', 'sign up', 'carrito', 'cart', 'mi cuenta', 'my account',
}


def _discover_links(html, source_url):
    """Same-domain links on `html`, capped and filtered to skip obvious
    non-store nav (home/legal/blog/login/...) — deliberately not filtered by
    URL pattern beyond that, since store-locator URL shapes vary too much
    per site to guess reliably. The candidates are tried and discarded by
    `_crawl_candidate_links`, not pre-judged here beyond this coarse pass."""
    soup = BeautifulSoup(html, 'html.parser')
    base_domain = urlparse(source_url).netloc.lower().removeprefix('www.')
    source_clean = urlparse(source_url)._replace(fragment='').geturl()

    seen, links = {source_clean}, []
    for a in soup.find_all('a', href=True):
        if len(links) >= _MAX_CANDIDATE_LINKS:
            break
        href = urljoin(source_url, a['href'])
        parsed = urlparse(href)
        if parsed.scheme not in ('http', 'https'):
            continue
        if parsed.netloc.lower().removeprefix('www.') != base_domain:
            continue

        text = a.get_text(strip=True).lower()
        if text in _CRAWL_NAV_LINK_TEXT or text in _GENERIC_LINK_TEXT:
            continue

        clean = parsed._replace(fragment='').geturl()
        if clean in seen:
            continue
        seen.add(clean)
        links.append(clean)

    return links


def _crawl_candidate_links(html, source_url):
    """One level of crawling: tries schema.org then the heuristic scraper
    (no Playwright at this nested level, to bound cost) against every
    same-domain link found on `html`, for store-locator index pages that
    are just a directory of links with no address data of their own (e.g.
    Zara's nationwide list of city links). Returns (locations,
    sub_analyses) — sub_analyses only ever contains links that actually
    resolved to a real location; irrelevant links (most of them, on a real
    page) are silently discarded rather than reported as "missing schema,"
    which would just be noise about pages that were never store pages."""
    links = _discover_links(html, source_url)
    if not links:
        return [], []

    def _try_link(link):
        try:
            r = requests.get(link, headers=_HEADERS, timeout=15)
        except requests.RequestException:
            return link, [], None
        if not r.ok:
            return link, [], None

        businesses = _schema_businesses_from_html(r.text)
        if businesses:
            return link, [_map_node(n, link) for n in businesses], 'found'

        heuristic = _extract_heuristic(r.text, link)
        if heuristic:
            return link, heuristic, 'found_heuristic'

        return link, [], None

    locations, sub_analyses = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for link, found, status in pool.map(_try_link, links):
            if found:
                locations.extend(found)
                # A crawled link that yields more than one location is itself
                # a listing/index page (e.g. Movistar's per-province pages
                # like /alava, each listing 8+ stores), not an individual
                # store page — so it's page_type 'index' and gets NO
                # "missing schema.org" finding (see extract_official). Only a
                # link resolving to a single location is a real store page.
                sub_analyses.append({'url': link, 'status': status,
                                      'location_count': len(found),
                                      'page_type': 'index' if len(found) > 1 else 'store_page'})

    return locations, sub_analyses


# --- City-guided deep crawl (reaches individual store pages) ---------------
#
# Movistar-style locators nest 3 levels deep: root (provinces) → province
# (cities) → city (individual store detail pages). The single-level
# `_crawl_candidate_links` above only reaches the province listing and scrapes
# its inline addresses (no hours, and every store's verify_url points at the
# listing). The individual store pages, in contrast, carry full schema.org
# LocalBusiness markup (hours, geo, phone) and their own canonical URL. When
# the audit is scoped to a city, this crawl walks down toward that city and
# collects those individual pages instead — bounded by a fetch budget so it
# never re-scans all of Spain.

_CITY_CRAWL_MAX_FETCHES = 120
_CITY_CRAWL_MAX_DEPTH = 4


def _discover_links_with_text(html, source_url):
    """Like `_discover_links` but returns (url, anchor_text) pairs — the
    anchor text is a useful city-match signal alongside the URL slug.

    Unlike `_discover_links`, this does NOT drop `_GENERIC_LINK_TEXT`
    ("Más detalles" / "Ver ficha" / "Ver tienda"): in a store locator those
    generic links are precisely the per-store detail links this crawl is
    trying to reach (e.g. Movistar's store cards all link out via a "Más
    detalles" anchor). Only true site nav (home/legal/login) is skipped."""
    soup = BeautifulSoup(html, 'html.parser')
    base_domain = urlparse(source_url).netloc.lower().removeprefix('www.')
    source_clean = urlparse(source_url)._replace(fragment='').geturl()

    seen, links = {source_clean}, []
    for a in soup.find_all('a', href=True):
        if len(links) >= _MAX_CANDIDATE_LINKS:
            break
        href = urljoin(source_url, a['href'])
        parsed = urlparse(href)
        if parsed.scheme not in ('http', 'https'):
            continue
        if parsed.netloc.lower().removeprefix('www.') != base_domain:
            continue
        text = a.get_text(strip=True)
        if text.lower() in _CRAWL_NAV_LINK_TEXT:
            continue
        clean = parsed._replace(fragment='').geturl()
        if clean in seen:
            continue
        seen.add(clean)
        links.append((clean, text))
    return links


def _city_link_priority(url, text, city_key):
    """Lower = crawl sooner. 0: the URL's own last path segment or anchor
    text names the city (a direct step to/at the city). 1: the city appears
    elsewhere in the path (e.g. a store slug under `/barcelona/barcelona/`).
    2: no city signal — only followed while still scanning the hierarchy for
    the city's branch (from shallow levels), never once the city is found."""
    path = urlparse(url).path
    last = name_norm(path.rstrip('/').split('/')[-1].replace('-', ' '))
    full = name_norm(path.replace('-', ' ').replace('/', ' '))
    tnorm = name_norm(text or '')
    word = lambda s: re.search(r'\b' + re.escape(city_key) + r'\b', s) is not None
    if word(last) or word(tnorm):
        return 0
    if word(full):
        return 1
    return 2


def _crawl_for_city_stores(html, source_url, city):
    """City-guided, budget-bounded BFS that collects individual store pages
    (schema.org LocalBusiness) for the audited city. Falls back to the
    single-level `_crawl_candidate_links` when no city is given. Returns
    (locations, sub_analyses) — the same shape as `_crawl_candidate_links`.
    Prefers schema.org store records (rich: hours + own verify_url); only if
    none are reached anywhere does it fall back to the heuristic listing
    records. Off-city stores that get swept in are trimmed later by
    `_filter_by_city`."""
    city_key = name_norm(city)
    if not city_key:
        return _crawl_candidate_links(html, source_url)

    seen = {urlparse(source_url)._replace(fragment='').geturl()}
    frontier, counter = [], 0

    def push(url, text, depth):
        nonlocal counter
        if url in seen:
            return
        seen.add(url)
        heapq.heappush(frontier, (_city_link_priority(url, text, city_key), depth, counter, url))
        counter += 1

    for url, text in _discover_links_with_text(html, source_url):
        push(url, text, 1)

    schema_records, heuristic_fallback, sub_analyses = [], [], []
    found_city_store = False
    fetches = 0

    while frontier and fetches < _CITY_CRAWL_MAX_FETCHES:
        priority, depth, _, url = heapq.heappop(frontier)
        # Priority-2 links (no city signal) exist only to discover the city's
        # branch from the top — stop chasing them once we've found the city's
        # stores, or below the province level, so we don't crawl all of Spain.
        if priority == 2 and (found_city_store or depth > 1):
            continue
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        fetches += 1

        businesses = _schema_businesses_from_html(r.text)
        if businesses:
            recs = [_map_node(n, url) for n in businesses]
            schema_records.extend(recs)
            sub_analyses.append({'url': url, 'status': 'found', 'location_count': len(recs),
                                  'page_type': 'store_page' if len(recs) == 1 else 'index'})
            if any(_location_matches_city(rec, city_key) for rec in recs):
                found_city_store = True
            continue  # a store detail page is a leaf — don't descend from it

        heuristic = _extract_heuristic(r.text, url)
        if heuristic:
            heuristic_fallback.extend(heuristic)
            sub_analyses.append({'url': url, 'status': 'found_heuristic', 'location_count': len(heuristic),
                                  'page_type': 'index' if len(heuristic) > 1 else 'store_page'})
        if depth < _CITY_CRAWL_MAX_DEPTH:
            for link, text in _discover_links_with_text(r.text, url):
                push(link, text, depth + 1)

    locations = schema_records if schema_records else heuristic_fallback
    return locations, sub_analyses


_POSTAL_LOCALITY_RE = re.compile(
    r'\b(?:0[1-9]|[1-4]\d|5[0-2])\d{3}\b\s+'
    r'[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*){0,3}'
)
_PHONE_RE = re.compile(r'(?:\+34|0034)?[\s.\-]?[6789]\d{2}[\s.\-]?\d{2,3}[\s.\-]?\d{2,3}[\s.\-]?\d{2,3}\b')
_GENERIC_LINK_TEXT = {
    'más detalles', 'mas detalles', 'ver más', 'ver mas', 'cómo llegar', 'como llegar',
    'more details', 'details', 'ver ficha', 'ver tienda', 'more info', 'más información',
}
_MAX_BLOCK_DEPTH = 8
_MAX_BLOCK_TEXT_LEN = 1000


def _extract_heuristic(html, source_url):
    """Finds location-like blocks in server-rendered HTML that has no
    schema.org markup, anchored on a Spanish postal-code + locality pattern
    (e.g. "08002 Barcelona") — deliberately not tied to any site's CSS
    classes/IDs. See official.py module docstring / docs/plan.md for the
    full algorithm rationale (Movistar's store-locator is the real case this
    was built for: full name/address/phone visible in the raw HTML, just not
    marked up as schema.org).
    """
    soup = BeautifulSoup(html, 'html.parser')
    # Script/style contents are raw source text, not visible page content —
    # without stripping them, a <script> block that builds an address string
    # (common when content is injected via JS, exactly the SPA case this
    # heuristic pass runs for after Playwright rendering) gets matched as if
    # it were a real location block.
    for tag in soup(['script', 'style']):
        tag.decompose()

    anchors = soup.find_all(string=_POSTAL_LOCALITY_RE)
    if not anchors:
        return []

    ancestor_counts = Counter()
    anchor_ancestors = {}
    for anchor in anchors:
        ancestors = list(anchor.parents)
        anchor_ancestors[id(anchor)] = ancestors
        for ancestor in ancestors:
            ancestor_counts[id(ancestor)] += 1

    records, seen_containers = [], set()
    for anchor in anchors:
        container = _pick_block_container(anchor_ancestors[id(anchor)], ancestor_counts)
        if container is None or id(container) in seen_containers:
            continue
        seen_containers.add(id(container))
        record = _map_heuristic(container, source_url)
        if record:
            records.append(record)

    return records


def _pick_block_container(ancestors, ancestor_counts):
    """Climbs from the anchor's immediate parent while the subtree still
    contains exactly this one postal-code anchor — stopping as soon as an
    ancestor would absorb a second one (the natural boundary between
    neighboring location blocks in a listing page), or hitting the depth/
    text-length safety caps (for the single-location-per-page case, where no
    second anchor ever appears to stop the climb)."""
    if not ancestors:
        return None

    container = ancestors[0]
    for depth, ancestor in enumerate(ancestors):
        if depth >= _MAX_BLOCK_DEPTH:
            break
        if ancestor_counts[id(ancestor)] > 1:
            break
        if len(ancestor.get_text(strip=True)) > _MAX_BLOCK_TEXT_LEN:
            break
        container = ancestor
    return container


def _map_heuristic(container, source_url):
    text = container.get_text('\n', strip=True)
    postal_match = _POSTAL_LOCALITY_RE.search(text)
    if not postal_match:
        return None

    name = _find_heuristic_name(container, text)
    if not name or len(name) < 2 or len(name) > 80 or name.strip().isdigit():
        return None

    phone_match = _PHONE_RE.search(text)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    address = _build_address(lines, name)

    return make_record(
        'official', f'{source_url}#heuristic#{name_norm(name)}',
        name=name, formatted_address=address,
        phone=phone_match.group(0) if phone_match else None,
        verify_url=source_url,
        raw={'_source_url': source_url, '_via': 'heuristic'},
    )


def _build_address(lines, name):
    """The street line is usually the line right before the postal-code
    line (e.g. "Plaça de Catalunya, 16" / "08002 Barcelona (Barcelona)") —
    combine them when that preceding line isn't the name or a phone."""
    postal_idx = next((i for i, line in enumerate(lines) if _POSTAL_LOCALITY_RE.search(line)), None)
    if postal_idx is None:
        return None

    parts = []
    if postal_idx > 0:
        prev_line = lines[postal_idx - 1]
        if prev_line != name and not _PHONE_RE.search(prev_line):
            parts.append(prev_line)
    parts.append(lines[postal_idx])
    return ', '.join(parts)


def _find_heuristic_name(container, full_text):
    def usable(candidate):
        return bool(candidate) and not _POSTAL_LOCALITY_RE.search(candidate) and not _PHONE_RE.search(candidate)

    heading = container.find(['h1', 'h2', 'h3', 'h4'])
    if heading:
        text = heading.get_text(strip=True)
        if usable(text):
            return text

    bold = container.find(['strong', 'b'])
    if bold:
        text = bold.get_text(strip=True)
        if usable(text):
            return text

    for link in container.find_all('a'):
        text = link.get_text(strip=True)
        if usable(text) and text.lower() not in _GENERIC_LINK_TEXT and len(text) <= 80:
            return text

    for line in full_text.split('\n'):
        line = line.strip()
        if _POSTAL_LOCALITY_RE.search(line):
            break
        if usable(line) and 2 <= len(line) <= 80:
            return line

    return None


def _flatten_jsonld(node):
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_flatten_jsonld(item))
        return out

    if not isinstance(node, dict):
        return []

    if '@graph' in node:
        return _flatten_jsonld(node['@graph'])

    node_type = node.get('@type')
    types = node_type if isinstance(node_type, list) else [node_type]
    if 'ItemList' in types:
        out = []
        for element in node.get('itemListElement', []):
            item = element.get('item', element) if isinstance(element, dict) else element
            out.extend(_flatten_jsonld(item))
        return out

    return [node]


def _looks_like_business(node):
    node_type = node.get('@type')
    types = [t for t in (node_type if isinstance(node_type, list) else [node_type]) if isinstance(t, str)]

    if any(t in _BUSINESS_TYPE_WHITELIST for t in types):
        return True
    if any(hint in t.lower() for t in types for hint in _BUSINESS_TYPE_HINTS):
        return True

    has_address = bool(node.get('address'))
    has_contact = bool(node.get('telephone') or node.get('openingHoursSpecification'))
    return has_address and has_contact


def _map_node(node, source_url):
    geo = node.get('geo') if isinstance(node.get('geo'), dict) else {}
    node_type = node.get('@type')

    return make_record(
        'official', f'{source_url}#{name_norm(node.get("name"))}',
        name=node.get('name'),
        formatted_address=_format_address(node.get('address')),
        lat=_to_float(geo.get('latitude')), lng=_to_float(geo.get('longitude')),
        # node.get('url') is the business's own url property if schema.org gave one
        # (comparable via R6/R7); falling back to source_url here would compare the
        # scraped page's URL as if it were "their website," which it may not be.
        phone=node.get('telephone'), website=node.get('url'),
        opening_hours=_format_hours(node.get('openingHoursSpecification')),
        category=node_type if isinstance(node_type, str) else None,
        verify_url=source_url,
        raw={'_source_url': source_url, **node},
    )


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _format_address(address):
    if address is None:
        return None
    if isinstance(address, str):
        return address
    if isinstance(address, dict):
        parts = [address.get('streetAddress'), address.get('postalCode'),
                 address.get('addressLocality'), address.get('addressRegion')]
        return ', '.join(p for p in parts if p) or None
    return None


def _day_label(day):
    s = str(day).strip()
    if 'schema.org' in s:
        s = s.rsplit('/', 1)[-1]
    return _DAY_MAP.get(s.lower(), s)


def _format_hours(spec):
    if not spec:
        return None
    if isinstance(spec, dict):
        spec = [spec]

    lines = []
    for entry in spec:
        if not isinstance(entry, dict):
            continue
        days = entry.get('dayOfWeek', [])
        days = days if isinstance(days, list) else [days]
        opens, closes = entry.get('opens'), entry.get('closes')
        if not (opens and closes):
            continue
        for day in days:
            lines.append(f'{_day_label(day)}: {opens}–{closes}')

    return lines or None


def _dedupe(locations):
    seen, out = set(), []
    for loc in locations:
        key = (loc['name_norm'], address_norm(loc['formatted_address']))
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
    return out
