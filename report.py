"""Renders the executive audit PDF report via Jinja2 + WeasyPrint."""
import base64
import os
import time

from flask import render_template
from weasyprint import HTML

import i18n

_LOGO_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'images', 'logo.png')

SOURCE_LABELS = {'google': 'Google Maps', 'apple': 'Apple Maps', 'azure': 'Bing Maps', 'official': 'Datos oficiales'}

LANGS = ('es', 'en')

# Textos del informe por idioma. Los que llevan {…} se rellenan con .format().
# La plantilla recibe el diccionario del idioma como `T`.
_STRINGS = {
    'es': {
        'html_lang': 'es', 'date_fmt': '%d/%m/%Y',
        'title': 'Auditoría {name}',
        'cover_eyebrow': 'Auditoría de presencia local', 'of_100': 'de 100', 'risk': 'RIESGO {level}',
        'cover_footer': '{n} sedes auditadas · Google, Apple, Bing, web oficial',
        'cover_footer_ai': ' y asistentes de IA', 'confidential': 'Confidencial',
        'exec_title': 'Resumen ejecutivo',
        'exec_narrative': 'Hemos auditado las <strong>{n} sedes de {name} en {city}</strong> con ficha en Google Maps '
                          '(fuente de referencia) y las hemos comparado con Apple Maps, Bing Maps y la web oficial. '
                          'El resultado es un <strong>Local Presence Score de {score}/100</strong>: los clientes que '
                          'buscan una tienda encuentran datos incompletos o contradictorios según dónde busquen.',
        'why_now': '<strong>Por qué importa ahora:</strong> las búsquedas locales acaban en visita en muy poco tiempo, '
                   'y los asistentes de IA ya recomiendan tiendas usando estos mismos datos. Cada ficha incoherente '
                   'es un cliente que llega a una tienda cerrada — o que va a la competencia.',
        'venues_title': 'Estado por sede', 'venues_tag': 'de peor a mejor · Google Maps como referencia',
        'th_score': 'SCORE', 'th_venue': 'SEDE', 'th_issues': 'QUÉ FALLA', 'th_reputation': 'REPUTACIÓN',
        'no_issues': 'Sin problemas detectados', 'reviews': 'reseñas', 'replied': 'respondidas',
        'rep_title': 'Reputación y visibilidad en IA', 'highlight_reviews': 'Reseñas recientes destacadas',
        'no_text': '(sin texto)', 'ai_review_summary': 'Resumen IA de reseñas:',
        'llm_title': 'Visibilidad en asistentes de IA · Visibility Index',
        'llm_checks_label': 'comprobaciones con aparición',
        'llm_asked': 'Preguntamos a <strong>ChatGPT</strong> «{prompt}» para cada sede ({runs} repeticiones)',
        'llm_position': '. {name} aparece {range} puesto',
        'llm_tail': ' — y los asistentes citan los datos de las plataformas, no la web oficial. Un programa GEO '
                    'consolida esas menciones en primeras posiciones con datos correctos.',
        'pos_single': 'el {n}º', 'pos_range': 'entre el {lo}º y el {hi}º',
        'locator_title': 'Web oficial · store locator', 'locator_not_analyzable': 'No se puede analizar',
        'locator_not_analyzable_desc': 'No hemos podido acceder automáticamente al store locator (bloqueo o error '
                                       'de acceso), así que no se ha podido evaluar. Recomendamos aportar un CSV de sedes.',
        'locator_consequence': 'Consecuencia: <strong>la web oficial no actúa como fuente de verdad</strong>. Google, '
                               'Apple, Bing y los asistentes de IA construyen la ficha de cada tienda con datos de '
                               'terceros, sin que la marca controle el resultado.',
        'locator_note': 'Nota del comercial (store locator):',
        'appendix_title': 'Apéndice · detalle por sede', 'appendix_tag': 'comparación campo a campo',
        'th_google_ref': 'GOOGLE · REF.', 'th_apple': 'APPLE', 'th_bing': 'BING', 'th_official': 'WEB OFICIAL',
        'row_links': 'Links', 'row_reputation': 'Reputación', 'replied_short': 'resp.',
        'google_reputation_ref': 'Google es la fuente de reputación de referencia',
        'llm_venue': '<strong>Visibilidad IA:</strong> «{prompt}» — aparece en {hits} de {runs} repeticiones.',
        'presenter_note': 'Nota del comercial:', 'official_findings': 'Hallazgos sobre los datos oficiales',
        'cta_title': '¿Hablamos de cómo arreglarlo?',
        'cta_desc': 'Localistico gestiona presencia, reputación y visibilidad en IA para marcas con múltiples sedes.',
        # report.py
        'risk_none': 'Sin datos', 'risk_critical': 'Crítico', 'risk_high': 'Alto', 'risk_medium': 'Medio',
        'sev_critico': 'Crítico', 'sev_alto': 'Alto', 'sev_medio': 'Medio', 'sev_ok': 'OK', 'sev_sin_datos': 'Sin datos',
        'cmp_name': 'Nombre', 'cmp_address': 'Dirección', 'cmp_phone': 'Teléfono', 'cmp_website': 'Web',
        'cmp_opening_hours': 'Horario',
        'cell_sin_dato': 'Sin dato', 'cell_missing': 'No encontrada', 'cell_na': 'N/D',
        'approx': ' (aprox.)',
        'sub_presence': 'Presencia', 'sub_consistency': 'Consistencia', 'sub_reputation': 'Reputación',
        'sub_geo': 'GEO · visibilidad IA',
        'stat_critical': 'sedes con problemas críticos de datos',
        'stat_low_rating': 'sedes con rating por debajo de 3,5',
        'stat_missing_official': 'sedes ausentes del store locator oficial',
        'stat_inconsistent': 'sedes con datos distintos entre plataformas',
        'stat_reply_rate': 'de las reseñas recientes reciben respuesta',
        'stat_missing_platform': 'sedes ausentes de Google, Apple o Bing',
        'blurb_inconsistent': '{inc} de {scored} sedes con datos incoherentes entre plataformas',
        'blurb_missing_official': 'sedes sin publicar en la web oficial', 'blurb_and': ' y ',
        'blurb_default': 'Presencia local auditada en Google, Apple, Bing y la web oficial.',
        'rec_missing_google': 'Reclamar/crear ficha de Google Business Profile en las {n} sedes ausentes — es el '
                              'canal de descubrimiento más usado por clientes.',
        'rec_schema': 'Implementar datos estructurados schema.org (LocalBusiness) en el store locator — hoy no los '
                      'tiene, lo que perjudica su visibilidad en buscadores y asistentes de IA (SEO local / GEO).',
        'rec_missing_official': 'Añadir datos estructurados (schema.org) o listar en la web oficial las {n} sedes '
                                'que hoy solo aparecen en mapas.',
        'rec_low_rating': 'Priorizar un plan de mejora de experiencia en las {n} sedes con rating por debajo de 3,5.',
        'rec_negative_reviews': 'Implementar un proceso de respuesta a reseñas negativas recientes — hoy no hay '
                                'ninguna gestión activa visible.',
        'rec_none': 'No se han detectado problemas críticos en esta auditoría — mantener el seguimiento periódico '
                    'de consistencia y reputación.',
    },
    'en': {
        'html_lang': 'en', 'date_fmt': '%b %d, %Y',
        'title': '{name} audit',
        'cover_eyebrow': 'Local presence audit', 'of_100': 'out of 100', 'risk': '{level} RISK',
        'cover_footer': '{n} venues audited · Google, Apple, Bing, official website',
        'cover_footer_ai': ' and AI assistants', 'confidential': 'Confidential',
        'exec_title': 'Executive summary',
        'exec_narrative': 'We audited the <strong>{n} {name} venues in {city}</strong> with a Google Maps listing '
                          '(reference source) and compared them with Apple Maps, Bing Maps and the official website. '
                          'The result is a <strong>Local Presence Score of {score}/100</strong>: customers looking '
                          'for a store find incomplete or contradictory data depending on where they search.',
        'why_now': '<strong>Why it matters now:</strong> local searches turn into visits very quickly, and AI '
                   'assistants already recommend stores using this same data. Every inconsistent listing is a '
                   'customer who arrives at a closed store — or goes to the competition.',
        'venues_title': 'Status by venue', 'venues_tag': 'worst to best · Google Maps as reference',
        'th_score': 'SCORE', 'th_venue': 'VENUE', 'th_issues': 'ISSUES', 'th_reputation': 'REPUTATION',
        'no_issues': 'No issues detected', 'reviews': 'reviews', 'replied': 'replied',
        'rep_title': 'Reputation and AI visibility', 'highlight_reviews': 'Highlighted recent reviews',
        'no_text': '(no text)', 'ai_review_summary': 'AI review summary:',
        'llm_title': 'Visibility in AI assistants · Visibility Index',
        'llm_checks_label': 'checks where it appears',
        'llm_asked': 'We asked <strong>ChatGPT</strong> “{prompt}” for each venue ({runs} repetitions)',
        'llm_position': '. {name} appears {range}',
        'llm_tail': ' — and assistants cite the platforms’ data, not the official website. A GEO programme '
                    'consolidates those mentions in top positions with correct data.',
        'pos_single': 'in position {n}', 'pos_range': 'between positions {lo} and {hi}',
        'locator_title': 'Official website · store locator', 'locator_not_analyzable': 'Cannot be analyzed',
        'locator_not_analyzable_desc': 'We could not access the store locator automatically (blocked or access '
                                       'error), so it could not be evaluated. We recommend providing a locations CSV.',
        'locator_consequence': 'Consequence: <strong>the official website does not act as the source of '
                               'truth</strong>. Google, Apple, Bing and AI assistants build each store’s listing '
                               'from third-party data, without the brand controlling the result.',
        'locator_note': 'Sales rep note (store locator):',
        'appendix_title': 'Appendix · detail by venue', 'appendix_tag': 'field-by-field comparison',
        'th_google_ref': 'GOOGLE · REF.', 'th_apple': 'APPLE', 'th_bing': 'BING', 'th_official': 'OFFICIAL WEBSITE',
        'row_links': 'Links', 'row_reputation': 'Reputation', 'replied_short': 'replied',
        'google_reputation_ref': 'Google is the reference source for reputation',
        'llm_venue': '<strong>AI visibility:</strong> “{prompt}” — appears in {hits} of {runs} repetitions.',
        'presenter_note': 'Sales rep note:', 'official_findings': 'Findings about the official data',
        'cta_title': 'Shall we talk about how to fix it?',
        'cta_desc': 'Localistico manages presence, reputation and AI visibility for multi-location brands.',
        'risk_none': 'No data', 'risk_critical': 'Critical', 'risk_high': 'High', 'risk_medium': 'Medium',
        'sev_critico': 'Critical', 'sev_alto': 'High', 'sev_medio': 'Medium', 'sev_ok': 'OK', 'sev_sin_datos': 'No data',
        'cmp_name': 'Name', 'cmp_address': 'Address', 'cmp_phone': 'Phone', 'cmp_website': 'Website',
        'cmp_opening_hours': 'Hours',
        'cell_sin_dato': 'No data', 'cell_missing': 'Not found', 'cell_na': 'N/A',
        'approx': ' (approx.)',
        'sub_presence': 'Presence', 'sub_consistency': 'Consistency', 'sub_reputation': 'Reputation',
        'sub_geo': 'GEO · AI visibility',
        'stat_critical': 'venues with critical data issues',
        'stat_low_rating': 'venues rated below 3.5',
        'stat_missing_official': 'venues missing from the official store locator',
        'stat_inconsistent': 'venues with different data across platforms',
        'stat_reply_rate': 'of recent reviews get a reply',
        'stat_missing_platform': 'venues missing from Google, Apple or Bing',
        'blurb_inconsistent': '{inc} of {scored} venues with inconsistent data across platforms',
        'blurb_missing_official': 'venues not published on the official website', 'blurb_and': ' and ',
        'blurb_default': 'Local presence audited on Google, Apple, Bing and the official website.',
        'rec_missing_google': 'Claim/create a Google Business Profile listing for the {n} missing venues — it is '
                              'the discovery channel customers use most.',
        'rec_schema': 'Implement schema.org structured data (LocalBusiness) on the store locator — it currently has '
                      'none, which hurts its visibility in search engines and AI assistants (local SEO / GEO).',
        'rec_missing_official': 'Add structured data (schema.org) or list on the official website the {n} venues '
                                'that currently only appear on maps.',
        'rec_low_rating': 'Prioritise an experience improvement plan for the {n} venues rated below 3.5.',
        'rec_negative_reviews': 'Implement a process for responding to recent negative reviews — there is '
                                'currently no visible active management.',
        'rec_none': 'No critical issues were detected in this audit — keep monitoring consistency and reputation '
                    'periodically.',
    },
}

