"""Google Maps signals (recent reviews + action links) via SerpApi — the API
replacement for the old Playwright scraper (google_reviews_scraper.py).

Why: scraping Google Maps with Chromium was the audit's slowest (~30–45s per
location) and most fragile part (DOM changes, CAPTCHAs). SerpApi's
`google_maps_reviews` engine returns reviews with REAL timestamps
(`iso_date`) and the owner's `response`, and `google_maps` (place) exposes
the order/reserve/menu action links — reliably, in ~1s, with no browser.

Returns the SAME shape the audit already consumes (app.py:_attach_scraped_
reviews → venue_metrics) so nothing downstream changes:
    {'reviews': [{'author_name', 'rating', 'text',
                  'relative_time_description', 'time', 'has_owner_reply'}],
     'action_links': [{'type', 'label'}],
     'posts': []}                      # Posts aren't exposed by the API → []

Opening hours are NOT part of this — those already come from the Google
Places Details API (weekday_text), unchanged.

Best-effort and never raises: any failure returns whatever was gathered
(possibly all-empty). Costs are bounded: reviews paginate only until the
3-month cutoff OR `SERPAPI_REVIEWS_MAX_PAGES` pages, whichever comes first.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

_SERPAPI_URL = 'https://serpapi.com/search'
_HTTP_ATTEMPTS = 3


def _reviews_max_pages():
    # 3 páginas por sede (antes 5): recorta el mayor consumidor de SerpApi sin
    # perder apenas cobertura del ventana de 3 meses. Configurable por env.
    try:
        return max(1, int(os.environ.get('SERPAPI_REVIEWS_MAX_PAGES', '3')))
    except ValueError:
        return 3


def _key():
    return os.environ.get('SERPAPI_KEY', '').strip()


def _cutoff_epoch(months, now=None):
    now = now or datetime.now(timezone.utc)
    return int((now - timedelta(days=30 * months)).timestamp())


def _get_json(session, params):
    """GET with a few retries on transient failures (SerpApi occasionally
    503s / throttles rapid identical calls, which was truncating review
    pagination). Returns parsed JSON or None."""
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            r = session.get(_SERPAPI_URL, params=params, timeout=20)
            if r.ok:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < _HTTP_ATTEMPTS - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < _HTTP_ATTEMPTS - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
    return None


def _iso_to_epoch(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())
    except (ValueError, AttributeError):
        return None


def _map_review(raw):
    """SerpApi google_maps_reviews item → the audit's review dict shape."""
    return {
        'author_name': ((raw.get('user') or {}).get('name')) or None,
        'rating': int(round(raw['rating'])) if raw.get('rating') is not None else None,
        'text': (raw.get('snippet') or raw.get('extracted_snippet') or '').strip(),
        'relative_time_description': (raw.get('date') or '').strip(),
        'time': _iso_to_epoch(raw.get('iso_date')),
        'has_owner_reply': bool(raw.get('response')),
    }


def _action_links_from_place(place):
    """Maps SerpApi place fields to the audit's action_links shape. These are
    explicit structured fields (no DOM keyword guessing needed)."""
    links = []
    if place.get('reservation'):
        links.append({'type': 'reservation', 'label': 'Reservar'})
    if place.get('order_online_link') or place.get('order_online'):
        links.append({'type': 'order', 'label': 'Pedir online'})
    if place.get('menu'):
        links.append({'type': 'menu', 'label': 'Menú'})
    return links


def _fetch_reviews(place_id, cutoff, session):
    reviews, token, pages = [], None, 0
    while pages < _reviews_max_pages():
        params = {'engine': 'google_maps_reviews', 'place_id': place_id,
                  'hl': 'es', 'sort_by': 'newestFirst', 'api_key': _key()}
        if token:
            params['next_page_token'] = token
        data = _get_json(session, params)
        if data is None:
            break
        page = data.get('reviews') or []
        pages += 1
        # Filter to the 3-month window rather than breaking at the first old
        # review: with sort_by=newestFirst a page can still start with an
        # out-of-order pinned/old review (seen live: a 2024 review atop an
        # otherwise-recent page), which an early break would wrongly treat as
        # end-of-window and drop the rest of the page. Keep in-window (and
        # undated) reviews; stop only when a whole page has none.
        in_window = [m for m in (_map_review(r) for r in page)
                     if m['time'] is None or m['time'] >= cutoff]
        reviews.extend(in_window)
        if not page or not in_window:
            break
        token = ((data.get('serpapi_pagination') or {}).get('next_page_token'))
        if not token:
            break
    return reviews


def _fetch_action_links(place_id, session):
    data = _get_json(session, {
        'engine': 'google_maps', 'type': 'place', 'place_id': place_id,
        'hl': 'es', 'api_key': _key()})
    if data is None:
        return []
    return _action_links_from_place(data.get('place_results') or {})


def fetch_place_signals(place_id, *, months=3, session=None):
    """Reviews (last ~`months`) + action links for a Google place, via
    SerpApi. Never raises — returns {'reviews', 'action_links', 'posts':[]}
    with whatever was gathered."""
    empty = {'reviews': [], 'action_links': [], 'posts': []}
    if not _key() or not place_id:
        return empty
    sess = session or requests
    cutoff = _cutoff_epoch(months)
    try:
        reviews = _fetch_reviews(place_id, cutoff, sess)
    except Exception:
        reviews = []
    action_links = _fetch_action_links(place_id, sess)
    return {'reviews': reviews, 'action_links': action_links, 'posts': []}
