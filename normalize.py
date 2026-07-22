"""Unified location schema + normalization helpers shared by every data source."""
import re
import unicodedata
from urllib.parse import quote

_ADDRESS_ABBREVIATIONS = {
    # trailing \b would never match here: '/' is itself a non-word char, so
    # there's no word-boundary between it and the following space/EOL.
    r'\bc/(?=\s|$)': 'calle',
    r'\bcl\.?\b': 'calle',
    r'\bav\.?\b': 'avenida',
    r'\bavda\.?\b': 'avenida',
    r'\bpl\.?\b': 'plaza',
    r'\bpg\.?\b': 'paseo',
    r'\bpo\.?\b': 'paseo',
    r'\bctra\.?\b': 'carretera',
    # Google often returns Catalan street types for Barcelona addresses
    # ("Carrer de Pelai") while other sources use Spanish ("Calle Pelai") —
    # without this, addr_sim tanks even for the exact same street.
    r'\bcarrer\b': 'calle',
    r'\bavinguda\b': 'avenida',
    r'\bpasseig\b': 'paseo',
    r'\bplaca\b': 'plaza',
    r'\bronda\b': 'ronda',
    r'\bcarretera\b': 'carretera',
}


def name_norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


def address_norm(s):
    if not s:
        return ''
    # Expand abbreviations (e.g. "C/" -> "calle") before name_norm strips
    # punctuation — otherwise "C/" becomes "c" and never matches \bc/\b.
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii').lower()
    for pattern, repl in _ADDRESS_ABBREVIATIONS.items():
        s = re.sub(pattern, repl, s)
    return name_norm(s)


def phone_norm(s):
    if not s:
        return None
    digits = re.sub(r'\D', '', s)
    if digits.startswith('0034'):
        digits = digits[2:]
    elif not digits.startswith('34') and len(digits) == 9:
        digits = '34' + digits
    return digits or None


def website_norm(s):
    if not s:
        return None
    s = re.sub(r'^https?://', '', s.strip().lower())
    s = re.sub(r'^www\.', '', s)
    s = s.split('/')[0]
    return s or None


_DAY_INDEX = {
    'monday': 0, 'lunes': 0, 'dilluns': 0,
    'tuesday': 1, 'martes': 1, 'dimarts': 1,
    'wednesday': 2, 'miercoles': 2, 'dimecres': 2,
    'thursday': 3, 'jueves': 3, 'dijous': 3,
    'friday': 4, 'viernes': 4, 'divendres': 4,
    'saturday': 5, 'sabado': 5, 'dissabte': 5,
    'sunday': 6, 'domingo': 6, 'diumenge': 6,
}
_TIME_RE = re.compile(r'(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?', re.IGNORECASE)
_CLOSED_WORDS = ('cerrado', 'closed', 'tancat')


def _day_index(label):
    key = unicodedata.normalize('NFKD', label.strip().lower()).encode('ascii', 'ignore').decode('ascii')
    return _DAY_INDEX.get(key)


def _to_minutes(match):
    h, m, ampm = match
    h, m = int(h), int(m)
    if ampm:
        ampm = ampm.lower().replace('.', '')
        if ampm == 'pm' and h != 12:
            h += 12
        if ampm == 'am' and h == 12:
            h = 0
    return h * 60 + m


def parse_hours(lines):
    """Parses ["Lunes: 09:00–22:00", "Monday: Closed", ...] into
    {day_index(0=Monday..6=Sunday): 'closed' | [(open_min, close_min), ...] | None}.

    Keyed by day index rather than the localized label so schedules from
    sources returning different languages (e.g. Google in English, official
    data in Spanish) can still be compared day-by-day.
    """
    schedule = {}
    for line in lines or []:
        if ':' not in line:
            continue
        day_label, rest = line.split(':', 1)
        day_idx = _day_index(day_label)
        if day_idx is None:
            continue
        rest = rest.strip()
        if any(w in rest.lower() for w in _CLOSED_WORDS):
            schedule[day_idx] = 'closed'
            continue
        ranges = []
        for part in re.split(r',\s*', rest):
            times = _TIME_RE.findall(part)
            if len(times) >= 2:
                ranges.append((_to_minutes(times[0]), _to_minutes(times[1])))
        schedule[day_idx] = ranges or None
    return schedule


def google_maps_url(place_id, name):
    """Deep-links to a specific place by place_id — Google's documented format
    for opening the exact result rather than a generic name search."""
    if not place_id:
        return None
    return f'https://www.google.com/maps/search/?api=1&query={quote(name or "sede")}&query_place_id={place_id}'


def apple_maps_url(lat, lng, name):
    if lat is None or lng is None:
        return None
    return f'https://maps.apple.com/?ll={lat},{lng}&q={quote(name or "sede")}'


