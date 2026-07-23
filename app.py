#!/usr/bin/env python3
import base64
import datetime
import logging
import os
import re
import secrets
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import jwt
import requests
import truststore
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from rapidfuzz import fuzz

import cache
import google_signals
import history
import inconsistencies
import llm_visibility
import matching
import normalize
import pricing
import reputation
import scoring
import venue_metrics
from google_reviews_scraper import scrape_place_signals
from official import extract_official, parse_official_csv

# OS-native TLS validation — some sites (e.g. tiendas.movistar.es) don't send their full
# certificate chain; browsers/curl tolerate this via the OS trust store's chain-building,
# plain certifi-based `requests` doesn't. Must run before any HTTPS connection is opened.
truststore.inject_into_ssl()

load_dotenv()

app = Flask(__name__, static_folder='public')

# Flask's app.logger defaults to WARNING regardless of debug mode — raise it
# so the review-scraping progress logs (app.logger.info below) are actually
# visible, both under `python app.py` and under gunicorn in production.
app.logger.setLevel(logging.INFO)

_BASIC_AUTH_USER = os.environ.get('BASIC_AUTH_USERNAME')
_BASIC_AUTH_PASS = os.environ.get('BASIC_AUTH_PASSWORD')

# Best-effort Google Maps review scraping (google_reviews_scraper.py) — fills
# in venue_metrics.py's review_rate_3m/reply_rate_3m with real data instead
# of the ≤5-review Places API sample. It's inherently fragile (unofficial
# DOM scraping, no API contract) and adds real time per audit, so it's kept
# behind a kill switch that can be flipped without a deploy if it ever
# starts causing problems (e.g. Google serving CAPTCHAs).
_REVIEW_SCRAPING_ENABLED = os.environ.get('DISABLE_REVIEW_SCRAPING', '').strip() != '1'
_REVIEW_SCRAPE_MONTHS = 3
_REVIEW_SCRAPE_MAX_REVIEWS = 60
_REVIEW_SCRAPE_MAX_SECONDS = 45
_REVIEW_SCRAPE_WORKERS = 5

# Apple enrichment via SerpApi's Apple Maps engine. Apple's own public Maps
# Server API returns only name/address/category — no phone, website, hours,
# rating or reviews. SerpApi's apple_maps engine exposes all of those, so we
# use it (best-effort, paid) to fill the gaps on each Apple location found by
# the free Server API search. Active only when SERPAPI_KEY is set; costs one
# SerpApi search per Apple location, so it's a paid per-audit cost.
_SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '').strip()
_APPLE_SERPAPI_ENABLED = bool(_SERPAPI_KEY) and os.environ.get('DISABLE_APPLE_SERPAPI', '').strip() != '1'
_APPLE_SERPAPI_WORKERS = 5
_APPLE_SERPAPI_NAME_SIM_MIN = 60  # rapidfuzz token_set_ratio floor to accept a SerpApi match as the same venue

# Apple's /v1/search has poor recall for multi-brand/franchise stores (e.g.
# it misses "Lowi/Vodafone Clot" on a "Lowi" query). /v1/searchAutocomplete
# surfaces them, so we ALSO discover Apple POIs via autocomplete anchored at
# each Google coordinate and resolve the matching suggestions, merging by id.
_APPLE_AUTOCOMPLETE_MAX_RESOLVE = 3   # suggestions resolved per anchor (each is one extra Apple call)
_APPLE_AUTOCOMPLETE_NAME_SIM_MIN = 55  # token_set_ratio floor to bother resolving a suggestion

# Gemini review summary (Places API New) — a paid per-place call that is
# almost always empty for Spain today, so off by default to save cost.
_GOOGLE_REVIEW_SUMMARY_ENABLED = os.environ.get('ENABLE_GOOGLE_REVIEW_SUMMARY', '').strip() == '1'

# Google signals (reviews / action links): prefer the SerpApi API path
# (google_signals) over the Playwright scraper when a SERPAPI_KEY is set —
# ~4–10x faster, no Chromium, real review timestamps + owner responses. Falls
# back to the scraper when there's no key (or DISABLE_GOOGLE_SIGNALS_API=1).
_GOOGLE_SIGNALS_VIA_SERPAPI = bool(_SERPAPI_KEY) and os.environ.get('DISABLE_GOOGLE_SIGNALS_API', '').strip() != '1'

# LLM visibility (Visibility Index) via Cloro.dev — checks whether the prospect
# shows up in ChatGPT for local-intent prompts. Paid (Cloro credits) and OPT-IN
# per audit (a checkbox in the input), so it only ever runs when the rep asks
# for it AND a CLORO_KEY is configured. Bounded to the worst N venues × R runs.
_CLORO_KEY = os.environ.get('CLORO_KEY', '').strip()
_LLM_VISIBILITY_ENABLED = bool(_CLORO_KEY) and os.environ.get('DISABLE_LLM_VISIBILITY', '').strip() != '1'


def _int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


_CLORO_RUNS = _int_env('CLORO_RUNS', 3)             # repeticiones por sede (consistencia)
_CLORO_MAX_VENUES = _int_env('CLORO_MAX_VENUES', 5)  # tope de sedes comprobadas (coste)
_CLORO_COUNTRY = os.environ.get('CLORO_COUNTRY', 'es').strip() or 'es'
_CLORO_WORKERS = _int_env('CLORO_WORKERS', 5)        # sedes comprobadas en paralelo

# Tope de sedes de Google que se auditan/muestran. Acota el mayor coste
# variable (1 Details + hasta N señales SerpApi por sede) y da un nº fijo para
# el "coste máximo" del estimador. Si se encuentran menos, sale más barato.
_GOOGLE_MAX_RESULTS = _int_env('GOOGLE_MAX_RESULTS', 25)

# Los action links de Google (1 llamada SerpApi por sede) solo se piden para
# las N peores sedes por score — igual que Cloro — para ahorrar SerpApi. El
# resto muestra N/D en "Links".
_ACTION_LINKS_MAX_VENUES = _int_env('GOOGLE_ACTION_LINKS_MAX_VENUES', 5)


# ── Background jobs (live progress polling) ─────────────────────
#
# An audit now routinely takes well past a minute (Apple's per-location
# searches, and especially review scraping — up to 45s per location, 5 in
# parallel — run inside what the old synchronous /search endpoint made look
# like one single "Buscando en Google Maps" step with zero visibility). This
# runs the audit in a background thread and lets the frontend poll for
# real progress messages instead of staring at a static label. In-memory job
# store — fine for a single-process internal tool; would need a real queue
# (Redis/DB-backed) behind more than one gunicorn worker or process.

_jobs = {}
_jobs_lock = threading.Lock()
_JOB_MAX_AGE_SECONDS = 3600  # prune finished jobs older than this on each new job creation


_AUDIT_SOURCES = ('google', 'apple', 'azure', 'official', 'llm')


def _new_job():
    _prune_old_jobs()
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'running', 'progress': [], 'result': None, 'error': None,
            'created_at': time.time(), 'cancelled': False, 'percent': 0,
            # Per-source state for the progress screen's step list. State is one
            # of pending|running|done|error|skipped; count is the #sedes found.
            'sources': {s: {'state': 'pending', 'count': None} for s in _AUDIT_SOURCES},
        }
    return job_id


def _prune_old_jobs():
    cutoff = time.time() - _JOB_MAX_AGE_SECONDS
    with _jobs_lock:
        stale = [jid for jid, job in _jobs.items()
                 if job['status'] != 'running' and job['created_at'] < cutoff]
        for jid in stale:
            del _jobs[jid]


def _job_progress_fn(job_id):
    """Returns a thread-safe `emit(message)` closure for this job — called
    from however many worker threads a given audit phase happens to use
    (ThreadPoolExecutor pools for Google Details/review-scraping/Apple's
    per-anchor searches all call this concurrently)."""
    def emit(message):
        app.logger.info(message)
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job['progress'].append(message)
    return emit


class _AuditCancelled(Exception):
    """Raised at a phase boundary when the user cancelled the job."""