_CHECK_LABELS_EN = {
    'jsonld_localbusiness': 'LocalBusiness structured data on the store pages',
    'schema_completeness': 'Complete schema.org markup (hours, phone, geo)',
    'store_pages_coverage': 'Individual page per venue (indexable)',
    'sitemap_present': 'Sitemap with the store pages',
}
_ACTION_LINK_LABELS_EN = {'reservation': 'Book', 'order': 'Order online', 'menu': 'Menu',
                          'tickets': 'Tickets', 'other': 'Other'}


def _logo_data_uri():
    try:
        with open(_LOGO_PATH, 'rb') as f:
            return 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
    except OSError:
        return None


def render_report_pdf(name, city, results, audit, official_comment='', lang='es'):
    # audit['clusters'] is already sorted worst-to-best by
    # venue_metrics.compute_venue_metrics (called in app.py's _build_audit) —
    # both the venue table and the appendix reuse that same order.
    clusters = audit['clusters']
    lang = lang if lang in LANGS else 'es'
    T = _STRINGS[lang]

    context = {
        'T': T,
        'prospect_name': name,
        'city': city,
        'generated_at': time.strftime(T['date_fmt']),
        'logo_data_uri': _logo_data_uri(),
        'summary': audit['summary'],
        'overall_risk': _overall_risk(audit['summary'], T),
        'recommendations': _recommendations(audit['summary'], results.get('official_findings', []), T),
        'clusters': clusters,
        'official_errors': results.get('official_errors', []),
        'official_findings': _official_findings(results.get('official_findings', []), lang),
        'official_comment': official_comment,
        'site_analysis': _site_analysis_summary(results.get('site_analysis', [])),
        'locator_report': _locator_report(results.get('locator_report'), lang),
        'source_labels': SOURCE_LABELS,
        'venue_cards': _venue_cards(clusters, lang),
        'cover_blurb': _cover_blurb(audit['summary'], T),
        'subscores': _subscores(audit['summary'], T),
        'exec_stats': _exec_stats(audit['summary'], clusters, T),
        'highlight_reviews': _highlight_reviews(clusters),
        'ai_review_summary': _ai_review_summary(clusters),
        'recent_reviews_total': _recent_reviews_total(clusters),
        'llm': _llm_block(audit['summary'], T),
    }

    html = render_template('report.html', **context)
    # base_url = raíz del repo para que el @font-face de Inter resuelva
    # url('assets/fonts/…') contra el disco.
    return HTML(string=html, base_url=os.path.dirname(os.path.abspath(__file__))).write_pdf()


