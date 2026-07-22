"""Field-level inconsistency detection across matched location clusters.

Every comparison is anchored on **Google Maps** — never platform vs. platform
(comparing Apple vs. Azure with no anchor is just noise: you don't know which
one, if either, is right). Google is the anchor because it's the one source
fetched via a reliable direct API call every time; official/store-locator
data depends on schema.org markup being present, or a manual CSV upload, and
is treated as *one of the things being checked*, not the source of truth.
Apple and Azure are compared the same way.

Every flag carries a `links` list — one entry per source involved, each with
that source's verify_url (a maps deep-link, or the scraped page for official
records) and formatted_address — so a finding can be checked live on the spot
instead of taken on faith.

v1 scope: source-coverage gaps vs. Google (R1/R11), ambiguous matches (R3),
phone/website missing-or-conflicting vs. Google (R4/R5/R6/R7), name variation
vs. Google (R9), and opening-hours contradictions/drift vs. Google (R14/R14b).
Category comparison is still deferred — see docs/plan.md backlog. Site-level
findings about the store locator itself (no schema.org markup, or
inaccessible entirely) are handled separately in official.py, not here — this
module only ever flags things about a specific matched location.
"""
from rapidfuzz import fuzz

from normalize import parse_hours

ANCHOR = 'google'
COMPARISON_SOURCES = ['apple', 'azure', 'official']

FIELD_SUPPORT = {
    'google': {'phone', 'website', 'opening_hours'},
    # Apple's own Maps Server API returns none of these, but when a
    # SERPAPI_KEY is configured we enrich Apple locations with SerpApi's
    # Apple Maps engine (app.py: _enrich_apple_with_serpapi), which does
    # provide phone/website/hours — so Apple can now be compared on all
    # three (a missing value just scores as 'sin_dato', same as any source
    # lacking data for a given venue).
    'apple': {'phone', 'website', 'opening_hours'},
    # Azure DOES support hours, but only when the fuzzy-search request opts
    # in via `openingHours=nextSevenDays` (see app.py's _search_azure) —
    # coverage per-POI isn't guaranteed even then.
    'azure': {'phone', 'website', 'opening_hours'},
    'official': {'phone', 'website', 'opening_hours'},
}

HOURS_MINOR_THRESHOLD_MIN = 30
HOURS_MODERATE_THRESHOLD_MIN = 60

_DAY_NAMES = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']

_SEVERITY_ORDER = {'critical': 0, 'moderate': 1, 'minor': 2}

_LABELS = {'google': 'Google Maps', 'apple': 'Apple Maps', 'azure': 'Bing Maps', 'official': 'la web oficial'}


def detect_inconsistencies(clusters):
    """Adds a 'flags' list to each cluster in place, sorted by severity."""
    for cluster in clusters:
        cluster['flags'] = _flags_for_cluster(cluster)
    return clusters


def _label(source):
    return _LABELS.get(source, source)


def _flag(rule, severity, message, sources, by_source, fields=None):
    links = [
        {'source': s, 'label': _label(s),
         'url': by_source.get(s, {}).get('verify_url'),
         'address': by_source.get(s, {}).get('formatted_address')}
        for s in sources
    ]
    return {'rule': rule, 'severity': severity, 'message': message,
            'sources': sources, 'fields': fields or [], 'links': links}