def _job_status_fn(job_id):
    """Thread-safe `set_status(source, state, count)` closure — drives the
    progress screen's per-source step list and the overall percent bar."""
    def set_status(source, state, count=None):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job['sources'][source] = {'state': state, 'count': count}
            job['percent'] = _compute_percent(job['sources'])
    return set_status


def _compute_percent(sources):
    active = [s for s in sources.values() if s['state'] != 'skipped']
    if not active:
        return 100
    done = sum(1 for s in active if s['state'] in ('done', 'error'))
    # Cap at 90 until the job flips to 'done' — there's still the cross/compare
    # step after every source has resolved.
    return min(90, round(90 * done / len(active)))


def _job_cancel_check(job_id):
    def is_cancelled():
        with _jobs_lock:
            job = _jobs.get(job_id)
            return bool(job and job.get('cancelled'))
    return is_cancelled


def _run_audit_job(job_id, name, city, official_urls, csv_locations, csv_errors, official_comment, mode,
                   check_llm_visibility=False, llm_category=''):
    emit = _job_progress_fn(job_id)
    set_status = _job_status_fn(job_id)
    is_cancelled = _job_cancel_check(job_id)
    try:
        results, audit = _run_audit(name, city, official_urls, csv_locations, csv_errors,
                                    progress=emit, status=set_status, should_cancel=is_cancelled,
                                    check_llm_visibility=check_llm_visibility, llm_category=llm_category)

        if mode == 'pdf':
            emit('Generando informe PDF…')
            from report import render_report_pdf
            pdf_bytes = render_report_pdf(name, city, results, audit, official_comment)
            payload = {
                'pdf_base64': base64.b64encode(pdf_bytes).decode('ascii'),
                'filename': 'auditoria_' + name.replace(' ', '_') + '.pdf',
            }
        else:
            payload = {**results, 'audit': audit, 'official_comment': official_comment}
            # Persist the audit so it shows up in "Auditorías recientes" and can
            # be reopened without recomputing (history.py — best-effort, never
            # blocks the job on a storage error). Only for search jobs: the
            # snapshot IS this payload.
            summary = audit.get('summary') or {}
            score = ((summary.get('presence_score') or {}).get('score'))
            history.save(job_id, name, city, score, summary.get('total_locations'), payload)

        emit('Auditoría completa.')
        with _jobs_lock:
            _jobs[job_id]['status'] = 'done'
            _jobs[job_id]['result'] = payload
            _jobs[job_id]['percent'] = 100
    except _AuditCancelled:
        app.logger.info(f'Audit job {job_id} cancelled')
        emit('Auditoría cancelada.')
        with _jobs_lock:
            _jobs[job_id]['status'] = 'cancelled'
            _jobs[job_id]['error'] = 'cancelada'
    except Exception as e:
        app.logger.exception(f'Audit job {job_id} failed')
        emit(f'Error: {e}')
        with _jobs_lock:
            _jobs[job_id]['status'] = 'error'
            _jobs[job_id]['error'] = str(e)


@app.before_request
def _require_basic_auth():
    # Only enforced when both are set — e.g. unset for frictionless local dev,
    # required in deployment via the platform's env vars/secrets.
    if not _BASIC_AUTH_USER or not _BASIC_AUTH_PASS:
        return None

    auth = request.authorization
    valid = (
        auth is not None
        and secrets.compare_digest(auth.username or '', _BASIC_AUTH_USER)
        and secrets.compare_digest(auth.password or '', _BASIC_AUTH_PASS)
    )
    if not valid:
        return Response('Autenticación requerida', 401, {'WWW-Authenticate': 'Basic realm="Prospect Audit"'})
    return None


