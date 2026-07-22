"""Scraper for Google Maps reviews, action links and Posts, up to N months
back for reviews/Posts.

Wired into the audit pipeline via app.py's `_attach_scraped_reviews`, which
attaches this module's output to each Google place as `scraped_reviews`/
`scraped_action_links`/`scraped_posts` before normalization —
venue_metrics.py then uses it (when present) to compute a real
`review_rate_3m`/`reply_rate_3m`/`action_links_google`/`posts_3m` instead of
the ≤5-review Places-API sample or a fixed 'N/D'. Also fully usable
standalone (see the CLI at the bottom) for testing against a single profile
without running a full audit.

Why this exists: the Google Places API used elsewhere in this repo
(`app.py:_search_google`/`_detail`) caps out at 5 reviews with no real
history, and exposes neither action links (Reserve/Order/Menu) nor Posts at
all — limitations explicitly deferred in docs/plan.md's backlog ("would
need a paid third-party scraping service or an in-house review/reply
scraper"). This module gets real data by rendering the public Google Maps
page with Playwright (already a dependency, already used the same way in
official.py for JS-rendered pages) instead of relying on the API.

Two entry points:
- `scrape_reviews(place_id_or_url, ...)` — reviews only, unchanged shape
  matching `google_record['raw']['reviews']` elsewhere in this repo (see
  reputation.py:44, venue_metrics.py:243), plus `has_owner_reply` (not
  present on the API's shape) so callers can derive a reply rate:
    {'author_name': str, 'rating': int, 'text': str,
     'relative_time_description': str, 'time': int | None,
     'has_owner_reply': bool}
- `scrape_place_signals(place_id_or_url, ...)` — reviews PLUS action_links
  and posts, extracted from the same browser session (visiting the place
  page twice — once per signal — would double the per-place time budget
  app.py already has to work with):
    {'reviews': [...],
     'action_links': [{'type': 'reservation'|'order'|'menu'|'tickets'|'other',
                        'label': str}, ...],
     'posts': [{'text': str, 'relative_time_description': str,
                 'time': int | None}, ...]}
  `scrape_reviews` is a thin wrapper over this for backward compatibility.

Fragility warning: Google Maps' DOM has no public API contract and its
class names are hashed/obfuscated and change without notice. This scraper
favors selectors based on ARIA roles/labels and generic text content over
hardcoded CSS classes where possible, but selectors below may need updating
if Google changes the markup, and heavy/frequent use risks CAPTCHAs or
temporary blocks. `time` is always an approximation: Google's UI only ever
shows relative strings ("hace 2 meses"), never an exact date. `max_seconds`
bounds the wall-clock time spent per place regardless of `months`/
`max_reviews` — a very actively-reviewed venue can otherwise take a long
time to scroll back 3 months, so hitting the deadline mid-scroll and
returning a partial (still `approx`) result is expected, not a bug.

Action-links/Posts specific caveats (less validated than the reviews path,
which was confirmed live against a real profile):
- The Posts selectors below are a best-effort guess, NOT yet confirmed
  against a live profile with active Posts — most small local businesses
  never post at all, so an empty result is the common, correct case, not
  necessarily evidence the selector is broken.
- Action-link classification is tuned for regular local businesses
  (restaurants/retail). Hotels show a fundamentally different
  price-based booking UI ("Desde 129€/noche") rather than a generic
  "Reservar" button, so this likely won't classify hotel booking links.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

_DEFAULT_USER_AGENT = 'Mozilla/5.0 (compatible; ProspectAuditBot/1.0; internal Localistico tool)'
_DEFAULT_LOCALE = 'es'

_NUMBER_WORDS = {'un': 1, 'una': 1, 'a': 1, 'an': 1}

_UNIT_SECONDS = {
    'segundo': 1, 'segundos': 1, 'second': 1, 'seconds': 1,
    'minuto': 60, 'minutos': 60, 'minute': 60, 'minutes': 60,
    'hora': 3600, 'horas': 3600, 'hour': 3600, 'hours': 3600,
    'dia': 86400, 'dias': 86400, 'día': 86400, 'días': 86400, 'day': 86400, 'days': 86400,
    'semana': 604800, 'semanas': 604800, 'week': 604800, 'weeks': 604800,
    'mes': 2592000, 'meses': 2592000, 'month': 2592000, 'months': 2592000,  # 30-day approximation
    'ano': 31536000, 'anos': 31536000, 'año': 31536000, 'años': 31536000,  # 365-day approximation
    'year': 31536000, 'years': 31536000,
}

_UNIT_ALTERNATION = '|'.join(sorted(_UNIT_SECONDS, key=len, reverse=True))
_RELATIVE_RE = re.compile(
    rf'(?:hace\s+)?\b(?P<num>\d+|un|una|a|an)\b\s+(?P<unit>{_UNIT_ALTERNATION})\b(?:\s+ago)?',
    re.IGNORECASE,
)
_RATING_RE = re.compile(r'(\d+(?:[.,]\d+)?)')


def _parse_relative_time(text, now=None):
    """Best-effort parse of Google's relative-time strings ("hace 2 meses",
    "3 weeks ago") into an approximate epoch. Month/year units are treated
    as flat 30/365-day approximations — good enough for a 3-month cutoff,
    not for precise ordering."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    match = _RELATIVE_RE.search(text.strip().lower())
    if not match:
        return None
    num_raw, unit = match.group('num'), match.group('unit')
    num = _NUMBER_WORDS.get(num_raw)
    if num is None:
        try:
            num = int(num_raw)
        except ValueError:
            return None
    seconds_per_unit = _UNIT_SECONDS.get(unit)
    if seconds_per_unit is None:
        return None
    return int((now - timedelta(seconds=num * seconds_per_unit)).timestamp())