def _flags_for_cluster(cluster):
    flags = []
    present = set(cluster['sources_present'])
    by_source = cluster['by_source']
    others_present = [s for s in COMPARISON_SOURCES if s in present]
    has_anchor = ANCHOR in present

    if not has_anchor:
        flags.append(_flag('R1', 'critical',
            f'Presente en {", ".join(_label(s) for s in others_present)} pero no en {_label(ANCHOR)} '
            f'— revisar si es una ficha desactualizada, duplicada o un local ya cerrado.',
            sources=others_present, by_source=by_source))
    elif not others_present:
        # A Google-only location isn't inherently a finding unless official
        # data was actually provided for this audit — with no official input
        # at all, "not on Apple/Azure either" would just be noise again.
        pass
    else:
        missing = [s for s in COMPARISON_SOURCES if s not in others_present]
        if missing:
            flags.append(_flag('R11', 'moderate',
                f'No encontrada en {", ".join(_label(s) for s in missing)} (sí en {_label(ANCHOR)} y '
                f'{", ".join(_label(s) for s in others_present)}).',
                sources=missing + [ANCHOR] + others_present, by_source=by_source))

    if cluster.get('ambiguous'):
        flags.append(_flag('R3', 'critical',
            'Match ambiguo: varias sedes de la misma fuente cayeron en el mismo grupo — revisar manualmente.',
            sources=cluster['sources_present'], by_source=by_source))

    if has_anchor and others_present:
        for other in others_present:
            flags.extend(_field_flags_vs_anchor(by_source, other, 'phone', 'phone_display', 'R4', 'R5', 'teléfono'))
            flags.extend(_field_flags_vs_anchor(by_source, other, 'website', 'website_display', 'R6', 'R7', 'web'))
            name_flag = _name_flag_vs_anchor(by_source, other)
            if name_flag:
                flags.append(name_flag)
        flags.extend(_hours_flags_vs_anchor(by_source, others_present))

    return sorted(flags, key=lambda f: _SEVERITY_ORDER[f['severity']])


def compare_field(by_source, other, field):
    """Pure match/conflict/missing verdict for a scalar field (phone/website)
    vs the anchor — shared by flag generation here and by venue_metrics.py's
    accuracy scoring, so there's a single source of truth for "does this
    agree with Google or not".

    Returns one of: 'unsupported' (this source doesn't carry this field at
    all, e.g. Apple has no opening_hours), 'missing_both', 'missing_one',
    'match', 'conflict'.
    """
    if field not in FIELD_SUPPORT[other] or field not in FIELD_SUPPORT[ANCHOR]:
        return 'unsupported'

    anchor_val = by_source[ANCHOR].get(field)
    other_val = by_source[other].get(field)
    if not anchor_val and not other_val:
        return 'missing_both'
    if bool(anchor_val) != bool(other_val):
        return 'missing_one'
    return 'match' if anchor_val == other_val else 'conflict'


def name_similarity(by_source, other):
    """rapidfuzz similarity (0-100) between the anchor's and `other`'s
    normalized name, or None if either side has no name to compare.

    Compares on name_norm (accent/case/punctuation-insensitive) rather than
    the raw strings — rapidfuzz's ratio is case-sensitive, so "ZARA" vs
    "Zara" would score 25 (looks "critical") purely from capitalization.
    """
    anchor_name = by_source[ANCHOR].get('name')
    other_name = by_source[other].get('name')
    if not anchor_name or not other_name:
        return None
    return fuzz.token_sort_ratio(by_source[ANCHOR]['name_norm'], by_source[other]['name_norm'])


def hours_agreement_pct(by_source, other):
    """0-100 agreement between the anchor's and `other`'s opening hours,
    across whichever days both sides have data for — or None if there's
    nothing comparable. Reuses `_hours_diff_for_pair` (the same per-day logic
    that drives the R14/R14b flags) so a day counts as "disagreeing" under
    exactly the same rules that would flag it, rather than a separate
    threshold that could quietly drift out of sync with the flags.
    """
    anchor_hours = by_source[ANCHOR].get('opening_hours')
    other_hours = by_source[other].get('opening_hours')
    if not anchor_hours or not other_hours:
        return None

    anchor_schedule = parse_hours(anchor_hours)
    other_schedule = parse_hours(other_hours)
    comparable_days = [d for d in range(7) if d in anchor_schedule and d in other_schedule]
    if not comparable_days:
        return None

    diffs = _hours_diff_for_pair(by_source, other, other_schedule, anchor_schedule)
    return round(100 * (len(comparable_days) - len(diffs)) / len(comparable_days))


