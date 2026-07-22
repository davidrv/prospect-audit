"""Per-venue metrics for the sales-facing "venue table" — one row per matched
location, ranked worst-to-best data quality.

This sits on top of `inconsistencies.py` (flags: "what's wrong") and
`reputation.py` (Google-only rating/review signals) without changing either's
responsibility — it just reads what they've already computed and derives a
few extra numbers meant for a table cell rather than a sentence:

- `presence_pct` / `presence_detail`: % of the sources actually checked in
  this audit where the venue was found at all, plus a per-source detail dict
  (`{'present': bool, 'url': str|None}`) — for a present source, `url` is its
  verify_url; for an absent one (google/apple/azure only — there's no generic
  "search" for a store-locator page), `url` is a text-search deep-link on
  that platform for this venue's name+city, so a rep can click through and
  confirm the absence live instead of taking "not found" on faith.
- `accuracy_hours` / `accuracy_phone` / `accuracy_website` / `accuracy_name`:
  each a `{'avg': 0-100|None, 'anchor_value': str|None, 'breakdown': {source:
  {'verdict', 'score', 'value'}}}` dict, deliberately simple to read: **each
  comparison platform (apple/azure/official) is worth an equal share of
  100%** — 33.33% each when all 3 are checked this audit, 50% each if
  official wasn't. Google itself is the anchor/base everything is compared
  against, not one of the scored platforms — it doesn't get its own bucket
  or count toward the denominator (previously it did; changed on explicit
  request, since "does Google match Google" isn't a meaningful question). A
  platform scores 100 only if it has a value that matches Google's, 0
  otherwise (missing entirely, empty for that field, or conflicting — all
  count the same, per the ask for a simple, equal-weight "does it agree
  with Google or not" number). `breakdown` always reports apple/azure/
  official individually: `verdict` is 'match' (has a value, matches Google
  — scores 100) / 'conflict' (has a value, differs — scores 0) / 'sin_dato'
  (present in this cluster but has no value for this field at all, e.g.
  Apple's API never returns opening hours — scores 0) / 'missing' (not
  present in this cluster on that platform at all — scores 0) / 'na' (this
  platform wasn't checked for the whole audit — only happens for 'official'
  when no official data was supplied at all; excluded entirely from the
  average, same principle as `presence_pct`'s adaptive denominator, so an
  audit with no store locator doesn't get permanently capped below 100%).
  `value`/`anchor_value` are that source's/Google's actual display value
  for the field, independent of verdict — kept in the data model for
  completeness/debugging, but the UI popover deliberately shows just the
  score + a link to where it came from, not the raw values side by side
  (an earlier design, dropped on explicit feedback that it was noisy).
- `rating` / `review_count`: passed straight through from `reputation.py`
  (two separate fields — rendered as two separate table columns).
- `action_links_apple`: always 'N/D' — Apple Business Connect is entirely
  OAuth-gated, not visible via the public Maps Server API nor via scraping
  Apple Maps' public web (no equivalent surface exists there).
- `products_3m`: always 'N/D' — Google Business Profile's "Products"
  catalog is a distinct UI surface the scraper doesn't cover (different
  page/interaction than reviews/Posts/action links). TODO: viable via a
  paid third-party provider or by extending the in-house scraper further.
- `review_rate_3m` / `reply_rate_3m` / `action_links_google` / `posts_3m`:
  sourced from `google_reviews_scraper.py` when app.py's best-effort
  scraping step (`_attach_scraped_reviews`) succeeded for this location —
  real data scraped straight from the Google Maps place page (see
  `_scraped_review_metrics`/`_scraped_action_links`/`_scraped_posts_metric`).
  `review_rate_3m`/`posts_3m` stay labeled `approx=True` since a very
  actively-reviewed/posting venue can hit the scraper's own time/count
  budget before reaching the full 3 months. When scraping wasn't attempted
  or returned nothing for this location (disabled, timed out, selectors
  broke, no Google match): `review_rate_3m` falls back to approximating
  over the ≤5 most recent reviews already fetched by `_search_google`
  (`_approx_review_rate`); `reply_rate_3m`/`action_links_google`/`posts_3m`
  report 'N/D' — none of those three have any public-API fallback at all.

`venue_score` exists only to sort the table worst-to-best. It's a weighted
sum with wide, order-of-magnitude gaps between tiers so that a full swing in
a lower-priority metric can never outrank a difference in a higher-priority
one — matching the requested priority order: presence >> accuracy >>
rating/reviews. It is intentionally a heuristic, not a formally-proven
lexicographic sort.
"""
import normalize
import scoring
from inconsistencies import compare_field, hours_agreement_pct, name_similarity