@app.route('/')
def index():
    # No-store so the browser always fetches the latest HTML/JS (all of it is
    # inline in index.html) — otherwise UI changes silently don't show up
    # until a manual hard-refresh, which looks like "my change didn't work".
    resp = send_from_directory('public', 'index.html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp


@app.route('/template_oficial.csv')
def official_csv_template():
    return send_from_directory('public', 'template_oficial.csv', as_attachment=True)


@app.route('/assets/<path:filename>')
def assets(filename):
    return send_from_directory('assets', filename)


def _run_audit(name, city, official_urls, csv_locations, csv_errors,
               progress=None, status=None, should_cancel=None, check_llm_visibility=False, llm_category=''):
    def emit(message):
        if progress:
            progress(message)

    def set_status(source, state, count=None):
        if status:
            status(source, state, count)

    def check_cancel():
        if should_cancel and should_cancel():
            raise _AuditCancelled()

    results = {'google': [], 'apple': [], 'azure': [],
               'official': list(csv_locations), 'official_errors': list(csv_errors),
               'official_findings': [], 'site_analysis': [], 'locator_report': None}

    def run_google():
        set_status('google', 'running')
        emit('Buscando en Google Maps…')
        try:
            results['google'] = _search_google(name, city, progress=progress)
            emit(f'Google Maps: {len(results["google"])} sede(s) encontradas.')
            set_status('google', 'done', len(results['google']))
        except Exception as e:
            app.logger.error(f'Google error: {e}')
            emit(f'Google Maps: error ({e}).')
            set_status('google', 'error')

    def run_official():
        if not official_urls and not csv_locations:
            set_status('official', 'skipped')
            return
        if not official_urls:  # CSV-only: nothing to crawl, data already loaded
            set_status('official', 'done', len(csv_locations))
            return
        set_status('official', 'running')
        emit(f'Extrayendo datos oficiales de {len(official_urls)} URL(s)…')
        try:
            extracted = extract_official(official_urls, city=city)
            results['official'].extend(extracted['locations'])
            results['official_errors'].extend(extracted['errors'])
            results['official_findings'].extend(extracted['findings'])
            results['site_analysis'] = extracted['site_analysis']
            results['locator_report'] = extracted.get('locator_report')
            emit(f'Datos oficiales: {len(extracted["locations"])} sede(s) extraídas.')
            set_status('official', 'done', len(results['official']))
        except Exception as e:
            app.logger.error(f'Official extraction error: {e}')
            emit(f'Datos oficiales: error ({e}).')
            set_status('official', 'error')

    def run_apple(google_coords, google_places):
        set_status('apple', 'running')
        emit('Buscando en Apple Maps (por sede, usando las coordenadas de Google)…')
        try:
            results['apple'] = _search_apple(name, city, extra_anchors=google_coords,
                                              google_places=google_places)
            emit(f'Apple Maps: {len(results["apple"])} sede(s) encontradas.')
            # El enriquecimiento SerpApi (de pago) se hace más tarde, tras
            # clusterizar, y SOLO sobre las sedes Apple que machean con Google
            # (ver _enrich_apple_clusters en _build_audit) — así no se gasta en
            # POIs que no salen en el informe.
            set_status('apple', 'done', len(results['apple']))
        except Exception as e:
            app.logger.error(f'Apple error: {e}')
            emit(f'Apple Maps: error ({e}).')
            set_status('apple', 'error')

    def run_azure(google_coords):
        set_status('azure', 'running')
        emit('Buscando en Bing Maps (por sede, usando las coordenadas de Google)…')
        try:
            results['azure'] = _search_azure(name, city, extra_anchors=google_coords)
            emit(f'Bing Maps: {len(results["azure"])} sede(s) encontradas.')
            set_status('azure', 'done', len(results['azure']))
        except Exception as e:
            app.logger.error(f'Azure error: {e}')
            emit(f'Bing Maps: error ({e}).')
            set_status('azure', 'error')

    # Both Apple and Azure are anchored per-location using Google's confirmed
    # coordinates (see _search_apple/_search_azure) — a single city-wide
    # query only surfaces the handful of matches closest to that one point
    # (Apple) or ranked highest by relevance (Azure), missing chain
    # locations spread across the rest of the city (confirmed via live
    # testing against the real Apple API; Azure never got this same fix
    # until now). That means both have to wait for Google to finish;
    # Official has no such dependency and still runs fully in parallel with
    # Google.
    check_cancel()
    google_thread = threading.Thread(target=run_google)
    official_thread = threading.Thread(target=run_official)

    google_thread.start()
    official_thread.start()
    google_thread.join()

    check_cancel()  # bail before spending Apple/Bing calls if the user cancelled
    google_coords = _google_coords(results['google'])
    google_places = _google_places(results['google'])
    post_google_threads = [
        threading.Thread(target=run_apple, args=(google_coords, google_places)),
        threading.Thread(target=run_azure, args=(google_coords,)),
    ]
    for t in post_google_threads:
        t.start()
    for t in post_google_threads:
        t.join()

    official_thread.join()

    emit('Cruzando y comparando datos entre plataformas…')
    audit = _build_audit(results, has_official_data=bool(official_urls or csv_locations), city=city,
                         progress=progress)

    # Fase opcional (opt-in por checkbox) y de pago: visibilidad en IA vía Cloro.
    if check_llm_visibility and _LLM_VISIBILITY_ENABLED:
        check_cancel()
        _attach_llm_visibility(audit, name, city, emit, set_status, category=llm_category)
    else:
        set_status('llm', 'skipped')

    return results, audit


def _attach_llm_visibility(audit, name, city, emit, set_status, category=''):
    """Best-effort: rellena venue_metrics['llm_visibility'] por sede y
    summary['llm_visibility'] (agregado). Acotado por _CLORO_MAX_VENUES/_RUNS."""
    set_status('llm', 'running')
    emit('Comprobando visibilidad en IA (ChatGPT vía Cloro)…')
    try:
        vis = llm_visibility.fetch_llm_visibility(
            audit['clusters'], name, city,
            runs=_CLORO_RUNS, max_venues=_CLORO_MAX_VENUES, country=_CLORO_COUNTRY,
            workers=_CLORO_WORKERS, progress=emit, category=category)
        audit['summary']['llm_visibility'] = vis
        per_venue = vis.get('per_venue') or {}
        for cluster in audit['clusters']:
            if cluster['cluster_id'] in per_venue:
                cluster['venue_metrics']['llm_visibility'] = per_venue[cluster['cluster_id']]
        emit(f"Visibilidad en IA: aparece en {vis['hits_total']} de {vis['checks_total']} "
             f"comprobaciones ({vis['calls']} llamadas a Cloro).")
        set_status('llm', 'done', vis['venues_checked'])
    except Exception as e:
        app.logger.error(f'LLM visibility error: {e}')
        emit(f'Visibilidad en IA: error ({e}).')
        set_status('llm', 'error')


def _google_coords(google_results):
    coords = []
    for place in google_results:
        loc = ((place.get('geometry') or {}).get('location')) or {}
        if loc.get('lat') is not None and loc.get('lng') is not None:
            coords.append((loc['lat'], loc['lng']))
    return coords


def _google_places(google_results):
    """(name, lat, lng) per Google location — lets the Apple autocomplete
    pass query each store by its OWN name (better recall for multi-brand
    stores) rather than the generic prospect name."""
    places = []
    for place in google_results:
        loc = ((place.get('geometry') or {}).get('location')) or {}
        if loc.get('lat') is not None and loc.get('lng') is not None and place.get('name'):
            places.append({'name': place['name'], 'lat': loc['lat'], 'lng': loc['lng']})
    return places


def _build_audit(results, has_official_data, city, progress=None):
    records = (
        [normalize.from_google(p) for p in results['google']]
        + [normalize.from_apple(p) for p in results['apple']]
        + [normalize.from_azure(p) for p in results['azure']]
        + _fill_missing_coords(results['official'])  # official.py rarely has geo; matching needs it
    )

    clusters = matching.cluster_records(records)
    # Enriquecer Apple (SerpApi, de pago) SOLO ahora que sabemos qué POIs
    # machean con Google — antes de comparar campos en inconsistencies/metrics.
    _enrich_apple_clusters(clusters, progress=progress)
    inconsistencies.detect_inconsistencies(clusters)
    reputation.compute_reputation(clusters)
    venue_metrics.compute_venue_metrics(clusters, has_official_data, city)
    # Action links: solo para las N peores sedes (ya ordenadas por score) —
    # ahorra SerpApi vs pedirlos para todas. Va después del sort.
    _attach_action_links_worst(clusters, progress=progress)

    return {'clusters': clusters, 'summary': _audit_summary(clusters)}


def _attach_action_links_worst(clusters, progress=None):
    """Pide los action links de Google (SerpApi) solo para las
    `_ACTION_LINKS_MAX_VENUES` peores sedes con Google (los clusters ya vienen
    ordenados peor→mejor) y actualiza su métrica `action_links_google`. Best-
    effort; el resto queda en N/D. Cacheado por place_id."""
    if not _GOOGLE_SIGNALS_VIA_SERPAPI or not _REVIEW_SCRAPING_ENABLED:
        return
    worst = [c for c in clusters if 'google' in c['sources_present']][:_ACTION_LINKS_MAX_VENUES]
    targets = [(c, (c['by_source']['google'].get('raw') or {})) for c in worst]
    targets = [(c, raw) for c, raw in targets if raw.get('place_id')]
    if not targets:
        return

    def emit(msg):
        app.logger.info(msg)
        if progress:
            progress(msg)

    emit(f'Analizando action links (top {len(targets)} peores sedes)…')

    def _one(pair):
        cluster, raw = pair
        place_id = raw['place_id']
        ck = f'action_links:{place_id}'
        links = cache.get(ck)
        if links is None:
            links = google_signals.fetch_action_links(place_id)
            if links:
                cache.set(ck, links)
        raw['scraped_action_links'] = links or []
        cluster['venue_metrics']['action_links_google'] = (
            venue_metrics._scraped_action_links(cluster['by_source']['google'])
            or {'value': 'N/D', 'reason': venue_metrics._ACTION_LINKS_UNAVAILABLE_REASON})

    with ThreadPoolExecutor(max_workers=_REVIEW_SCRAPE_WORKERS) as pool:
        list(pool.map(_one, targets))


def _enrich_apple_clusters(clusters, progress=None):
    """Enriquece vía SerpApi (phone/web/horario/rating) SOLO los records Apple
    que caen en un cluster con Google — las únicas sedes que se comparan y
    salen en el informe. Recorta el coste de ~1 llamada por POI Apple a ≤1 por
    sede con match en Google. Best-effort, threaded. No-op sin key."""
    if not _APPLE_SERPAPI_ENABLED:
        return
    targets = [c['by_source']['apple'] for c in clusters
               if 'google' in c['sources_present'] and 'apple' in c['by_source']]
    if not targets:
        return

    def emit(msg):
        app.logger.info(msg)
        if progress:
            progress(msg)

    total = len(targets)
    emit(f'Enriqueciendo Apple vía SerpApi (solo sedes con match en Google): 0/{total}…')
    done, lock = 0, threading.Lock()

    def _one(rec):
        nonlocal done
        _enrich_apple_record(rec)
        with lock:
            done += 1
            if done == total or done % 5 == 0:
                emit(f'Enriqueciendo Apple vía SerpApi: {done}/{total}…')

    with ThreadPoolExecutor(max_workers=_APPLE_SERPAPI_WORKERS) as pool:
        list(pool.map(_one, targets))


def _enrich_apple_record(rec):
    """Rellena un record Apple NORMALIZADO con datos de SerpApi (los campos que
    el Server API de Apple no da: horario/rating/reseñas; y phone/web si faltan).
    Los valores propios del Server API ganan. Best-effort."""
    match = _serpapi_apple_lookup(rec.get('name'), rec.get('lat'), rec.get('lng'))
    if not match:
        return
    if not rec.get('phone_display') and match.get('phone'):
        rec['phone_display'] = match['phone']
        rec['phone'] = normalize.phone_norm(match['phone'])
    if not rec.get('website_display') and match.get('website'):
        rec['website_display'] = match['website']
        rec['website'] = normalize.website_norm(match['website'])
    if not rec.get('category'):
        rec['category'] = match.get('type') or (match.get('types') or [None])[0]
    hours = _serpapi_weekly_hours_to_list(match.get('weekly_hours'))
    if hours:
        rec['opening_hours'] = hours
    if match.get('rating') is not None:
        rec['rating'] = match.get('rating')
    if match.get('reviews') is not None:
        rec['review_count'] = match.get('reviews')
    (rec.setdefault('raw', {}))['serpapi_enriched'] = True


def _audit_summary(clusters):
    total = len(clusters)
    return {
        'presence_score': scoring.audit_score(clusters),
        'stats': scoring.summary_stats(clusters),
        'total_locations': total,
        'locations_with_critical_flags': sum(
            1 for c in clusters if any(f['severity'] == 'critical' for f in c['flags'])),
        'missing_google': sum(1 for c in clusters if 'google' not in c['sources_present']),
        'missing_apple': sum(1 for c in clusters if 'apple' not in c['sources_present']),
        'missing_azure': sum(1 for c in clusters if 'azure' not in c['sources_present']),
        'missing_official': sum(1 for c in clusters if 'official' not in c['sources_present']),
        'low_rating': sum(
            1 for c in clusters
            if c['reputation']['rating'] is not None and c['reputation']['rating'] < reputation.RATING_CRITICAL),
        'no_reviews': sum(
            1 for c in clusters
            if 'google' in c['sources_present'] and not c['reputation']['review_count']),
        'negative_review_samples': sum(len(c['reputation']['negative_samples']) for c in clusters),
    }


def _parse_request_params():
    # request.values covers both query-string args (GET, or a bookmarkable
    # URL-only request) and form fields (POST multipart, needed for the CSV
    # upload) with the same code.
    name = request.values.get('name', '').strip()
    city = request.values.get('city', '').strip() or 'Barcelona'
    official_urls = [u.strip() for u in request.values.getlist('official_url') if u.strip()]
    official_comment = request.values.get('official_comment', '').strip()

    csv_locations, csv_errors = [], []
    csv_file = request.files.get('official_csv')
    if csv_file and csv_file.filename:
        parsed = parse_official_csv(csv_file.stream)
        csv_locations, csv_errors = parsed['locations'], parsed['errors']

    return name, city, official_urls, csv_locations, csv_errors, official_comment


@app.route('/search', methods=['GET', 'POST'])
def search():
    name, city, official_urls, csv_locations, csv_errors, official_comment = _parse_request_params()
    if not name:
        return jsonify({'error': 'Falta el nombre del prospect'}), 400

    results, audit = _run_audit(name, city, official_urls, csv_locations, csv_errors)
    return jsonify({**results, 'audit': audit, 'official_comment': official_comment})


@app.route('/report', methods=['GET', 'POST'])
def report():
    name, city, official_urls, csv_locations, csv_errors, official_comment = _parse_request_params()
    if not name:
        return jsonify({'error': 'Falta el nombre del prospect'}), 400

    results, audit = _run_audit(name, city, official_urls, csv_locations, csv_errors)

    from report import render_report_pdf
    pdf_bytes = render_report_pdf(name, city, results, audit, official_comment)

    filename = 'auditoria_' + name.replace(' ', '_') + '.pdf'
    return Response(pdf_bytes, mimetype='application/pdf',
                     headers={'Content-Disposition': f'attachment; filename="{filename}"'})


_MAX_COMMENT_LEN = 600  # per-venue presenter note — capped so an arbitrarily long paste can't blow up the PDF


def _apply_report_edits(audit, deleted_ids, comments):
    """Applies the browser's findings-table edits to an already-computed
    audit in place: drops venues the user discarded and attaches a
    presenter comment to the rest, then recomputes the summary KPIs from
    the surviving clusters so deleted venues stop counting. Deleting from
    `audit['clusters']` also removes them from the PDF's review-highlights
    and per-venue appendix, since those iterate the same list."""
    deleted = set(deleted_ids or [])
    comments = comments or {}
    kept = [c for c in audit.get('clusters', []) if c.get('cluster_id') not in deleted]
    for cluster in kept:
        raw = comments.get(cluster.get('cluster_id'))
        cluster['presenter_comment'] = (str(raw).strip()[:_MAX_COMMENT_LEN] or None) if raw else None
    # Preserve the LLM-visibility aggregate (computed in a separate phase, not
    # by _audit_summary) across the recompute so the PDF keeps it.
    prev_llm = (audit.get('summary') or {}).get('llm_visibility')
    audit['clusters'] = kept
    audit['summary'] = _audit_summary(kept)
    if prev_llm:
        audit['summary']['llm_visibility'] = prev_llm
    return audit


@app.route('/report/from_data', methods=['POST'])
def report_from_data():
    """Renders the PDF from the audit the browser already has, plus the
    user's table edits (discarded rows + per-venue comments) — NO recompute,
    NO scraping. See _apply_report_edits and the /report/start recompute
    path (kept for the non-edited flow / scripting)."""
    data = request.get_json(force=True, silent=True) or {}
    audit = data.get('audit')
    if not isinstance(audit, dict) or 'clusters' not in audit:
        return jsonify({'error': 'Falta el audit a renderizar'}), 400

    _apply_report_edits(audit, data.get('deleted_cluster_ids'), data.get('row_comments'))
    results = {
        'official_errors': data.get('official_errors') or [],
        'official_findings': data.get('official_findings') or [],
        'site_analysis': data.get('site_analysis') or [],
        'locator_report': data.get('locator_report'),
    }
    name = (data.get('name') or '').strip()
    city = (data.get('city') or '').strip()
    official_comment = (data.get('official_comment') or '').strip()

    from report import render_report_pdf
    pdf_bytes = render_report_pdf(name, city, results, audit, official_comment)

    filename = 'auditoria_' + (name or 'prospect').replace(' ', '_') + '.pdf'
    return Response(pdf_bytes, mimetype='application/pdf',
                     headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# ── Background job endpoints (live progress polling) ────────────
#
# /search and /report above are kept as-is (synchronous, unchanged) for
# backward compatibility/scripting; the UI uses these instead to show real
# progress instead of a static "Buscando en..." label.

def _start_job(mode):
    name, city, official_urls, csv_locations, csv_errors, official_comment = _parse_request_params()
    if not name:
        return jsonify({'error': 'Falta el nombre del prospect'}), 400

    check_llm = request.values.get('check_llm_visibility', '').strip().lower() in ('1', 'true', 'on', 'yes')
    llm_category = request.values.get('llm_category', '').strip()

    job_id = _new_job()
    thread = threading.Thread(
        target=_run_audit_job,
        args=(job_id, name, city, official_urls, csv_locations, csv_errors, official_comment, mode),
        kwargs={'check_llm_visibility': check_llm, 'llm_category': llm_category},
    )
    thread.start()
    return jsonify({'job_id': job_id})


@app.route('/search/start', methods=['POST'])
def search_start():
    return _start_job('search')


@app.route('/report/start', methods=['POST'])
def report_start():
    return _start_job('pdf')


@app.route('/jobs/<job_id>/status')
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({'error': 'job no encontrado (puede haber expirado)'}), 404

        since = request.args.get('since', 0, type=int)
        payload = {
            'status': job['status'],
            'progress': job['progress'][since:],
            'progress_count': len(job['progress']),
            'sources': job.get('sources'),
            'percent': job.get('percent', 0),
        }
        if job['status'] == 'done':
            payload['result'] = job['result']
        elif job['status'] in ('error', 'cancelled'):
            payload['error'] = job['error']

    return jsonify(payload)


@app.route('/jobs/<job_id>/cancel', methods=['POST'])
def job_cancel(job_id):
    """Best-effort cancel: flags the job so _run_audit bails out at its next
    phase boundary (the in-flight source search still finishes)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({'error': 'job no encontrado'}), 404
        if job['status'] == 'running':
            job['cancelled'] = True
    return jsonify({'ok': True})


# ── Histórico de auditorías ──────────────────────────────────────

@app.route('/pricing')
def pricing_route():
    """Coste máximo estimado por auditoría (para el widget del input). Solo
    precios/caps, sin secretos."""
    est = pricing.estimate_max(
        google_max=_GOOGLE_MAX_RESULTS,
        reviews_pages=google_signals._reviews_max_pages(),
        cloro_venues=_CLORO_MAX_VENUES, cloro_runs=_CLORO_RUNS,
        action_links_venues=_ACTION_LINKS_MAX_VENUES)
    est['llm_available'] = _LLM_VISIBILITY_ENABLED
    return jsonify(est)


@app.route('/audits/recent')
def audits_recent():
    limit = request.args.get('limit', 10, type=int)
    return jsonify({'audits': history.recent(limit=max(1, min(50, limit)))})


@app.route('/audits/<audit_id>')
def audit_get(audit_id):
    record = history.get(audit_id)
    if record is None:
        return jsonify({'error': 'auditoría no encontrada'}), 404
    return jsonify(record)


def _purge_audit_cache(snapshot):
    """Borra de la caché las señales por-sede (reviews/action links) de los
    place_id de esta auditoría, para que un re-análisis las vuelva a pedir
    frescas (útil si una corrida quedó cacheada con datos malos)."""
    clusters = ((snapshot or {}).get('audit') or {}).get('clusters') or []
    for cluster in clusters:
        google = (cluster.get('by_source') or {}).get('google') or {}
        pid = (google.get('raw') or {}).get('place_id') or google.get('source_id')
        if not pid:
            continue
        cache.delete(f'signals:api:{pid}:{_REVIEW_SCRAPE_MONTHS}')
        cache.delete(f'signals:scrape:{pid}:{_REVIEW_SCRAPE_MONTHS}')
        cache.delete(f'action_links:{pid}')


@app.route('/audits/<audit_id>', methods=['DELETE'])
def audit_delete(audit_id):
    """Borra una auditoría del histórico y purga la caché de sus sedes, para
    que re-auditarla parta de cero. Idempotente."""
    record = history.get(audit_id)
    if record is not None:
        _purge_audit_cache(record.get('snapshot'))
    history.delete(audit_id)
    return jsonify({'ok': True})


@app.route('/cache/clear', methods=['POST'])
def cache_clear():
    """Vacía toda la caché de análisis (reviews/enriquecimiento/IA)."""
    cache.clear()
    return jsonify({'ok': True})


@app.route('/audits/<audit_id>/edits', methods=['POST'])
def audit_edits(audit_id):
    """Persiste ediciones manuales en el snapshot guardado: comentarios por
    sede (presenter_comment) y el comentario del store locator
    (official_comment). Fusiona lo enviado; enviar '' borra ese comentario.
    Requiere que el histórico esté activo y la auditoría exista."""
    record = history.get(audit_id)
    if record is None:
        return jsonify({'error': 'auditoría no encontrada (histórico desactivado o expirada)'}), 404

    data = request.get_json(force=True, silent=True) or {}
    snapshot = record.get('snapshot') or {}
    comments = data.get('comments') or {}
    if comments:
        by_id = {c.get('cluster_id'): c for c in (snapshot.get('audit') or {}).get('clusters', [])}
        for cid, raw in comments.items():
            cluster = by_id.get(cid)
            if cluster is not None:
                cluster['presenter_comment'] = (str(raw).strip()[:_MAX_COMMENT_LEN] or None) if raw else None
    if 'official_comment' in data:
        snapshot['official_comment'] = (str(data['official_comment']).strip()[:_MAX_COMMENT_LEN] or '')

    history.save(audit_id, record['name'], record['city'], record['score'],
                 record['total_locations'], snapshot)
    return jsonify({'ok': True})


# ── Geocoding ────────────────────────────────────────────────────

_geocode_cache = {}
_geocode_lock = threading.Lock()


def _geocode_city(city):
    """Resolves a city name to (lat, lng) via Google Geocoding API, cached in memory."""
    return _geocode_address(city)


def _geocode_address(address):
    """Resolves any free-text address (or city name) to (lat, lng) via Google
    Geocoding API, cached in memory."""
    cache_key = address.lower()
    with _geocode_lock:
        if cache_key in _geocode_cache:
            return _geocode_cache[cache_key]

    r = requests.get(
        'https://maps.googleapis.com/maps/api/geocode/json',
        params={'address': address, 'key': os.environ['GOOGLE_PLACES_API_KEY']},
        timeout=10,
    ).json()

    coords = None
    if r.get('status') == 'OK' and r.get('results'):
        loc = r['results'][0]['geometry']['location']
        coords = (loc['lat'], loc['lng'])
    else:
        app.logger.error(f"Geocoding '{address}': {r.get('status')} — {r.get('error_message', '')}")

    with _geocode_lock:
        _geocode_cache[cache_key] = coords
    return coords


def _fill_missing_coords(records):
    """Official records (schema.org data, CSV rows) usually lack lat/lng,
    which breaks entity-resolution's robust distance-based matching and falls
    back to a much more fragile name+address text match. Geocode what's missing."""
    missing = [r for r in records if r['lat'] is None and r['formatted_address']]
    if not missing:
        return records

    with ThreadPoolExecutor(max_workers=10) as pool:
        coords_list = list(pool.map(lambda r: _geocode_address(r['formatted_address']), missing))

    for record, coords in zip(missing, coords_list):
        if coords:
            record['lat'], record['lng'] = coords

    return records


# ── Google Places ──────────────────────────────────────────────

# Google's Text Search is relevance-based, not an exact-brand filter — a
# query like "Movistar Barcelona" comes back full of nearby phone shops that
# merely mention Movistar in passing (a multi-carrier SIM stall) or nothing
# at all (Vodafone/Orange stores Google considers "related" to a telecom
# query in that area). Confirmed live: real results included "Tienda
# Orange", "Vodafone Barcelona - Manso", "Rogent Telefonia", etc., scoring
# token_set_ratio 16-35 against "movistar" — comfortably separated from
# genuine matches ("Tienda Movistar", "Movistar") which score 100.
NAME_MATCH_THRESHOLD = 60
# Bing/Azure's fuzzy search is far looser than Google's Text Search — for
# "Lowi Barcelona" it returns 148 results, almost all unrelated (Bird
# scooters, Donkey Republic bikes, "Low Cost"/"Loli"/"Liwi" shops...). A
# stricter bar than Google's here is justified: genuine matches carry the
# prospect name as an exact token (token_set_ratio 100), while homograph
# noise like "Liwi" tops out ~75, so 80 keeps the real ones and drops it.
AZURE_NAME_MATCH_THRESHOLD = 80

# Apple's /v1/search is proximity-biased and returns nearby POIs that aren't
# the prospect. We filter those by name like Bing, but a bit looser than Azure
# (70): the autocomplete-by-name pass is exempt from this filter (see
# _search_apple) so multi-brand stores like "Lowi/Vodafone Clot" survive.
APPLE_NAME_MATCH_THRESHOLD = 70


# Palabras genéricas de sector: si el comercial busca "NH hoteles" o "Zara
# tiendas", esas fichas en Google se llaman "Hotel NH Barcelona…" / "Zara" —
# sin el token "hoteles"/"tiendas" — y el token_set_ratio se hundía (<60),
# descartando TODAS las sedes. Se prueba también la consulta sin estas palabras.
_GENERIC_NAME_WORDS = {
    'hotel', 'hoteles', 'hotels', 'tienda', 'tiendas', 'store', 'stores', 'shop', 'shops',
    'restaurante', 'restaurantes', 'restaurant', 'farmacia', 'farmacias', 'pharmacy',
    'clinica', 'clinicas', 'supermercado', 'supermercados', 'cafe', 'cafeteria',
    'oficina', 'oficinas', 'sucursal', 'sucursales', 'centro', 'centros',
}


def _strip_generic_words(name_norm):
    return ' '.join(t for t in name_norm.split() if t not in _GENERIC_NAME_WORDS)


def _matches_prospect_name(query_name_norm, place_name, threshold=NAME_MATCH_THRESHOLD):
    cand = normalize.name_norm(place_name)
    if fuzz.token_set_ratio(query_name_norm, cand) >= threshold:
        return True
    # Reintenta sin las palabras genéricas de sector ("NH hoteles" → "nh"),
    # que si no hunden el match contra "Hotel NH Barcelona…".
    core = _strip_generic_words(query_name_norm)
    if core and core != query_name_norm:
        return fuzz.token_set_ratio(core, cand) >= threshold
    return False


def _search_google(name, city, progress=None):
    key = os.environ['GOOGLE_PLACES_API_KEY']
    coords = _geocode_city(city)
    places, page_token = [], None

    while True:
        params = {
            'query': f'{name} {city}',
            'language': 'es',
            'key': key,
        }
        if coords:
            params['location'] = f'{coords[0]},{coords[1]}'
            params['radius'] = 40_000
        if page_token:
            params['pagetoken'] = page_token

        data = requests.get(
            'https://maps.googleapis.com/maps/api/place/textsearch/json',
            params=params,
            timeout=15,
        ).json()

        if data.get('status') not in ('OK', 'ZERO_RESULTS'):
            app.logger.error(f"Google text search: {data.get('status')} — {data.get('error_message','')}")
            break

        places.extend(data.get('results', []))
        page_token = data.get('next_page_token')
        if not page_token:
            break
        time.sleep(2)  # Google requires a short pause before the next page token is valid

    query_name_norm = normalize.name_norm(name)
    places = [p for p in places if _matches_prospect_name(query_name_norm, p.get('name', ''))]
    # Cap del nº de sedes auditadas (coste + tamaño del informe). Se aplica
    # antes de pedir Details, así recorta también las señales SerpApi por sede.
    places = places[:_GOOGLE_MAX_RESULTS]

    def _detail(place):
        r = requests.get(
            'https://maps.googleapis.com/maps/api/place/details/json',
            params={
                'place_id': place['place_id'],
                'fields': 'place_id,name,formatted_address,formatted_phone_number,website,'
                          'rating,user_ratings_total,opening_hours,geometry,reviews,types,'
                          'address_component',
                'reviews_sort': 'newest',
                'language': 'es',
                'key': key,
            },
            timeout=10,
        ).json()
        result = r.get('result') if r.get('status') == 'OK' else None
        if result and _GOOGLE_REVIEW_SUMMARY_ENABLED:
            # Gemini review summary is a paid Places API (New) call that is
            # almost always empty for Spain (rollout not there yet), so it's
            # off by default to save cost — flip ENABLE_GOOGLE_REVIEW_SUMMARY=1
            # to re-enable if/when it starts returning data.
            result['review_summary'] = _google_review_summary(result.get('place_id') or place['place_id'], key)
        return result

    with ThreadPoolExecutor(max_workers=10) as pool:
        details = [d for d in pool.map(_detail, places) if d]

    if _REVIEW_SCRAPING_ENABLED:
        _attach_scraped_reviews(details, progress=progress)

    return sorted(details, key=lambda x: x.get('formatted_address', ''))


@contextmanager
def _scrape_browser():
    """Opens ONE headless Chromium for a scraping worker to reuse across its
    whole chunk of places. Isolated in a helper so tests can stub it out
    (Playwright's sync API is single-threaded, so the browser is created and
    used entirely within the calling worker thread)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            yield browser
        finally:
            browser.close()


def _attach_scraped_reviews(places, progress=None):
    """Best-effort: fetches each Google place's last ~3 months of reviews,
    its action links (Reserve/Order/Menu) and its Posts, attaching them as
    place['scraped_reviews'] / ['scraped_action_links'] / ['scraped_posts']
    so venue_metrics.py can compute real review_rate_3m/reply_rate_3m/
    action_links_google/posts_3m instead of the ≤5-review Places API sample
    or a fixed 'N/D'.

    Source: SerpApi (google_signals) when a SERPAPI_KEY is set — fast HTTP,
    real timestamps + owner responses, no browser; otherwise the Playwright
    scraper (one reused Chromium per worker). A cache pass first means any
    already-cached place skips the fetch entirely (the big re-audit win).
    Never blocks the audit: any per-place failure just leaves it unenriched,
    falling back to the existing approximation / 'N/D'."""
    targets = [p for p in places if p.get('place_id')]
    mode = 'api' if _GOOGLE_SIGNALS_VIA_SERPAPI else 'scrape'

    def emit(message):
        app.logger.info(message)
        if progress:
            progress(message)

    def _apply(place, signals):
        place['scraped_reviews'] = signals.get('reviews') or []
        place['scraped_action_links'] = signals.get('action_links') or []
        place['scraped_posts'] = signals.get('posts') or []

    def _cache_key(place):
        # Mode in the key so switching source doesn't serve the other's data.
        return f"signals:{mode}:{place['place_id']}:{_REVIEW_SCRAPE_MONTHS}"

    # Cache pass first: any place already cached skips the fetch entirely.
    misses, cached = [], 0
    for place in targets:
        hit = cache.get(_cache_key(place))
        if hit is not None:
            _apply(place, hit)
            cached += 1
        else:
            misses.append(place)

    total = len(targets)
    if not total:
        return
    if cached:
        emit(f'Reseñas: {cached}/{total} sede(s) desde caché.')
    if not misses:
        emit(f'Análisis de reseñas completo ({total} sede(s)).')
        return

    done = 0
    done_lock = threading.Lock()

    def _store(place, signals):
        nonlocal done
        label = place.get('name') or place.get('place_id')
        _apply(place, signals)
        # Only cache a result that found something — an all-empty result may
        # be a soft failure (throttle/layout change); let it retry next audit.
        if signals.get('reviews') or signals.get('action_links') or signals.get('posts'):
            cache.set(_cache_key(place), signals)
        with done_lock:
            done += 1
            emit(f'Reseñas recientes: {done}/{len(misses)} — {label} '
                 f'({len(signals.get("reviews") or [])} reseñas, '
                 f'{len(signals.get("action_links") or [])} action links).')

    if _GOOGLE_SIGNALS_VIA_SERPAPI:
        emit(f'Analizando reseñas recientes vía API: 0/{len(misses)} sede(s) '
             f'({_REVIEW_SCRAPE_WORKERS} en paralelo)…')

        def _fetch_one(place):
            nonlocal done
            try:
                # Solo reseñas aquí; los action links se piden aparte y solo
                # para las N peores sedes (ver _attach_action_links_worst).
                signals = google_signals.fetch_place_signals(
                    place['place_id'], months=_REVIEW_SCRAPE_MONTHS, include_action_links=False)
                _store(place, signals)
            except Exception as e:
                with done_lock:
                    done += 1
                emit(f'Reseñas recientes: {done}/{len(misses)} — {place.get("name")} FALLÓ ({e}).')

        with ThreadPoolExecutor(max_workers=_REVIEW_SCRAPE_WORKERS) as pool:
            list(pool.map(_fetch_one, misses))
        emit(f'Análisis de reseñas completo ({total} sede(s)).')
        return

    # Fallback: Playwright scraper, one reused Chromium per worker chunk.
    emit(f'Analizando reseñas recientes: 0/{len(misses)} sede(s) por scrapear '
         f'({_REVIEW_SCRAPE_WORKERS} navegadores en paralelo, hasta {_REVIEW_SCRAPE_MAX_SECONDS}s cada sede)…')

    def _scrape_one(place, browser):
        nonlocal done
        try:
            signals = scrape_place_signals(
                place['place_id'], months=_REVIEW_SCRAPE_MONTHS, max_reviews=_REVIEW_SCRAPE_MAX_REVIEWS,
                max_seconds=_REVIEW_SCRAPE_MAX_SECONDS, browser=browser,
            )
            _store(place, signals)
        except Exception as e:
            with done_lock:
                done += 1
            emit(f'Reseñas recientes: {done}/{len(misses)} — {place.get("name")} FALLÓ ({e}).')

    def _worker(chunk):
        if not chunk:
            return
        try:
            with _scrape_browser() as browser:
                for place in chunk:
                    _scrape_one(place, browser)
        except Exception as e:
            app.logger.warning(f'Review scraping worker failed to start a browser: {e}')

    workers = min(_REVIEW_SCRAPE_WORKERS, len(misses))
    chunks = [misses[i::workers] for i in range(workers)]  # round-robin, balanced
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_worker, chunks))

    emit(f'Análisis de reseñas completo ({total} sede(s)).')


def _google_review_summary(place_id, key):
    """AI-generated (Gemini) summary of a place's reviews — only available via
    Places API (New), a separate endpoint/product from the legacy Places API
    used everywhere else in this file. Best-effort: many locations won't have
    one yet depending on language/region rollout, and the New API must be
    enabled separately in the Google Cloud project.
    """
    try:
        r = requests.get(
            f'https://places.googleapis.com/v1/places/{place_id}',
            headers={'X-Goog-Api-Key': key, 'X-Goog-FieldMask': 'reviewSummary'},
            timeout=10,
        )
        if not r.ok:
            return None
        summary = r.json().get('reviewSummary')
        return (summary or {}).get('text', {}).get('text')
    except requests.RequestException:
        return None


# ── Apple Maps ─────────────────────────────────────────────────

def _apple_reshape(item):
    """Reshapes a raw Apple Maps Place (from /v1/search or a resolved
    autocomplete completionUrl) into our common per-source dict. Apple's own
    API returns neither phone nor website (fields absent from the response) —
    those stay None until SerpApi enrichment fills them; category comes from
    `poiCategory`."""
    coord = item.get('coordinate') or {}
    return {
        'id':                item.get('id'),
        'name':              item.get('name'),
        'formatted_address': ', '.join(item.get('formattedAddressLines', [])) or None,
        'phone_number':      item.get('phoneNumber'),
        'url':               item.get('url'),
        'category':          item.get('poiCategory'),
        'lat':               coord.get('latitude'),
        'lng':               coord.get('longitude'),
    }


def _search_apple(name, city, extra_anchors=None, google_places=None):
    """Apple's `/v1/search` is proximity-biased, not an exhaustive chain
    search like Google's Text Search — confirmed live: querying "NH" anchored
    at Barcelona's city center returned only 2 hotels, while the identical
    query anchored at one specific hotel's own coordinates returned 10,
    including the one the city-wide search missed. A single broad query never
    even gets a `pageToken` back for these chain-name searches, so there's
    nothing to paginate through — the API is just handing back "closest
    matches to this one point."

    So beyond the original city-wide search, this also re-runs the same
    query anchored at each of `extra_anchors` (Google's confirmed location
    coordinates, passed in by the caller once Google's search has
    completed) and merges everything by POI id — this is what actually
    surfaces chain locations spread across the city that the single broad
    query would otherwise miss entirely.
    """
    headers = {'Authorization': f'Bearer {_apple_access_token()}'}

    def _search_near(anchor):
        results, page_token = [], None
        while True:
            params = {
                'q': f'{name} {city}',
                'lang': 'es-ES',
                'limitToCountries': 'ES',
                'resultTypeFilter': 'Poi',
            }
            if anchor:
                params['searchLocation'] = f'{anchor[0]},{anchor[1]}'
            if page_token:
                params['pageToken'] = page_token

            r = requests.get(
                'https://maps-api.apple.com/v1/search',
                params=params,
                headers=headers,
                timeout=15,
            )
            if not r.ok:
                app.logger.error(f'Apple Maps {r.status_code}: {r.text[:200]}')
                break

            data = r.json()
            results.extend(_apple_reshape(item) for item in data.get('results', []))

            page_token = data.get('pageToken')
            if not page_token:
                break

        return results

    def _autocomplete_by_name(place):
        """Discovers the Apple POI for one specific Google location, querying
        /v1/searchAutocomplete with that location's OWN name anchored at its
        coordinates, then resolving the best name-matching suggestion to a
        full POI. This is what actually surfaces multi-brand/franchise stores
        (e.g. "Lowi/Vodafone Clot") that a generic single-brand /v1/search
        query never returns — confirmed live."""
        pname, lat, lng = place.get('name'), place.get('lat'), place.get('lng')
        if not pname or lat is None or lng is None:
            return []
        try:
            r = requests.get(
                'https://maps-api.apple.com/v1/searchAutocomplete',
                params={'q': pname, 'lang': 'es-ES', 'searchLocation': f'{lat},{lng}'},
                headers=headers, timeout=15,
            )
            if not r.ok:
                return []
            suggestions = r.json().get('results', [])
        except Exception as e:
            app.logger.warning(f'Apple autocomplete failed for {pname!r}: {e}')
            return []

        target = normalize.name_norm(pname)
        results, resolved = [], 0
        for s in suggestions:
            if resolved >= _APPLE_AUTOCOMPLETE_MAX_RESOLVE:
                break
            completion = s.get('completionUrl')
            cand_name = (s.get('displayLines') or [''])[0]
            if not completion:
                continue
            if fuzz.token_set_ratio(target, normalize.name_norm(cand_name)) < _APPLE_AUTOCOMPLETE_NAME_SIM_MIN:
                continue
            try:
                rr = requests.get('https://maps-api.apple.com' + completion, headers=headers, timeout=15)
                resolved += 1
                if rr.ok:
                    results.extend(_apple_reshape(item) for item in rr.json().get('results', []))
            except Exception:
                continue
        return results

    # /v1/search is proximity-biased and pulls in unrelated nearby POIs, so
    # filter those by prospect name (like Bing). The autocomplete pass below is
    # NOT filtered — it's name-anchored already and carries the multi-brand
    # stores a strict name filter would wrongly drop.
    query_norm = normalize.name_norm(name)

    def _named(items):
        return [it for it in items
                if _matches_prospect_name(query_norm, it.get('name', ''), APPLE_NAME_MATCH_THRESHOLD)]

    merged = {}
    for item in _named(_search_near(_geocode_city(city))):
        merged[item['id']] = item

    if extra_anchors:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for batch in pool.map(_search_near, extra_anchors):
                for item in _named(batch):
                    merged.setdefault(item['id'], item)

    # Second pass: autocomplete each Google location by its OWN name — adds
    # venues /v1/search misses (merged by id, never overwrites). Exempt from the
    # name filter above.
    if google_places:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for batch in pool.map(_autocomplete_by_name, google_places):
                for item in batch:
                    merged.setdefault(item['id'], item)

    return list(merged.values())


# ── Apple enrichment via SerpApi (phone/website/hours/rating/reviews) ──

def _serpapi_weekly_hours_to_list(weekly_hours):
    """Converts SerpApi's `weekly_hours` (a {day: hours-text} dict, or a list
    of {day, hours} objects) into the same normalized
    ["Lunes: 09:00–20:00", ...] list shape every other source uses, so
    normalize.parse_hours / the accuracy comparator can read Apple hours
    day-by-day like any other. Unknown days / empty values are skipped."""
    if not weekly_hours:
        return None
    if isinstance(weekly_hours, dict):
        items = list(weekly_hours.items())
    else:
        items = [(d.get('day') or d.get('name'), d.get('hours') or d.get('time'))
                 for d in weekly_hours if isinstance(d, dict)]
    out = []
    for day, value in items:
        idx = normalize._day_index(str(day)) if day else None
        if idx is None or not value:
            continue
        # Collapse any dash variant with surrounding spaces ("09:00 – 20:00")
        # into a bare "–" so the format matches every other source.
        text = re.sub(r'\s*[–—-]\s*', '–', str(value)).strip()
        out.append((idx, f'{_DAY_NAMES_ES[idx]}: {text}'))
    out.sort(key=lambda pair: pair[0])
    return [line for _, line in out] or None


def _serpapi_apple_lookup(name, lat, lng):
    """Looks up one Apple Maps place via SerpApi near (lat,lng), returning the
    best name-matching `local_results` entry (or None). Best-effort: any
    failure/miss returns None so enrichment silently leaves the record as-is.

    Cached by name+coords (including negatives, which are stable for niche
    brands): SerpApi is the audit's main variable cost, so never pay twice
    for the same lookup. A cache miss vs a cached-None (`{'match': None}`)
    are distinguished so a genuine 'not found' isn't re-queried."""
    if not _APPLE_SERPAPI_ENABLED or lat is None or lng is None or not name:
        return None

    cache_key = f'serpapi_apple:{normalize.name_norm(name)}:{lat:.5f}:{lng:.5f}'
    hit = cache.get(cache_key)
    if hit is not None:
        return hit.get('match')

    try:
        r = requests.get('https://serpapi.com/search', params={
            'engine': 'apple_maps', 'query': name,
            'center': f'{lat},{lng}', 'span': '0.05,0.05',
            'api_key': _SERPAPI_KEY,
        }, timeout=15)
        if not r.ok:
            app.logger.warning(f'SerpApi Apple {r.status_code}: {r.text[:150]}')
            return None  # transient/HTTP error → don't cache, retry next audit
        results = r.json().get('local_results') or []
    except Exception as e:
        app.logger.warning(f'SerpApi Apple lookup failed for {name!r}: {e}')
        return None

    query_norm = normalize.name_norm(name)
    best, best_sim = None, 0
    for res in results:
        sim = fuzz.token_set_ratio(query_norm, normalize.name_norm(res.get('title', '')))
        if sim > best_sim:
            best, best_sim = res, sim
    match = best if best and best_sim >= _APPLE_SERPAPI_NAME_SIM_MIN else None
    cache.set(cache_key, {'match': match})  # cache success AND clean 'no match'
    return match


# Apple enrichment now runs per-cluster (only venues matched to Google) in
# _enrich_apple_clusters / _enrich_apple_record — see _build_audit.


# ── Azure Maps ─────────────────────────────────────────────────

_DAY_NAMES_ES = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']


def _azure_opening_hours(poi):
    """Azure's `openingHours=nextSevenDays` param returns a *rolling 7-day
    window* of absolute date+time ranges (not a repeating weekly schedule
    like Google's `weekday_text`) — each range is bucketed onto its
    `startTime.date`'s weekday and reformatted into the same
    "Día: HH:MM–HH:MM" shape `normalize.parse_hours` already understands, so
    it can be compared against Google/official hours day-by-day like any
    other source. A day with no time range in the 7-day window is simply
    absent from the result (not marked 'closed') — Azure's coverage isn't
    guaranteed per-POI, and treating "no data this week" as "closed" would
    manufacture false hours-mismatch flags."""
    ranges = ((poi.get('openingHours') or {}).get('timeRanges')) or []
    by_day = {}
    for r in ranges:
        start, end = r.get('startTime') or {}, r.get('endTime') or {}
        date_str = start.get('date')
        if not date_str:
            continue
        try:
            day_idx = datetime.date.fromisoformat(date_str).weekday()
        except ValueError:
            continue
        label = f"{start.get('hour', 0):02d}:{start.get('minute', 0):02d}–{end.get('hour', 0):02d}:{end.get('minute', 0):02d}"
        by_day.setdefault(day_idx, []).append(label)

    if not by_day:
        return None
    return [f'{_DAY_NAMES_ES[day_idx]}: {", ".join(labels)}' for day_idx, labels in sorted(by_day.items())]


def _search_azure(name, city, extra_anchors=None):
    """Azure's Fuzzy Search ranks by relevance to the query text near a
    single center point, the same "closest/most relevant matches to this
    one point" shape that made Apple's city-wide-only search miss chain
    locations spread across a city (see `_search_apple`) — Azure never got
    the same per-location anchor fix until now, and was still capped at a
    single city-wide call (hard limit of 200 results via `ofs`/`limit`).

    Beyond the original city-wide search, this also re-runs the same query
    anchored at each of `extra_anchors` (Google's confirmed location
    coordinates) with a tight radius — since the anchor is already known to
    be near the real location, a small radius keeps each anchored call
    fast and focused rather than re-scanning the whole city — and merges
    everything by POI id, mirroring `_search_apple`'s merge logic exactly.
    """
    key = os.environ['AZURE_MAPS_SUBSCRIPTION_KEY']
    query_name_norm = normalize.name_norm(name)

    def _search_near(coords, radius, limit):
        results, offset = [], 0
        while True:
            params = {
                'api-version': '1.0',
                'query': f'{name} {city}',
                'limit': limit,
                'ofs': offset,
                'countrySet': 'ES',
                'language': 'es-ES',
                'subscription-key': key,
                # Opting in — without this the fuzzy-search response has no
                # opening-hours field at all, even when Azure has the data.
                'openingHours': 'nextSevenDays',
            }
            if coords:
                params['lat'] = coords[0]
                params['lon'] = coords[1]
                params['radius'] = radius

            r = requests.get(
                'https://atlas.microsoft.com/search/fuzzy/json',
                params=params,
                timeout=15,
            )
            if not r.ok:
                app.logger.error(f'Azure Maps {r.status_code}: {r.text[:200]}')
                break

            items = r.json().get('results', [])
            for item in items:
                if item.get('type') != 'POI':
                    continue
                poi = item.get('poi') or {}
                # Drop the flood of unrelated fuzzy matches (Bird/Donkey/"Low
                # Cost"/"Loli"...) — keep only POIs whose name actually matches
                # the prospect, same idea as Google (_matches_prospect_name)
                # but with a stricter threshold for Bing's noisier search.
                if not _matches_prospect_name(query_name_norm, poi.get('name', ''),
                                              threshold=AZURE_NAME_MATCH_THRESHOLD):
                    continue
                address = item.get('address') or {}
                position = item.get('position') or {}
                categories = poi.get('categories') or []
                results.append({
                    'id':                item.get('id'),
                    'name':              poi.get('name'),
                    'formatted_address': address.get('freeformAddress'),
                    'phone_number':      poi.get('phone'),
                    'url':               poi.get('url'),
                    'category':          ', '.join(categories) if categories else None,
                    'lat':               position.get('lat'),
                    'lng':               position.get('lon'),
                    'opening_hours':     _azure_opening_hours(poi),
                })

            offset += limit
            if len(items) < limit or offset >= 200:
                break

        return results

    def _search_anchor(anchor):
        return _search_near(anchor, radius=1_000, limit=20)

    merged = {}
    for item in _search_near(_geocode_city(city), radius=50_000, limit=100):
        merged[item['id']] = item

    if extra_anchors:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for batch in pool.map(_search_anchor, extra_anchors):
                for item in batch:
                    merged.setdefault(item['id'], item)

    return list(merged.values())


_apple_token_cache = {'token': None, 'expires_at': 0}
_apple_token_lock = threading.Lock()


def _apple_jwt():
    key_path = os.path.join(os.path.dirname(__file__), os.environ['APPLE_MAPS_PRIVATE_KEY_PATH'])
    with open(key_path, 'rb') as f:
        private_key = load_pem_private_key(f.read(), password=None)

    now = int(time.time())
    return jwt.encode(
        {'iss': os.environ['APPLE_MAPS_TEAM_ID'], 'iat': now, 'exp': now + 3600},
        private_key,
        algorithm='ES256',
        headers={'kid': os.environ['APPLE_MAPS_KEY_ID'], 'typ': 'JWT'},
    )


def _apple_access_token():
    # The .p8-signed JWT isn't accepted directly by /v1/search — it must
    # first be exchanged for a short-lived access token via /v1/token.
    with _apple_token_lock:
        if time.time() < _apple_token_cache['expires_at']:
            return _apple_token_cache['token']

        r = requests.get(
            'https://maps-api.apple.com/v1/token',
            headers={'Authorization': f'Bearer {_apple_jwt()}'},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        _apple_token_cache['token'] = data['accessToken']
        _apple_token_cache['expires_at'] = time.time() + data['expiresInSeconds'] - 60
        return _apple_token_cache['token']


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    print(f'\n  → http://localhost:{port}\n')
    app.run(port=port, debug=True)