def _parse_rating(rating_label):
    """Google only exposes the star rating via an aria-label (e.g. '5
    estrellas', 'Puntuación: 4,0 de 5') — extract the leading number."""
    if not rating_label:
        return None
    match = _RATING_RE.search(rating_label)
    if not match:
        return None
    return int(round(float(match.group(1).replace(',', '.'))))


def _parse_review_card(author_name, rating_label, relative_time_text, review_text,
                        has_owner_reply=False, now=None):
    """Turns already-extracted DOM strings into the normalized review dict.
    Kept as a pure function (no Playwright/DOM access) so it's testable
    without a browser."""
    return {
        'author_name': (author_name or '').strip() or None,
        'rating': _parse_rating(rating_label),
        'text': (review_text or '').strip(),
        'relative_time_description': (relative_time_text or '').strip(),
        'time': _parse_relative_time(relative_time_text, now=now),
        'has_owner_reply': bool(has_owner_reply),
    }


def _build_place_url(place_id_or_url, locale=_DEFAULT_LOCALE):
    """Accepts either a bare Google place_id or a full Maps URL and returns
    a URL that opens directly on that place, with the UI language pinned via
    `hl` so relative-time strings come back in a predictable language."""
    value = (place_id_or_url or '').strip()
    if value.startswith('http://') or value.startswith('https://'):
        if 'hl=' in value:
            return value
        separator = '&' if '?' in value else '?'
        return f'{value}{separator}hl={locale}'
    return f'https://www.google.com/maps/place/?q=place_id:{value}&hl={locale}'


def _cutoff_epoch(months, now=None):
    now = now or datetime.now(timezone.utc)
    return int((now - timedelta(days=30 * months)).timestamp())


# Generic anchors present on every place page — must be excluded before
# keyword-matching so e.g. the plain "Sitio web" link isn't mistaken for an
# action link. Checked first, wins over the type keywords below.
_ACTION_LINK_IGNORE_KEYWORDS = [
    'indicaciones', 'cómo llegar', 'directions', 'guardar', 'save', 'cercano', 'cerca', 'nearby',
    'compartir', 'share', 'llamar', 'call', 'teléfono', 'phone', 'sitio web', 'website',
    'enviar al teléfono', 'foto', 'photo', 'copiar', 'copy',
]
# Confirmed live against Google Maps ES: real action links are outbound <a>
# elements whose aria-label reads "Abrir el enlace al sitio de reservas" /
# "Abrir el enlace al menú" (or whose visible text is e.g. "Reservar una
# mesa"), NOT the Cómo-llegar/Guardar/Cercano/Compartir button row. Keywords
# use word stems ("reserva" covers reservar/reservas/reserva de mesa) since
# the exact phrasing varies by business.
_ACTION_LINK_TYPE_KEYWORDS = {
    'reservation': ['reserva', 'reserve', 'cita', 'appointment', 'book'],
    'order': ['pedir', 'pedido', 'a domicilio', 'order', 'delivery', 'pickup'],
    'menu': ['menú', 'menu', 'carta'],
    'tickets': ['entrada', 'ticket', 'comprar', 'buy'],
}