CORE_SOURCES = ('google', 'apple', 'azure')
_COMPARISON_SOURCES = ('apple', 'azure', 'official')

_SEARCH_URL_BUILDERS = {
    'google': normalize.google_search_url,
    'apple': normalize.apple_search_url,
    'azure': normalize.bing_search_url,
}

_NOT_AVAILABLE_REASONS = {
    'action_links_apple': 'Requiere que el negocio gestione Apple Business Connect — no aparece en la '
                           'respuesta pública del Maps Server API, ni es visible vía scraping (Apple Maps '
                           'no expone esto en su web pública).',
    'products_3m': 'El catálogo de "Productos" de Google Business Profile es una superficie de UI '
                   'totalmente distinta (no reviews/Posts) — no cubierta por el scraper actual. '
                   'TODO: viable vía un proveedor externo o extendiendo el scraper propio.',
}

_REPLY_RATE_UNAVAILABLE_REASON = (
    "La Places API pública no expone las respuestas del propietario a las reseñas, y el scraper propio "
    "de Google Maps (google_reviews_scraper.py) no pudo obtener datos para este local en esta auditoría "
    "(deshabilitado, tiempo agotado, o sin coincidencia en Google)."
)

_ACTION_LINKS_UNAVAILABLE_REASON = (
    "El scraper propio de Google Maps (google_reviews_scraper.py) no pudo obtener datos para este local "
    "en esta auditoría (deshabilitado, tiempo agotado, o sin coincidencia en Google). La Places API "
    "pública tampoco expone action links (requeriría OAuth del propio negocio a su Google Business "
    "Profile, vía la Place Actions API)."
)

_POSTS_UNAVAILABLE_REASON = (
    "El scraper propio de Google Maps (google_reviews_scraper.py) no pudo obtener datos para este local "
    "en esta auditoría (deshabilitado, tiempo agotado, o sin coincidencia en Google). La Places API "
    "pública tampoco expone Posts (requeriría OAuth del propio negocio a su Google Business Profile)."
)

_ACTION_LINK_TYPE_LABELS = {
    'reservation': 'Reservar', 'order': 'Pedir online', 'menu': 'Menú', 'tickets': 'Entradas', 'other': 'Otro',
}

PRESENCE_WEIGHT = 1_000_000
ACCURACY_WEIGHT = 1_000
RATING_WEIGHT = 1


def compute_venue_metrics(clusters, has_official_data, city):
    """Adds a 'venue_metrics' dict to each cluster in place, and returns the
    clusters sorted worst-to-best — but with venues that have a Google Maps
    match ranked as a whole group ahead of venues that don't. Google is the
    most actively maintained source (it's the one API call that always
    runs), so a venue with no Google match at all is more likely a stale
    duplicate/closed location lingering on Apple/Bing/the official site than
    a real gap worth a rep's attention — it shouldn't outrank real venues
    just because its own venue_score happens to be low."""
    sources_checked = list(CORE_SOURCES) + (['official'] if has_official_data else [])

    for cluster in clusters:
        cluster['venue_metrics'] = _metrics_for_cluster(cluster, sources_checked, has_official_data, city)

    clusters.sort(key=lambda c: (
        0 if 'google' in c['sources_present'] else 1,
        c['venue_metrics']['venue_score'],
    ))
    return clusters