def bing_maps_url(lat, lng, name):
    """Azure Maps has no consumer-facing web map to deep-link into — Bing Maps
    is the closest human-checkable surface backed by the same Microsoft
    mapping stack, so it's used as a stand-in "verify live" link for Azure results.

    Must use the `/maps/search` path with a real `q=` — confirmed live that
    plain `/maps?sp=point...` (no `q=`, no `/search`) never opens a place
    card: Bing has nothing to resolve without a search query, so it silently
    falls back to some unrelated default map center instead. `sp=`/`cp=` are
    kept (matching what a real Bing Maps search URL produces) so the pin
    lands exactly on the known point rather than wherever Bing's own name
    search would resolve to.
    """
    if lat is None or lng is None:
        return None
    label = quote(name or "sede")
    return f'https://www.bing.com/maps/search?q={label}&sp=point.{lat}_{lng}_{label}&cp={lat}~{lng}&lvl=16&style=r'


def google_search_url(query):
    """Generic text search (no place_id) — used to verify the ABSENCE of a
    venue on Google Maps, since there's no record to deep-link to."""
    return f'https://www.google.com/maps/search/?api=1&query={quote(query or "")}'


def apple_search_url(query, lat=None, lng=None, name=None):
    """Text search on Apple Maps, for the same "verify it's really not
    there" purpose as google_search_url.

    Anchors on `ll=<lat,lng>` + a name-only `q=` when the cluster has
    coordinates from some other source. Confirmed live: an unanchored
    `q=<name> <address>` query can get its address text mis-parsed by
    Apple's own fallback address-geocoder when the business name itself
    contains a street-like fragment (a real case: "Tienda Movistar Ciutat
    d'Asunción" got redirected to a wrong street called "Ciutat d'Asunción"
    instead of the real address) — anchoring by coordinate and dropping the
    address from the query text avoids feeding it that ambiguous blob.
    Falls back to the plain `query` (name + address/city, no anchor) when no
    coordinates are available at all.
    """
    if lat is not None and lng is not None:
        return f'https://maps.apple.com/?ll={lat},{lng}&q={quote(name or query or "sede")}'
    return f'https://maps.apple.com/?q={quote(query or "")}'


def bing_search_url(query):
    """Text search on Bing Maps, for the same purpose. Must use the
    `/maps/search` path — confirmed live that plain `/maps?q=...` (no
    `/search`) never resolves a place; `/maps/search?q=...` correctly shows
    either a matching result or an empty/irrelevant list when the business
    genuinely isn't there."""
    return f'https://www.bing.com/maps/search?q={quote(query or "")}'


def make_record(source, source_id, name=None, formatted_address=None, lat=None, lng=None,
                 phone=None, website=None, rating=None, review_count=None,
                 opening_hours=None, category=None, verify_url=None, raw=None):
    """The common shape every source gets normalized into before matching/comparison.

    verify_url is a link a human can open to check this exact record live
    (a maps deep-link for google/apple/azure, or the scraped page itself for
    official records) — distinct from `website`, which is the business's own
    website field used for cross-source comparison (R6/R7).
    """
    return {
        'source': source,
        'source_id': source_id,
        'name': name,
        'name_norm': name_norm(name),
        'formatted_address': formatted_address,
        'address_norm': address_norm(formatted_address),
        'lat': lat,
        'lng': lng,
        'phone': phone_norm(phone),
        'phone_display': phone,
        'website': website_norm(website),
        'website_display': website,
        'rating': rating,
        'review_count': review_count,
        'opening_hours': opening_hours,
        'category': category,
        'verify_url': verify_url,
        'raw': raw if raw is not None else {},
    }


def from_google(place):
    loc = ((place.get('geometry') or {}).get('location')) or {}
    place_id = place.get('place_id')
    return make_record(
        'google', place_id,
        name=place.get('name'), formatted_address=place.get('formatted_address'),
        lat=loc.get('lat'), lng=loc.get('lng'),
        phone=place.get('formatted_phone_number'), website=place.get('website'),
        rating=place.get('rating'), review_count=place.get('user_ratings_total'),
        opening_hours=(place.get('opening_hours') or {}).get('weekday_text'),
        verify_url=google_maps_url(place_id, place.get('name')),
        raw=place,
    )


def from_apple(item):
    # phone/website/opening_hours/rating/review_count are only present when
    # the record was enriched via SerpApi (app.py: _enrich_apple_with_serpapi);
    # Apple's own Server API provides none of them, so they default to None.
    return make_record(
        'apple', item.get('id'),
        name=item.get('name'), formatted_address=item.get('formatted_address'),
        lat=item.get('lat'), lng=item.get('lng'),
        phone=item.get('phone_number'), website=item.get('url'),
        rating=item.get('rating'), review_count=item.get('review_count'),
        opening_hours=item.get('opening_hours'),
        category=item.get('category'),
        verify_url=apple_maps_url(item.get('lat'), item.get('lng'), item.get('name')),
        raw=item,
    )


def from_azure(item):
    return make_record(
        'azure', item.get('id'),
        name=item.get('name'), formatted_address=item.get('formatted_address'),
        lat=item.get('lat'), lng=item.get('lng'),
        phone=item.get('phone_number'), website=item.get('url'),
        opening_hours=item.get('opening_hours'),
        category=item.get('category'),
        verify_url=bing_maps_url(item.get('lat'), item.get('lng'), item.get('name')),
        raw=item,
    )