def _classify_action_link(label):
    """Classifies an action link's accessible label / visible text into a
    coarse Place Action type, or None if it's a generic anchor every place
    has (website/phone/copy) or nothing recognizable. Pure function —
    testable without a browser. `_extract_action_links` handles the DOM
    scanning and per-type dedup around this."""
    if not label:
        return None
    lowered = label.strip().lower()
    if any(keyword in lowered for keyword in _ACTION_LINK_IGNORE_KEYWORDS):
        return None
    for action_type, keywords in _ACTION_LINK_TYPE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return action_type
    return None


def _parse_post_card(text, relative_time_text, now=None):
    """Turns an already-extracted Google Posts card's raw text into a
    normalized dict. Pure function, same testable-without-a-browser pattern
    as _parse_review_card."""
    return {
        'text': (text or '').strip(),
        'relative_time_description': _clean_relative_time_line(relative_time_text) or '',
        'time': _parse_relative_time(relative_time_text, now=now),
    }


# --- Playwright interaction (thin; business logic lives in the pure functions above) ---

_CONSENT_BUTTON_LABELS = ['Rechazar todo', 'Aceptar todo', 'Reject all', 'Accept all', 'I agree']
_REVIEWS_TAB_LABEL_PREFIXES = ['Reseñas de', 'Reseñas sobre', 'Revisiones para', 'Reviews for']
_SHOW_ALL_REVIEWS_LABELS = ['Todas las reseñas', 'Todas las opiniones', 'All reviews']
_SORT_BUTTON_LABELS = ['Más útiles', 'Ordenar las reseñas', 'Ordenar reseñas', 'Sort reviews', 'Most relevant']
_SORT_NEWEST_OPTION_TEXTS = ['Más recientes', 'Newest']
_REVIEW_ITEM_SELECTOR = 'div[data-review-id][aria-label]'
_REVIEWS_FEED_SELECTOR = 'div[role="feed"]'
_REVIEWS_MAIN_SELECTOR = 'div[role="main"]'
_MORE_BUTTON_LABELS = ['Ver más', 'Más', 'More', 'Read more']
_RATING_TEXT_RE = re.compile(r'^\d+(?:[.,]\d+)?/5$')
_TRAILING_SOURCE_RE = re.compile(r'\s+en\s*$', re.IGNORECASE)
_SKIP_LINE_RE = re.compile(r'^(nueva|new)$', re.IGNORECASE)
_STOP_LINE_RE = re.compile(
    r'^(local guide|guía local|·|me gusta|like|compartir|share|más|more|ver más|'
    r'respuesta del propietario|response from the owner)\b',
    re.IGNORECASE,
)
_OWNER_REPLY_RE = re.compile(r'^(respuesta del propietario|response from the owner)\b', re.IGNORECASE)
_MAX_STALE_SCROLL_ROUNDS = 4

_POSTS_SECTION_LABELS = ['Actualizaciones', 'Updates', 'Novedades']
_POST_ITEM_SELECTOR = '[role="article"]'

# Pulls every outbound anchor's aria-label + visible text in ONE round trip
# (scanning 150+ elements one-by-one via Playwright is slow). Action links
# are always <a> (they navigate off-Maps to the booking/menu provider);
# scanning <button> too would pull in "Copiar el enlace" helpers and the
# bare icon-only "Menú" button — false positives confirmed live.
_ANCHOR_SCAN_JS = """
(root) => Array.from(root.querySelectorAll('a')).map(a => ({
    label: a.getAttribute('aria-label') || '',
    text: (a.textContent || '').trim(),
}))
"""


def _dismiss_consent(page):
    """Closes the EU cookie-consent flow if it appears. Best-effort —
    absence of it (e.g. already-consented session) is not an error.

    Depending on session state, Google serves this either as an in-page
    dialog (quick to dismiss) or a full redirect to consent.google.com that
    then navigates back to Maps (needs a real reload, not just a short
    pause) — wait for network-idle after clicking to cover both."""
    for label in _CONSENT_BUTTON_LABELS:
        try:
            button = page.get_by_role('button', name=label, exact=False)
            if button.count():
                button.first.click(timeout=3000)
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    page.wait_for_timeout(1500)
                return
        except Exception:
            continue


def _open_reviews_tab(page):
    for prefix in _REVIEWS_TAB_LABEL_PREFIXES:
        try:
            button = page.locator(f'button[aria-label^="{prefix}"], a[aria-label^="{prefix}"]')
            if button.count():
                button.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