def _overall_risk(summary, T):
    if not summary['total_locations']:
        return T['risk_none']
    if summary['locations_with_critical_flags'] >= summary['total_locations'] * 0.3:
        return T['risk_critical']
    if summary['locations_with_critical_flags'] > 0:
        return T['risk_high']
    return T['risk_medium']


def _recommendations(summary, official_findings, T):
    items = []
    if summary['missing_google']:
        items.append(T['rec_missing_google'].format(n=summary['missing_google']))
    if any(f['severity'] == 'moderate' for f in official_findings):
        items.append(T['rec_schema'])
    if summary['missing_official']:
        items.append(T['rec_missing_official'].format(n=summary['missing_official']))
    if summary['low_rating']:
        items.append(T['rec_low_rating'].format(n=summary['low_rating']))
    if summary['negative_review_samples']:
        items.append(T['rec_negative_reviews'])
    if not items:
        items.append(T['rec_none'])
    return items


def _official_findings(findings, lang):
    return [{**f, 'message': i18n.render(f.get('message'), f.get('msg_key'), None, lang)} for f in findings]


def _locator_report(report, lang):
    """Copia del locator_report con label/detail de cada check en `lang`
    (las claves las pone official.py; sin clave → texto original)."""
    if not report or lang == 'es':
        return report
    checks = [{**c,
               'label': _CHECK_LABELS_EN.get(c.get('key'), c.get('label')),
               'detail': i18n.render(c.get('detail'), c.get('detail_key'), c.get('detail_params'), lang)}
              for c in report.get('checks') or []]
    return {**report, 'checks': checks}