def _metrics_for_cluster(cluster, sources_checked, has_official_data, city):
    by_source = cluster['by_source']
    present = set(cluster['sources_present'])

    presence_pct = round(100 * len(present & set(sources_checked)) / len(sources_checked))
    presence_detail = _presence_detail(by_source, present, sources_checked, cluster['canonical_label'],
                                        cluster['canonical_address'], city,
                                        cluster.get('lat'), cluster.get('lng'))

    accuracy_hours = _field_metric(by_source, present, has_official_data, 'opening_hours',
                                    lambda source: hours_agreement_pct(by_source, source))
    accuracy_phone = _field_metric(by_source, present, has_official_data, 'phone',
                                    lambda source: _field_score(by_source, source, 'phone'))
    accuracy_website = _field_metric(by_source, present, has_official_data, 'website',
                                      lambda source: _field_score(by_source, source, 'website'))
    accuracy_name = _field_metric(by_source, present, has_official_data, 'name',
                                   lambda source: name_similarity(by_source, source))

    reputation = cluster.get('reputation') or {}
    rating = reputation.get('rating')
    review_count = reputation.get('review_count')

    accuracy_avgs = [m['avg'] for m in (accuracy_hours, accuracy_phone, accuracy_website, accuracy_name)
                      if m['avg'] is not None]
    accuracy_avg = sum(accuracy_avgs) / len(accuracy_avgs) if accuracy_avgs else None

    google_record = by_source.get('google')
    scraped_review_rate, scraped_reply_rate = _scraped_review_metrics(google_record)
    scraped_action_links = _scraped_action_links(google_record)
    scraped_posts = _scraped_posts_metric(google_record)

    # Local Presence Score (0–100) + severidad de esta sede — el número que
    # pinta el badge/color de fila del mockup. Se deriva de presencia +
    # consistencia (accuracy_avg) + reputación (ver scoring.py).
    reputation_score = reputation.get('score')
    score, severity = scoring.venue_score(presence_pct, accuracy_avg, reputation_score)

    accuracy_by_field = {
        'name': accuracy_name, 'phone': accuracy_phone,
        'website': accuracy_website, 'opening_hours': accuracy_hours,
    }

    metrics = {
        'presence_pct': presence_pct,
        'presence_detail': presence_detail,
        'accuracy_hours': accuracy_hours,
        'accuracy_phone': accuracy_phone,
        'accuracy_website': accuracy_website,
        'accuracy_name': accuracy_name,
        'accuracy_avg': accuracy_avg,
        'score': score,
        'severity': severity,
        'platform_state': _platform_state(present, accuracy_by_field),
        'issue_summary': _issue_summary(cluster.get('flags') or []),
        'rating': rating,
        'review_count': review_count,
        'review_rate_3m': scraped_review_rate if scraped_review_rate is not None
                          else _approx_review_rate(google_record),
        'reply_rate_3m': scraped_reply_rate if scraped_reply_rate is not None
                          else {'value': 'N/D', 'reason': _REPLY_RATE_UNAVAILABLE_REASON},
        'action_links_google': scraped_action_links if scraped_action_links is not None
                               else {'value': 'N/D', 'reason': _ACTION_LINKS_UNAVAILABLE_REASON},
        'posts_3m': scraped_posts if scraped_posts is not None
                    else {'value': 'N/D', 'reason': _POSTS_UNAVAILABLE_REASON},
        'venue_score': _venue_score(presence_pct, accuracy_avg, rating, review_count),
    }
    for field, reason in _NOT_AVAILABLE_REASONS.items():
        metrics[field] = {'value': 'N/D', 'reason': reason}
    return metrics


# Fuentes cuyas pills muestra el mockup en la columna "Comprobar en"
# (w = web oficial). Google es el ancla/referencia: si está presente, siempre
# 'ok' (no se compara consigo mismo).
_PILL_SOURCES = ('google', 'apple', 'azure', 'official')


def _platform_state(present, accuracy_by_field):
    """Estado por plataforma para las pills: 'off' (no encontrada), 'issue'
    (presente pero con algún dato en conflicto o ausente frente a Google) u
    'ok' (presente y todo coincide)."""
    states = {}
    for source in _PILL_SOURCES:
        if source not in present:
            states[source] = 'off'
        elif source == 'google':
            states[source] = 'ok'
        else:
            verdicts = [m['breakdown'].get(source, {}).get('verdict')
                        for m in accuracy_by_field.values()]
            states[source] = 'issue' if any(v in ('conflict', 'sin_dato') for v in verdicts) else 'ok'
    return states


def _issue_summary(flags):
    """Una línea "qué falla" para la tabla — el mensaje del hallazgo de mayor
    severidad (los flags ya vienen ordenados por severidad), o None si no hay
    ninguno."""
    return flags[0]['message'] if flags else None