def _open_full_reviews_list(page):
    """Some layouts (e.g. hotels) show only a rating-distribution summary
    under the reviews tab until you click through to the actual scrollable
    list. Best-effort: a no-op if that extra button isn't present."""
    for label in _SHOW_ALL_REVIEWS_LABELS:
        try:
            button = page.get_by_role('button', name=label, exact=False)
            if button.count():
                button.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def _sort_by_newest(page):
    opened = False
    for label in _SORT_BUTTON_LABELS:
        try:
            sort_button = page.get_by_role('button', name=label, exact=False)
            if sort_button.count():
                sort_button.first.click(timeout=5000)
                page.wait_for_timeout(500)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    for option_text in _SORT_NEWEST_OPTION_TEXTS:
        try:
            option = page.get_by_role('menuitemradio', name=option_text, exact=False)
            if not option.count():
                option = page.get_by_text(option_text, exact=False)
            if option.count():
                option.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


def _expand_truncated_text(item):
    """Clicks the "Ver más"/"More" button that expands a truncated review
    body, if present, before reading inner_text — so long reviews aren't
    read back with a trailing ellipsis."""
    for label in _MORE_BUTTON_LABELS:
        try:
            button = item.locator(f'button[aria-label="{label}"]')
            if button.count():
                button.first.click(timeout=1000)
                return
        except Exception:
            pass


def _clean_relative_time_line(line):
    """Google renders the relative-time text and its inline review-source
    label (e.g. a "Google" logo+text span) as one string, but Chromium's
    innerText puts the source label on its own line, leaving a dangling
    "... en" ("... on") at the end of the relative-time line — strip it so
    relative_time_description reads naturally."""
    if not line:
        return line
    return _TRAILING_SOURCE_RE.sub('', line).strip()


def _extract_review_text(lines, relative_time_line):
    """The review body sits between the relative-time line and the trailing
    action row (Me gusta/Compartir/owner reply). A "NUEVA"/"New" badge line
    can appear right after the source label — that one gets skipped, not
    treated as the end of the review, or every unread review would come
    back with empty text."""
    if relative_time_line is not None and relative_time_line in lines:
        start = lines.index(relative_time_line) + 1
        if _TRAILING_SOURCE_RE.search(relative_time_line):
            start += 1  # skip the inline review-source label line (e.g. "Google")
    else:
        start = 1
    text_lines = []
    for line in lines[start:]:
        stripped = line.strip()
        if _STOP_LINE_RE.match(stripped):
            break
        if _SKIP_LINE_RE.match(stripped):
            continue
        text_lines.append(line)
    return ' '.join(text_lines).strip()


def _extract_review_item(item):
    """Reads one review card's DOM into raw strings. Returns None if the
    card couldn't be read at all (detached, unexpected structure, etc.)."""
    try:
        author_name = item.get_attribute('aria-label', timeout=2000)
    except Exception:
        author_name = None

    try:
        rating_el = item.locator('[aria-label*="star"], [aria-label*="estrella"]').first
        rating_label = rating_el.get_attribute('aria-label', timeout=2000) if rating_el.count() else None
    except Exception:
        rating_label = None

    _expand_truncated_text(item)

    try:
        full_text = item.inner_text(timeout=2000)
    except Exception:
        return None

    lines = [line.strip() for line in full_text.split('\n') if line.strip()]
    if not lines:
        return None

    if not rating_label:
        # Some layouts (e.g. hotels) render the rating as plain "N/5" text
        # instead of an aria-label.
        rating_label = next((line for line in lines if _RATING_TEXT_RE.match(line)), None)
    if not author_name:
        author_name = lines[0]

    raw_relative_line = next((line for line in lines if _RELATIVE_RE.search(line.lower())), None)
    review_text = _extract_review_text(lines, raw_relative_line)
    relative_time_text = _clean_relative_time_line(raw_relative_line)
    has_owner_reply = any(_OWNER_REPLY_RE.match(line) for line in lines)
    return author_name, rating_label, relative_time_text, review_text, has_owner_reply