def _review_rate_display(review_rate, T):
    """A real scraped count (google_reviews_scraper.py) is already filtered
    to the last ~3 months, so 'value' and 'sample_size' are always equal —
    showing it as 'X/Y' would be a meaningless self-referential ratio, so
    it's just the plain count instead. The API-sample fallback (≤5 most
    recent reviews from the Places API) genuinely is "X of Y recent", so
    that one keeps the fraction, clearly marked as an approximation."""
    if not review_rate:
        return '—'
    if review_rate.get('source') == 'scraped':
        return str(review_rate['value'])
    return f"{review_rate['value']}/{review_rate['sample_size']}{T['approx']}"


def _reply_rate_display(reply_rate, T):
    value = reply_rate['value']
    return T['cell_na'] if value == 'N/D' else f'{value}%'


_CMP_FIELDS = ['name', 'address', 'phone', 'website', 'opening_hours']


def _cell_value(breakdown_entry, T):
    """Texto a mostrar en una celda de comparación (mismo criterio que la UI):
    el valor real si lo hay, o una etiqueta según el veredicto."""
    value = breakdown_entry.get('value')
    if value:
        return value
    verdict = breakdown_entry.get('verdict')
    return T['cell_' + verdict] if verdict in ('sin_dato', 'missing', 'na') else '—'