def _presence_detail(by_source, present, sources_checked, canonical_label, canonical_address, city,
                      lat=None, lng=None):
    detail = {}
    for source in sources_checked:
        if source in present:
            detail[source] = {'present': True, 'url': by_source[source].get('verify_url')}
        elif source in _SEARCH_URL_BUILDERS:
            # The full address disambiguates the venue far better than just the
            # city (e.g. a chain with several locations in the same city) — fall
            # back to city only when no source in this cluster had an address.
            location = canonical_address or city
            query = f'{canonical_label} {location}'.strip()
            if source == 'apple':
                url = normalize.apple_search_url(query, lat=lat, lng=lng, name=canonical_label)
            else:
                url = _SEARCH_URL_BUILDERS[source](query)
            detail[source] = {'present': False, 'url': url}
        else:  # 'official' — no generic "search" concept for a store-locator page
            detail[source] = {'present': False, 'url': None}
    return detail


_DISPLAY_FIELDS = {'phone': 'phone_display', 'website': 'website_display'}


def _display_value(record, field):
    """The human-readable value of `field` on `record` — the raw (non
    phone/website-normalized) string where one exists, since that's what a
    rep wants to actually read in a popover, not the normalized comparison
    value. Opening hours (a list of "Día: HH:MM–HH:MM" lines) are joined into
    one compact string."""
    if record is None:
        return None
    if field == 'opening_hours':
        hours = record.get('opening_hours')
        return '; '.join(hours) if hours else None
    return record.get(_DISPLAY_FIELDS.get(field, field)) or record.get(field) or None


def _field_metric(by_source, present, has_official_data, field, score_fn):
    """Builds the {'avg', 'anchor_value', 'breakdown'} shape described in the
    module docstring for one field (hours/phone/website/name): every checked
    *comparison* platform (apple/azure/official — never Google itself, the
    anchor) is worth an equal share of 100%, scoring 100 only when it has a
    value that matches Google's, 0 otherwise (missing entirely, empty for
    that field, or conflicting — all count the same, per the explicit ask
    for a simple, equal-weight "does it agree with Google or not" number).
    """
    has_anchor = 'google' in by_source
    anchor_value = _display_value(by_source.get('google'), field) if has_anchor else None

    breakdown = {}
    for source in _COMPARISON_SOURCES:
        if source == 'official' and not has_official_data:
            # Not checked for this audit at all — excluded from the average
            # entirely, not counted as a failure (same principle as
            # presence_pct: no store locator shouldn't cap every score).
            breakdown[source] = {'verdict': 'na', 'score': None, 'value': None}
        elif not has_anchor:
            breakdown[source] = {'verdict': 'na', 'score': None, 'value': _display_value(by_source.get(source), field)}
        elif source not in present:
            breakdown[source] = {'verdict': 'missing', 'score': 0, 'value': None}
        else:
            value = _display_value(by_source.get(source), field)
            score = score_fn(source)
            if score is None:
                breakdown[source] = {'verdict': 'sin_dato', 'score': 0, 'value': value}
            else:
                verdict = 'match' if score >= 90 else 'conflict'
                breakdown[source] = {'verdict': verdict, 'score': 100 if verdict == 'match' else 0, 'value': value}

    if not has_anchor:
        return {'avg': None, 'anchor_value': None, 'breakdown': breakdown}

    # Google isn't one of the scored buckets — only the comparison platforms
    # that were actually checked this audit count toward the average, each
    # an equal share (33.33% each for 3, 50% each for 2 when official wasn't
    # checked — 'na' entries are excluded via the `is not None` filter).
    scores = [b['score'] for b in breakdown.values() if b['score'] is not None]
    avg = round(sum(scores) / len(scores)) if scores else None
    return {'avg': avg, 'anchor_value': anchor_value, 'breakdown': breakdown}


def _field_score(by_source, other, field):
    verdict = compare_field(by_source, other, field)
    if verdict == 'match':
        return 100
    if verdict == 'conflict':
        return 0
    return None  # unsupported / missing_both / missing_one — not a comparable data point