def _scroll_reviews_panel(page, cutoff_epoch, max_reviews, now=None, deadline=None):
    collected = []
    seen_keys = set()
    stale_rounds = 0

    feed = page.locator(_REVIEWS_FEED_SELECTOR).first
    main = page.locator(_REVIEWS_MAIN_SELECTOR).first

    while (len(collected) < max_reviews and stale_rounds < _MAX_STALE_SCROLL_ROUNDS
           and (deadline is None or time.monotonic() < deadline)):
        items = page.locator(_REVIEW_ITEM_SELECTOR)
        found_new = False

        for i in range(items.count()):
            item = items.nth(i)
            try:
                key = item.get_attribute('data-review-id')
            except Exception:
                key = None
            if key and key in seen_keys:
                continue

            extracted = _extract_review_item(item)
            if extracted is None:
                continue
            if key:
                seen_keys.add(key)
            found_new = True

            review = _parse_review_card(*extracted, now=now)
            if review['time'] is not None and review['time'] < cutoff_epoch:
                return collected
            collected.append(review)
            if len(collected) >= max_reviews:
                return collected

        stale_rounds = 0 if found_new else stale_rounds + 1

        try:
            scrollable = feed if feed.count() else (main if main.count() else page)
            scrollable.hover()
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1200)
        except Exception:
            break

    return collected


def _extract_action_links(page):
    """Finds genuine Place Actions (reservation/order/menu/tickets links) on
    the Overview panel. Confirmed live: these are outbound <a> elements
    whose aria-label reads "Abrir el enlace al sitio de reservas" / "Abrir
    el enlace al menú" etc. — their *visible* text is just the destination
    domain (identical to the plain website link), so the aria-label is the
    only discriminator, hence we classify on aria-label + text combined.
    Deduplicated by type, since each action typically appears 2-3 times (a
    labelled row, an "Abrir el enlace" affordance, a "Copiar el enlace"
    one). Returns [] when none are found — a business without any configured
    is a valid, common result, not a failure."""
    try:
        scope = page.locator(_REVIEWS_MAIN_SELECTOR).first
        if not scope.count():
            scope = page.locator('body').first
        anchors = scope.evaluate(_ANCHOR_SCAN_JS)
    except Exception:
        return []

    seen_types = set()
    links = []
    for anchor in anchors:
        label = anchor.get('label') or ''
        text = anchor.get('text') or ''
        action_type = _classify_action_link(f'{label} {text}')
        if action_type and action_type not in seen_types:
            seen_types.add(action_type)
            # `label` is diagnostic only (venue_metrics renders a fixed
            # per-type label, not this) — prefer the descriptive aria-label,
            # falling back to the first line of visible text.
            display = (label.strip() or text.strip().split('\n')[0])[:80]
            links.append({'type': action_type, 'label': display})
    return links


def _extract_posts(page, cutoff_epoch, deadline=None):
    """Best-effort extraction of Google Posts ("Actualizaciones") from the
    Overview tab. UNCONFIRMED against a live profile with active Posts as
    of this writing (see module docstring) — most small local businesses
    never post at all, so an empty result here is the common, expected
    case, not necessarily evidence of a broken selector."""
    try:
        heading = None
        for label in _POSTS_SECTION_LABELS:
            candidate = page.get_by_text(label, exact=False)
            if candidate.count():
                heading = candidate.first
                break
        if heading is None:
            return []

        posts = []
        items = page.locator(_POST_ITEM_SELECTOR)
        for i in range(items.count()):
            if deadline is not None and time.monotonic() > deadline:
                break
            item = items.nth(i)
            try:
                text = item.inner_text(timeout=1000)
            except Exception:
                continue
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            if not lines:
                continue
            raw_relative_line = next((line for line in lines if _RELATIVE_RE.search(line.lower())), None)
            post = _parse_post_card(' '.join(lines), raw_relative_line)
            if post['time'] is not None and post['time'] < cutoff_epoch:
                break
            posts.append(post)
        return posts
    except Exception:
        return []


