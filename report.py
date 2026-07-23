"""Renders the executive audit PDF report via Jinja2 + WeasyPrint."""
import base64
import os
import time

from flask import render_template
from weasyprint import HTML

_LOGO_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'images', 'logo.png')

SOURCE_LABELS = {'google': 'Google Maps', 'apple': 'Apple Maps', 'azure': 'Bing Maps', 'official': 'Datos oficiales'}


def _logo_data_uri():
    try:
        with open(_LOGO_PATH, 'rb') as f:
            return 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
    except OSError:
        return None


def render_report_pdf(name, city, results, audit, official_comment=''):
    # audit['clusters'] is already sorted worst-to-best by
    # venue_metrics.compute_venue_metrics (called in app.py's _build_audit) —
    # both the venue table and the appendix reuse that same order.
    clusters = audit['clusters']

    context = {
        'prospect_name': name,
        'city': city,
        'generated_at': time.strftime('%d/%m/%Y'),
        'logo_data_uri': _logo_data_uri(),
        'summary': audit['summary'],
        'overall_risk': _overall_risk(audit['summary']),
        'recommendations': _recommendations(audit['summary'], results.get('official_findings', [])),
        'clusters': clusters,
        'official_errors': results.get('official_errors', []),
        'official_findings': results.get('official_findings', []),
        'official_comment': official_comment,
        'site_analysis': _site_analysis_summary(results.get('site_analysis', [])),
        'locator_report': results.get('locator_report'),
        'source_labels': SOURCE_LABELS,
        'venue_cards': _venue_cards(clusters),
    }

    html = render_template('report.html', **context)
    return HTML(string=html).write_pdf()


def _overall_risk(summary):
    if not summary['total_locations']:
        return 'Sin datos'
    if summary['locations_with_critical_flags'] >= summary['total_locations'] * 0.3:
        return 'Crítico'
    if summary['locations_with_critical_flags'] > 0:
        return 'Alto'
    return 'Medio'


def _recommendations(summary, official_findings):
    items = []
    if summary['missing_google']:
        items.append(f"Reclamar/crear ficha de Google Business Profile en las {summary['missing_google']} "
                      f"sedes ausentes — es el canal de descubrimiento más usado por clientes.")
    if any(f['severity'] == 'moderate' for f in official_findings):
        items.append("Implementar datos estructurados schema.org (LocalBusiness) en el store locator — "
                      "hoy no los tiene, lo que perjudica su visibilidad en buscadores y asistentes de IA "
                      "(SEO local / GEO).")
    if summary['missing_official']:
        items.append(f"Añadir datos estructurados (schema.org) o listar en la web oficial las "
                      f"{summary['missing_official']} sedes que hoy solo aparecen en mapas.")
    if summary['low_rating']:
        items.append(f"Priorizar un plan de mejora de experiencia en las {summary['low_rating']} "
                      f"sedes con rating por debajo de 3,5.")
    if summary['negative_review_samples']:
        items.append("Implementar un proceso de respuesta a reseñas negativas recientes — "
                      "hoy no hay ninguna gestión activa visible.")
    if not items:
        items.append("No se han detectado problemas críticos en esta auditoría — mantener el "
                      "seguimiento periódico de consistencia y reputación.")
    return items


def _review_rate_display(review_rate):
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
    return f"{review_rate['value']}/{review_rate['sample_size']} (aprox.)"


def _reply_rate_display(reply_rate):
    value = reply_rate['value']
    return 'N/D' if value == 'N/D' else f'{value}%'


_SEVERITY_LABEL = {'critico': 'Crítico', 'alto': 'Alto', 'medio': 'Medio', 'ok': 'OK', 'sin_datos': 'Sin datos'}
_CMP_FIELDS = [('name', 'Nombre'), ('phone', 'Teléfono'), ('website', 'Web'), ('opening_hours', 'Horario')]


def _cell_value(breakdown_entry):
    """Texto a mostrar en una celda de comparación (mismo criterio que la UI):
    el valor real si lo hay, o una etiqueta según el veredicto."""
    value = breakdown_entry.get('value')
    if value:
        return value
    return {'sin_dato': 'Sin dato', 'missing': 'No encontrada', 'na': 'N/D'}.get(
        breakdown_entry.get('verdict'), '—')


def _compare_rows(m):
    """Filas de comparación por campo (Google = referencia vs Apple/Bing/web),
    igual que el detalle expandible de la UI — solo lee accuracy.breakdown, no
    inventa nada."""
    acc = {'name': m['accuracy_name'], 'phone': m['accuracy_phone'],
           'website': m['accuracy_website'], 'opening_hours': m['accuracy_hours']}
    rows = []
    for key, label in _CMP_FIELDS:
        a = acc[key]
        cells = []
        for source in ('apple', 'azure', 'official'):
            b = (a.get('breakdown') or {}).get(source) or {'verdict': 'na', 'value': None}
            cells.append({'verdict': b.get('verdict'), 'text': _cell_value(b)})
        rows.append({'label': label, 'anchor': a.get('anchor_value') or '—', 'cells': cells})
    return rows


def _venue_cards(clusters):
    """Una ficha por sede (mismo contenido que la fila + detalle de la UI):
    score/severidad, qué falla, reputación, estado por plataforma, la
    comparación por campo, el comentario manual del comercial y (si se pidió)
    la visibilidad en IA. No añade métricas nuevas — solo reordena lo que ya
    calcula venue_metrics."""
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
            'severity_label': _SEVERITY_LABEL.get(m.get('severity'), '—'),
            'issue_summary': m.get('issue_summary'),
            'presenter_comment': cluster.get('presenter_comment'),
            'platform_state': m.get('platform_state') or {},
            'rating': m['rating'],
            'review_count': m['review_count'],
            'review_rate_display': _review_rate_display(m['review_rate_3m']),
            'reply_rate_display': _reply_rate_display(m['reply_rate_3m']),
            'action_links': m['action_links_google'].get('value'),
            'compare': _compare_rows(m),
            'llm': m.get('llm_visibility'),
            'negative_samples': reputation.get('negative_samples') or [],
            'ai_summary': reputation.get('ai_summary'),
        })
    return cards


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