def _compare_rows(m, T):
    """Filas de comparación por campo (Google = referencia vs Apple/Bing/web),
    igual que el detalle expandible de la UI — solo lee accuracy.breakdown, no
    inventa nada."""
    acc = {'name': m['accuracy_name'], 'address': m['accuracy_address'], 'phone': m['accuracy_phone'],
           'website': m['accuracy_website'], 'opening_hours': m['accuracy_hours']}
    rows = []
    for key in _CMP_FIELDS:
        a = acc[key]
        cells = []
        for source in ('apple', 'azure', 'official'):
            b = (a.get('breakdown') or {}).get(source) or {'verdict': 'na', 'value': None}
            cells.append({'verdict': b.get('verdict'), 'text': _cell_value(b, T)})
        rows.append({'label': T['cmp_' + key], 'anchor': a.get('anchor_value') or '—', 'cells': cells})
    return rows


def _issue_summary(cluster, lang):
    """"Qué falla" en `lang`: el hallazgo más grave (flags ya ordenados por
    severidad); sin clave de traducción → el texto guardado en venue_metrics."""
    flags = cluster.get('flags') or []
    fallback = cluster['venue_metrics'].get('issue_summary')
    if flags and flags[0].get('msg_key'):
        return i18n.render(fallback or flags[0].get('message'), flags[0]['msg_key'],
                           flags[0].get('msg_params'), lang)
    return fallback


def _action_links_display(action_links, lang):
    value = action_links.get('value')
    links = action_links.get('links')
    if lang == 'es' or action_links.get('source') != 'scraped' or links is None:
        return value
    if not links:
        return 'None detected'
    return ', '.join(sorted({_ACTION_LINK_LABELS_EN.get(l['type'], l['type']) for l in links}))


def _venue_cards(clusters, lang='es'):
    """Una ficha por sede (mismo contenido que la fila + detalle de la UI):
    score/severidad, qué falla, reputación, estado por plataforma, la
    comparación por campo, el comentario manual del comercial y (si se pidió)
    la visibilidad en IA. No añade métricas nuevas — solo reordena lo que ya
    calcula venue_metrics."""
    T = _STRINGS[lang]
    cards = []
    for cluster in clusters:
        m = cluster['venue_metrics']
        reputation = cluster.get('reputation') or {}
        cards.append({
            'id': cluster['cluster_id'],
            'label': cluster['canonical_label'],
            'address': cluster['canonical_address'],
            'has_google': 'google' in cluster['sources_present'],
            'score': m.get('score'),
            'severity': m.get('severity'),
            'severity_label': T.get(f"sev_{m.get('severity')}", '—'),
            'issue_summary': _issue_summary(cluster, lang),
            'presenter_comment': cluster.get('presenter_comment'),
            'platform_state': m.get('platform_state') or {},
            'rating': m['rating'],
            'review_count': m['review_count'],
            'review_rate_display': _review_rate_display(m['review_rate_3m'], T),
            'reply_rate_display': _reply_rate_display(m['reply_rate_3m'], T),
            'action_links': _action_links_display(m['action_links_google'], lang),
            'compare': _compare_rows(m, T),
            'llm': m.get('llm_visibility'),
            'negative_samples': reputation.get('negative_samples') or [],
            'ai_summary': reputation.get('ai_summary'),
        })
    return cards


def _tone(value, good=70, mid=50):
    if value is None:
        return 'na'
    return 'ok' if value >= good else ('warn' if value >= mid else 'bad')


def _geo_score(summary):
    """Puntuación GEO derivada del Visibility Index (hits/comprobaciones) — no
    es una métrica nueva, solo el % de apariciones ya calculado. None si no se
    ejecutó la fase de IA."""
    llm = summary.get('llm_visibility') or {}
    checks = llm.get('checks_total') or 0
    if not checks:
        return None
    return round(100 * (llm.get('hits_total') or 0) / checks)