def _scrape_signals_on_page(page, place_id_or_url, months, max_reviews, locale, max_seconds):
    """Core scrape against an already-open Playwright `page`. Factored out so
    a caller (e.g. a full audit) can reuse ONE browser across many places
    instead of paying a Chromium cold-start per location."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    url = _build_place_url(place_id_or_url, locale=locale)
    cutoff = _cutoff_epoch(months)
    deadline = time.monotonic() + max_seconds

    try:
        page.goto(url, wait_until='networkidle', timeout=20000)
    except PlaywrightTimeoutError:
        page.goto(url, wait_until='load', timeout=10000)
    # The place page keeps rendering client-side JS after the network goes
    # idle — give it a moment before looking for anything else.
    page.wait_for_timeout(1500)

    _dismiss_consent(page)

    # Action links and Posts live on the Overview tab the place page loads
    # on — extract them before navigating away to Reviews.
    action_links = _extract_action_links(page)
    posts = _extract_posts(page, cutoff, deadline=deadline)

    if not _open_reviews_tab(page):
        print('google_reviews_scraper: no se encontró la pestaña de reseñas', file=sys.stderr)
        return {'reviews': [], 'action_links': action_links, 'posts': posts}
    _open_full_reviews_list(page)
    _sort_by_newest(page)
    reviews = _scroll_reviews_panel(page, cutoff, max_reviews, deadline=deadline)
    return {'reviews': reviews, 'action_links': action_links, 'posts': posts}


def scrape_place_signals(place_id_or_url, *, months=3, max_reviews=150, headless=True,
                          locale=_DEFAULT_LOCALE, max_seconds=60, browser=None):
    """Returns reviews, action links and Posts for a place in one browser
    session — see the module docstring for the exact output shape and the
    fragility caveats specific to action links/Posts (less validated than
    the reviews path).

    Pass `browser` to reuse a caller-managed Playwright Browser (one per
    audit worker, opening a fresh context per place) instead of launching a
    throwaway Chromium for every location — the big latency win for a full
    audit. With `browser=None` it launches and tears down its own Chromium
    (the standalone/CLI path). Playwright's sync API is single-threaded, so
    a shared `browser` must be created and used on the same thread.

    `max_seconds` bounds the wall-clock time spent on the whole call
    (action links + Posts + reviews together), not per-signal.

    Best-effort and never raises: on any failure (Chromium missing, page
    structure changed, network error, timeout) returns whatever was
    collected so far for each signal — possibly all empty — and prints a
    warning to stderr.
    """
    empty = {'reviews': [], 'action_links': [], 'posts': []}

    if browser is not None:
        try:
            context = browser.new_context(user_agent=_DEFAULT_USER_AGENT, locale=locale)
            try:
                page = context.new_page()
                return _scrape_signals_on_page(page, place_id_or_url, months, max_reviews, locale, max_seconds)
            finally:
                context.close()
        except Exception as e:
            print(f'google_reviews_scraper: fallo durante el scraping ({e})', file=sys.stderr)
            return empty

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('google_reviews_scraper: Playwright no está instalado', file=sys.stderr)
        return empty
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=headless, args=['--disable-dev-shm-usage'])
            try:
                context = b.new_context(user_agent=_DEFAULT_USER_AGENT, locale=locale)
                page = context.new_page()
                return _scrape_signals_on_page(page, place_id_or_url, months, max_reviews, locale, max_seconds)
            finally:
                b.close()
    except Exception as e:
        print(f'google_reviews_scraper: fallo durante el scraping ({e})', file=sys.stderr)
        return empty


def scrape_reviews(place_id_or_url, *, months=3, max_reviews=150, headless=True,
                    locale=_DEFAULT_LOCALE, max_seconds=60):
    """Returns up to `max_reviews` Google Maps reviews for a place, newest
    first, stopping once a review older than `months` back is reached, or
    once `max_seconds` of wall-clock time has passed. Thin wrapper over
    `scrape_place_signals` for backward compatibility / standalone use —
    see there for the full best-effort/never-raises contract."""
    return scrape_place_signals(
        place_id_or_url, months=months, max_reviews=max_reviews, headless=headless,
        locale=locale, max_seconds=max_seconds,
    )['reviews']


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description='Scrapea reviews, action links y Posts de Google Maps de los últimos N meses de un perfil.')
    parser.add_argument('place_id_or_url', help='place_id de Google o URL completa de Google Maps')
    parser.add_argument('--months', type=int, default=3)
    parser.add_argument('--max-reviews', type=int, default=150)
    parser.add_argument('--max-seconds', type=int, default=60)
    parser.add_argument('--locale', default=_DEFAULT_LOCALE)
    parser.add_argument('--no-headless', dest='headless', action='store_false')
    parser.add_argument('--reviews-only', action='store_true',
                         help='Solo reviews (equivalente a scrape_reviews), sin action links ni Posts.')
    parser.set_defaults(headless=True)
    args = parser.parse_args(argv)

    if args.reviews_only:
        result = scrape_reviews(
            args.place_id_or_url, months=args.months, max_reviews=args.max_reviews,
            headless=args.headless, locale=args.locale, max_seconds=args.max_seconds,
        )
    else:
        result = scrape_place_signals(
            args.place_id_or_url, months=args.months, max_reviews=args.max_reviews,
            headless=args.headless, locale=args.locale, max_seconds=args.max_seconds,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