def _scraped_review_metrics(google_record):
    """Real review-rate and reply-rate over the last ~3 months, from
    google_reviews_scraper's Google Maps scrape — attached to the Google
    record's raw payload as 'scraped_reviews' by app.py's best-effort
    `_attach_scraped_reviews` step. `scraped_reviews` is already filtered to
    the last ~3 months (scrape_reviews stops once it hits an older review),
    so `value` here is a genuine count, not a ratio over a larger sample —
    `source: 'scraped'` tells the caller/UI to render it as a plain count
    rather than the "X of Y" fraction that only makes sense for the
    API-sample fallback below. Returns (None, None) when scraping wasn't
    attempted, failed, or returned nothing for this location, so the caller
    falls back to the Places-API approximation (review_rate_3m) or 'N/D'
    (reply_rate_3m, which has no API-based fallback at all)."""
    if not google_record:
        return None, None
    scraped = (google_record.get('raw') or {}).get('scraped_reviews') or None
    if not scraped:
        return None, None

    review_rate = {'value': len(scraped), 'sample_size': len(scraped), 'approx': True, 'source': 'scraped'}
    replied = sum(1 for r in scraped if r.get('has_owner_reply'))
    reply_rate = {'value': round(100 * replied / len(scraped)), 'sample_size': len(scraped), 'approx': True,
                  'source': 'scraped'}
    return review_rate, reply_rate


def _scraped_action_links(google_record):
    """Real action links (Reserve/Order/Menu/Tickets) detected on the
    Google Maps place page by google_reviews_scraper.py, attached as
    'scraped_action_links' by app.py's best-effort scraping step. Returns
    None when scraping wasn't attempted/failed for this location, so the
    caller falls back to 'N/D' — app.py only ever sets the key on a
    completed scrape, so an empty list here is a genuine, checked "no
    action links configured", not a missing attempt."""
    if not google_record:
        return None
    raw = google_record.get('raw') or {}
    if 'scraped_action_links' not in raw:
        return None
    links = raw['scraped_action_links'] or []
    if not links:
        return {'value': 'Ninguno detectado', 'links': [], 'source': 'scraped'}
    labels = sorted({_ACTION_LINK_TYPE_LABELS.get(link['type'], link['type']) for link in links})
    return {'value': ', '.join(labels), 'links': links, 'source': 'scraped'}


def _scraped_posts_metric(google_record):
    """Real count of Google Posts within the last ~3 months, from
    google_reviews_scraper.py, attached as 'scraped_posts' by app.py's
    best-effort scraping step. Same "checked vs. not attempted" distinction
    as _scraped_action_links — most small local businesses never post at
    all, so 0 is the common, correct result, not a sign the scrape failed.
    Returns None when scraping wasn't attempted/failed."""
    if not google_record:
        return None
    raw = google_record.get('raw') or {}
    if 'scraped_posts' not in raw:
        return None
    posts = raw['scraped_posts'] or []
    return {'value': len(posts), 'posts': posts, 'approx': True, 'source': 'scraped'}


def _approx_review_rate(google_record):
    """How many of the ≤5 most recent Google reviews (already fetched by
    _search_google, sorted newest-first) fall in the last ~90 days. This is
    an approximation over a small, possibly-unrepresentative sample — never
    a real rate, since the public API has no access to the full review
    history. `source: 'api_sample'` tells the caller/UI this is a "X of Y"
    fraction over that small sample, unlike the scraped count above. Returns
    None when there's no Google record / no reviews at all."""
    if not google_record:
        return None
    reviews = (google_record.get('raw') or {}).get('reviews') or []
    if not reviews:
        return None

    recent = sum(1 for r in reviews if (r.get('time') or 0) >= _ninety_days_ago(reviews))
    return {'value': recent, 'sample_size': len(reviews), 'approx': True, 'source': 'api_sample'}


def _ninety_days_ago(reviews):
    # Reviews carry a unix timestamp in 'time'; anchor "now" on the newest
    # review's timestamp rather than the real wall clock — this module must
    # stay pure/deterministic (no direct time.time() dependency) and the
    # data is fetched with reviews_sort='newest' already, so the first
    # review's time is a reasonable stand-in for "now" at fetch time.
    newest = max((r.get('time') or 0) for r in reviews)
    return newest - 90 * 24 * 60 * 60


def _venue_score(presence_pct, accuracy_avg, rating, review_count):
    score = presence_pct * PRESENCE_WEIGHT
    if accuracy_avg is not None:
        score += accuracy_avg * ACCURACY_WEIGHT
    if rating is not None:
        rating_component = (rating / 5) * 100
        review_bonus = min(20, (review_count or 0) / 10)
        score += (rating_component + review_bonus) * RATING_WEIGHT
    return score