def _subscores(summary, T):
    ps = summary.get('presence_score') or {}
    out = [{'label': T['sub_presence'], 'value': ps.get('presence')},
           {'label': T['sub_consistency'], 'value': ps.get('consistency')},
           {'label': T['sub_reputation'], 'value': ps.get('reputation')}]
    geo = _geo_score(summary)
    if geo is not None:
        out.append({'label': T['sub_geo'], 'value': geo})
    for t in out:
        t['tone'] = _tone(t['value'])
    return out


def _exec_stats(summary, clusters, T):
    total = summary['total_locations']
    scored = (summary.get('presence_score') or {}).get('venues_scored') or 0
    stats = summary.get('stats') or {}
    reply = stats.get('reply_rate_overall')
    crit = summary['locations_with_critical_flags']
    low = summary['low_rating']
    miss_off = summary['missing_official']
    inc = stats.get('inconsistent_locations') or 0
    miss_plat = stats.get('missing_some_platform') or 0

    def s(value, label, tone):
        return {'value': value, 'label': label, 'tone': tone}

    return [
        s(crit, T['stat_critical'], 'bad' if crit else 'ok'),
        s(low, T['stat_low_rating'], 'bad' if low else 'ok'),
        s(f'{miss_off}/{total}' if total else '—', T['stat_missing_official'],
          'bad' if miss_off else 'ok'),
        s(f'{inc}/{scored}' if scored else str(inc), T['stat_inconsistent'],
          'warn' if inc else 'ok'),
        s(f'{reply}%' if reply is not None else T['cell_na'], T['stat_reply_rate'], 'warn'),
        s(miss_plat, T['stat_missing_platform'], 'ok' if not miss_plat else 'warn'),
    ]


def _cover_blurb(summary, T):
    stats = summary.get('stats') or {}
    scored = (summary.get('presence_score') or {}).get('venues_scored') or 0
    inc = stats.get('inconsistent_locations') or 0
    parts = []
    if scored and inc:
        parts.append(T['blurb_inconsistent'].format(inc=inc, scored=scored))
    if summary['missing_official']:
        parts.append(T['blurb_missing_official'])
    if not parts:
        return T['blurb_default']
    return (T['blurb_and'].join(parts) + '.').capitalize()


def _highlight_reviews(clusters, limit=2):
    out = []
    for cluster in clusters:
        for r in (cluster.get('reputation') or {}).get('negative_samples') or []:
            out.append({'rating': r.get('rating'), 'label': cluster['canonical_label'],
                        'text': r.get('text')})
    out.sort(key=lambda x: x['rating'] if x['rating'] is not None else 5)
    return out[:limit]


def _ai_review_summary(clusters):
    """Resumen IA de reseñas — solo disponible si Places API (New) devolvió uno
    (gated por ENABLE_GOOGLE_REVIEW_SUMMARY, casi siempre vacío en España). No
    tenemos un resumen agregado multi-mes propio (ver notas del informe)."""
    for cluster in clusters:
        s = (cluster.get('reputation') or {}).get('ai_summary')
        if s:
            return s
    return None


def _recent_reviews_total(clusters):
    total, any_scraped = 0, False
    for cluster in clusters:
        rr = cluster['venue_metrics'].get('review_rate_3m') or {}
        if rr.get('source') == 'scraped' and isinstance(rr.get('value'), int):
            total += rr['value']
            any_scraped = True
    return total if any_scraped else None


def _llm_block(summary, T):
    llm = summary.get('llm_visibility')
    if not llm or not llm.get('venues_checked'):
        return None
    positions = [r['position'] for v in (llm.get('per_venue') or {}).values()
                 for r in (v.get('runs') or []) if r.get('position') is not None]
    rng = None
    if positions:
        lo, hi = min(positions), max(positions)
        rng = T['pos_single'].format(n=lo) if lo == hi else T['pos_range'].format(lo=lo, hi=hi)
    return {**llm, 'position_range': rng}


def _site_analysis_summary(site_analysis):
    """Derived from official.py's per-URL site_analysis (status + inferred
    page_type). `None` for *_optimized means "no se aportó ninguna URL de
    ese tipo" — distinct from `False` ("se aportó y no tiene schema.org")."""
    index_urls = [s for s in site_analysis if s['page_type'] == 'index']
    store_urls = [s for s in site_analysis if s['page_type'] == 'store_page']
    return {
        'has_data': bool(site_analysis),
        'index_optimized': all(s['status'] == 'found' for s in index_urls) if index_urls else None,
        'store_pages_optimized': all(s['status'] == 'found' for s in store_urls) if store_urls else None,
        'urls': site_analysis,
    }
