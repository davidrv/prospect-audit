"""Local Presence Score (0–100) — the single memorable number for the sales
deliverable (portada del informe / medidor de la UI), plus the per-venue
score + severity that drive the results table's badge and row colour.

Pure and deterministic: it derives ONLY from numbers already computed
upstream — `venue_metrics` (`presence_pct`, the `accuracy_*` averages) and
`reputation` (`score`) — so it introduces no new data source and is trivially
testable. Higher is better (100 = presencia local impecable).

Three equally-legible components, each on a 0–100 scale, combined with fixed
weights and **renormalised over whichever components actually have data** for
the venue (so a venue with no reputation signal isn't silently penalised):

- presencia    (peso .40): `presence_pct` — ¿aparece siquiera listada?
- consistencia (peso .35): media de los `accuracy_*['avg']` — ¿coinciden los
  datos NAP con Google en Apple/Bing/web oficial?
- reputación   (peso .25): `100 - reputation['score']` (reputation.py puntúa
  0 = bien … 100 = lo peor), de modo que un buen rating con reseñas suficientes
  puntúa alto aquí.

Los cortes de severidad están calibrados con los ejemplos del mockup v2.
"""

WEIGHTS = {'presence': 0.40, 'consistency': 0.35, 'reputation': 0.25}


def severity_for(score):
    """Score 0–100 → banda de severidad para el color de la fila/badge."""
    if score is None:
        return 'sin_datos'
    if score < 33:
        return 'critico'
    if score < 50:
        return 'alto'
    if score < 70:
        return 'medio'
    return 'ok'


def venue_score(presence_pct, accuracy_avg, reputation_score):
    """Devuelve (score:int 0–100, severity:str) para una sede. Los componentes
    sin datos (None) se descartan y los pesos restantes se renormalizan."""
    components = {}
    if presence_pct is not None:
        components['presence'] = presence_pct
    if accuracy_avg is not None:
        components['consistency'] = accuracy_avg
    if reputation_score is not None:
        components['reputation'] = max(0, 100 - reputation_score)

    if not components:
        return 0, severity_for(0)

    total_w = sum(WEIGHTS[k] for k in components)
    raw = sum(components[k] * WEIGHTS[k] for k in components) / total_w
    score = max(0, min(100, round(raw)))
    return score, severity_for(score)


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def audit_score(clusters):
    """Local Presence Score global + sub-scores por componente, promediados
    **solo sobre las sedes con ficha en Google** (el conjunto realmente
    auditado — las sedes sin match en Google suelen ser duplicados/bajas y
    arrastrarían el número de forma engañosa). `score` es None si no hay nada
    que puntuar."""
    scored = [c for c in clusters if 'google' in c.get('sources_present', [])]
    if not scored:
        return {'score': None, 'presence': None, 'consistency': None,
                'reputation': None, 'venues_scored': 0}

    def rep_component(c):
        rep_score = (c.get('reputation') or {}).get('score')
        return None if rep_score is None else max(0, 100 - rep_score)

    def vm(c):
        return c.get('venue_metrics') or {}

    return {
        'score': _avg([vm(c).get('score') for c in scored]),
        'presence': _avg([vm(c).get('presence_pct') for c in scored]),
        'consistency': _avg([vm(c).get('accuracy_avg') for c in scored]),
        'reputation': _avg([rep_component(c) for c in scored]),
        'venues_scored': len(scored),
    }


def summary_stats(clusters):
    """Los tres números destacados del resumen del mockup, derivados del
    `platform_state`/reputación ya calculados. Solo cuenta sedes con ficha en
    Google (las auditadas):

    - inconsistent_locations: con datos distintos en Apple/Bing/web (state 'issue').
    - missing_some_platform: ausentes en Apple o Bing (state 'off').
    - reply_rate_overall: % medio de reseñas respondidas (donde hay dato scrapeado).
    """
    scored = [c for c in clusters if 'google' in c.get('sources_present', [])]

    def states(c):
        return (c.get('venue_metrics') or {}).get('platform_state') or {}

    inconsistent = sum(
        1 for c in scored
        if any(states(c).get(s) == 'issue' for s in ('apple', 'azure', 'official')))
    missing_some = sum(
        1 for c in scored
        if any(states(c).get(s) == 'off' for s in ('apple', 'azure')))

    reply_values = []
    for c in scored:
        rr = (c.get('venue_metrics') or {}).get('reply_rate_3m') or {}
        if isinstance(rr.get('value'), (int, float)):
            reply_values.append(rr['value'])

    return {
        'inconsistent_locations': inconsistent,
        'missing_some_platform': missing_some,
        'reply_rate_overall': _avg(reply_values),
    }