def _field_flags_vs_anchor(by_source, other, field, display_field, missing_rule, conflict_rule, label):
    verdict = compare_field(by_source, other, field)
    if verdict in ('unsupported', 'missing_both', 'match'):
        return []

    anchor_val = by_source[ANCHOR].get(field)
    other_val = by_source[other].get(field)

    flags = []
    if verdict == 'missing_one':
        has_it, lacks_it = (ANCHOR, other) if anchor_val else (other, ANCHOR)
        flags.append(_flag(missing_rule, 'critical',
            f'Sin {label} publicado en {_label(lacks_it)} (sí lo tiene {_label(has_it)}).',
            sources=[lacks_it, has_it], by_source=by_source, fields=[field]))
    else:  # conflict
        anchor_display = by_source[ANCHOR].get(display_field) or anchor_val
        other_display = by_source[other].get(display_field) or other_val
        flags.append(_flag(conflict_rule, 'critical',
            f'{label.capitalize()} distinto — {_label(ANCHOR)}: {anchor_display} vs '
            f'{_label(other)}: {other_display}.',
            sources=[other, ANCHOR], by_source=by_source, fields=[field]))

    return flags


def _name_flag_vs_anchor(by_source, other):
    anchor_name = by_source[ANCHOR].get('name')
    other_name = by_source[other].get('name')
    sim = name_similarity(by_source, other)
    if sim is None:
        return None
    if sim >= 90:
        return None  # cosmetic only (accents/case/branch suffix) — not worth surfacing

    severity = 'moderate' if sim >= 60 else 'critical'
    verb = 'varía' if sim >= 60 else 'varía notablemente'
    return _flag('R9', severity,
        f'El nombre {verb} frente a {_label(ANCHOR)} — {_label(ANCHOR)}: {anchor_name} vs '
        f'{_label(other)}: {other_name}.',
        sources=[other, ANCHOR], by_source=by_source, fields=['name'])


def _hours_flags_vs_anchor(by_source, others_present):
    supporting = [s for s in others_present if 'opening_hours' in FIELD_SUPPORT[s] and by_source[s].get('opening_hours')]
    anchor_hours = by_source[ANCHOR].get('opening_hours')
    if not supporting or not anchor_hours:
        return []

    anchor_schedule = parse_hours(anchor_hours)
    flags = []

    for other in supporting:
        other_schedule = parse_hours(by_source[other]['opening_hours'])
        flags.extend(_hours_diff_for_pair(by_source, other, other_schedule, anchor_schedule))

    return flags


def _hours_diff_for_pair(by_source, other, other_schedule, anchor_schedule):
    flags = []
    for day_idx in range(7):
        if day_idx not in other_schedule or day_idx not in anchor_schedule:
            continue

        other_val, anchor_val = other_schedule[day_idx], anchor_schedule[day_idx]
        statuses = {_status(other_val), _status(anchor_val)}
        if statuses == {'closed', 'open'}:
            flags.append(_flag('R14b', 'moderate',
                f'Horario contradictorio el {_DAY_NAMES[day_idx]} — {_label(ANCHOR)}: '
                f'{"cerrado" if anchor_val == "closed" else "abierto"}, {_label(other)}: '
                f'{"cerrado" if other_val == "closed" else "abierto"}.',
                sources=[other, ANCHOR], by_source=by_source, fields=['opening_hours']))
            continue

        if not (isinstance(other_val, list) and isinstance(anchor_val, list) and other_val and anchor_val):
            continue

        max_diff = 0
        for (o_open, o_close), (a_open, a_close) in zip(other_val, anchor_val):
            max_diff = max(max_diff, abs(o_open - a_open), abs(o_close - a_close))

        if max_diff >= HOURS_MINOR_THRESHOLD_MIN:
            severity = 'moderate' if max_diff >= HOURS_MODERATE_THRESHOLD_MIN else 'minor'
            flags.append(_flag('R14', severity,
                f'Horario distinto el {_DAY_NAMES[day_idx]} (~{max_diff} min de diferencia) — '
                f'{_label(ANCHOR)}: {_format_range(anchor_val)}, {_label(other)}: {_format_range(other_val)}.',
                sources=[other, ANCHOR], by_source=by_source, fields=['opening_hours']))

    return flags


def _status(value):
    if value == 'closed':
        return 'closed'
    if value:
        return 'open'
    return 'unknown'


def _format_range(ranges):
    return ', '.join(f'{o // 60:02d}:{o % 60:02d}–{c // 60:02d}:{c % 60:02d}' for o, c in ranges)
