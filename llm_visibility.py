"""Visibilidad en IA (Visibility Index) vía Cloro.dev.

Por cada una de las peores sedes con ficha en Google, construye un prompt de
intención local ("{categoría} en {zona}, {ciudad}") y lo lanza N veces a
ChatGPT (a través de la API de Cloro), midiendo en cuántas repeticiones aparece
el prospecto. `hits/N` por sede es la señal de consistencia (GPT-5 con web
search no es determinista, de ahí las repeticiones).

Acotado y best-effort:
- Solo se ejecuta si hay `CLORO_KEY` y el comercial marcó el checkbox del input.
- Tope de sedes (`max_venues`) y repeticiones (`runs`) para controlar el coste
  (cada llamada consume créditos de Cloro).
- Cachea el resultado por sede (mismo prompt) para que re-auditar sea gratis.
- Nunca lanza: cualquier fallo degrada a "sin dato" para esa repetición.

Cloro API: POST https://api.cloro.dev/v1/monitor/chatgpt
  headers: Authorization: Bearer $CLORO_KEY
  body: {prompt, country, include:{markdown:false}}
  respuesta: {result: {text, model, sources:[{position,url,label,description}],
              entities:[{name,type}], shoppingCards:[{brand,...}], ...}}
"""
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

import cache
import normalize

_CLORO_URL = 'https://api.cloro.dev/v1/monitor/chatgpt'
_HTTP_ATTEMPTS = 3
_HTTP_TIMEOUT = 60
_NAME_PARTIAL_MIN = 90  # umbral rapidfuzz para "la marca aparece en este texto"

# Categoría (Google Places `types`) → frase de intención local en español.
_CATEGORY_MAP = {
    'cell_phone_store': 'tienda de telefonía móvil',
    'telecommunications_service_provider': 'tienda de telefonía móvil',
    'electronics_store': 'tienda de electrónica',
    'pharmacy': 'farmacia',
    'drugstore': 'farmacia',
    'restaurant': 'restaurante',
    'meal_takeaway': 'restaurante para llevar',
    'cafe': 'cafetería',
    'bakery': 'panadería',
    'bar': 'bar',
    'clothing_store': 'tienda de ropa',
    'shoe_store': 'zapatería',
    'jewelry_store': 'joyería',
    'supermarket': 'supermercado',
    'grocery_or_supermarket': 'supermercado',
    'bank': 'banco',
    'insurance_agency': 'agencia de seguros',
    'real_estate_agency': 'inmobiliaria',
    'car_dealer': 'concesionario de coches',
    'car_repair': 'taller mecánico',
    'hair_care': 'peluquería',
    'beauty_salon': 'centro de estética',
    'gym': 'gimnasio',
    'doctor': 'clínica',
    'dentist': 'clínica dental',
    'hospital': 'clínica',
    'veterinary_care': 'clínica veterinaria',
    'lodging': 'hotel',
    'furniture_store': 'tienda de muebles',
    'hardware_store': 'ferretería',
    'book_store': 'librería',
    'store': 'tienda',
}


def _key():
    return os.environ.get('CLORO_KEY', '').strip()


def _get_result(session, prompt, country):
    """POST a Cloro con reintentos en fallos transitorios. Devuelve el objeto
    `result` (o el propio JSON si no viene envuelto), o None."""
    # Cloro exige el código de país ISO en mayúsculas (ej. 'ES', no 'es').
    payload = {'prompt': prompt, 'country': (country or 'ES').upper(), 'include': {'markdown': False}}
    headers = {'Authorization': 'Bearer ' + _key(), 'Content-Type': 'application/json'}
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            r = session.post(_CLORO_URL, json=payload, headers=headers, timeout=_HTTP_TIMEOUT)
            if r.ok:
                data = r.json()
                return data.get('result') if isinstance(data, dict) and 'result' in data else data
            if r.status_code in (429, 500, 502, 503, 504) and attempt < _HTTP_ATTEMPTS - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < _HTTP_ATTEMPTS - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def _name_in(name_q, haystack_norm):
    """¿Aparece la marca (name_q ya normalizado) en un texto normalizado?
    Word-boundary exacto o coincidencia parcial alta (rapidfuzz)."""
    if not name_q or not haystack_norm:
        return False
    if re.search(r'\b' + re.escape(name_q) + r'\b', haystack_norm):
        return True
    return fuzz.partial_ratio(name_q, haystack_norm) >= _NAME_PARTIAL_MIN


def _detect(result, name_q):
    """¿Aparece el prospecto en esta respuesta de ChatGPT? Con posición si se
    cita en `sources`. Devuelve {'appears': bool, 'position': int|None,
    'label': str|None}."""
    result = result or {}
    best_pos, best_label = None, None
    for s in (result.get('sources') or []):
        label = s.get('label') or ''
        dom = urlparse(s.get('url') or '').netloc
        if _name_in(name_q, normalize.name_norm(label + ' ' + dom)):
            pos = s.get('position')
            if isinstance(pos, int) and (best_pos is None or pos < best_pos):
                best_pos, best_label = pos, label or dom
            elif best_label is None:
                best_label = label or dom
    if best_label is not None:
        return {'appears': True, 'position': best_pos, 'label': best_label}

    for e in (result.get('entities') or []):
        if _name_in(name_q, normalize.name_norm(e.get('name') or '')):
            return {'appears': True, 'position': None, 'label': e.get('name')}
    for c in (result.get('shoppingCards') or []):
        if _name_in(name_q, normalize.name_norm(c.get('brand') or '')):
            return {'appears': True, 'position': None, 'label': c.get('brand')}
    if _name_in(name_q, normalize.name_norm(result.get('text') or '')):
        return {'appears': True, 'position': None, 'label': None}
    return {'appears': False, 'position': None, 'label': None}


# Tipos de Google demasiado genéricos para un prompt de intención local.
_GENERIC_TYPES = {'point_of_interest', 'establishment', 'premise', 'geocode', 'food'}


def _category_for(cluster):
    """Categoría para el prompt, priorizando la de Google Places:
    1) `types` de Google mapeados a una frase ES (prefiere el más específico
       sobre el genérico 'tienda');
    2) categoría legible de Apple/Bing/oficial si Google no da nada;
    3) un `type` específico de Google humanizado (guiones bajos → espacios);
    4) 'negocio' como último recurso."""
    by_source = cluster.get('by_source') or {}
    types = ((by_source.get('google') or {}).get('raw') or {}).get('types') or []

    mapped = [_CATEGORY_MAP[t] for t in types if t in _CATEGORY_MAP]
    specific = [m for m in mapped if m != 'tienda']
    if specific:
        return specific[0]
    if mapped:
        return mapped[0]

    for source in ('apple', 'azure', 'official'):
        cat = (by_source.get(source) or {}).get('category')
        if cat and cat.strip():
            return cat.strip()

    for t in types:
        if t not in _GENERIC_TYPES:
            return t.replace('_', ' ')
    return 'negocio'


def _area_from_address(address):
    """Zona para el prompt: el primer segmento de la dirección (la calle),
    sin el número, como ancla local por-sede. None si no hay nada usable."""
    if not address:
        return None
    first = address.split(',')[0].strip()
    first = re.sub(r'\s*\d+\s*$', '', first).strip()  # quita el número de portal final
    return first or None


def _build_prompt(cluster, city, category=None):
    # `category` manual (del input) tiene prioridad; si no, se infiere de Places.
    category = (category or '').strip() or _category_for(cluster)
    area = _area_from_address(cluster.get('canonical_address') or '')
    if area and normalize.name_norm(area) != normalize.name_norm(city):
        return f'{category} en {area}, {city}'
    return f'{category} en {city}'


def _venue_visibility(prompt, name_q, runs, country, session):
    """N repeticiones del mismo prompt → {prompt, runs:[...], hits}. Cacheado
    por (country, runs, prompt) para que re-auditar sea gratis. `_calls` es el
    nº de llamadas HTTP reales hechas (0 si vino de caché)."""
    ck = 'cloro:vis:%s:%d:%s' % (country, runs, hashlib.md5(prompt.encode('utf-8')).hexdigest())
    cached = cache.get(ck)
    if cached is not None:
        return {**cached, '_calls': 0}

    run_results, calls = [], 0
    for _ in range(runs):
        result = _get_result(session, prompt, country)
        calls += 1
        run_results.append(_detect(result, name_q) if result is not None
                           else {'appears': None, 'position': None, 'label': None})

    hits = sum(1 for r in run_results if r.get('appears'))
    out = {'prompt': prompt, 'runs': run_results, 'hits': hits}
    if any(r.get('appears') is not None for r in run_results):  # no cachear un fallo total
        cache.set(ck, out)
    return {**out, '_calls': calls}


def fetch_llm_visibility(clusters, prospect_name, city, *, runs=3, max_venues=5,
                         country='es', session=None, workers=5, progress=None, category=None):
    """Visibility Index (ChatGPT) para las `max_venues` peores sedes con ficha
    en Google. Comprueba las sedes EN PARALELO (hasta `workers` a la vez) —
    cada llamada a ChatGPT-con-web-search es lenta (~10–40s), así que el pool
    recorta el tiempo de pared ~Nx. Emite progreso por sede vía `progress`.
    `category` (opcional) fija a mano la categoría del prompt para todas las
    sedes (p.ej. "tienda de móviles", "proveedor de internet"); si es vacía se
    infiere de Google Places. Devuelve el agregado + por-sede. Never raises."""
    empty = {'engine': 'chatgpt', 'prompt_template': None, 'venues_checked': 0,
             'runs': runs, 'checks_total': 0, 'hits_total': 0, 'per_venue': {}, 'calls': 0,
             'category': (category or '').strip() or None}
    if not _key() or not clusters:
        return empty

    sess = session or requests
    name_q = normalize.name_norm(prospect_name)
    manual_category = (category or '').strip() or None
    # Los clusters ya vienen ordenados peor→mejor (venue_metrics); las peores
    # sedes son las más accionables para la conversación de venta.
    targets = [c for c in clusters if 'google' in c.get('sources_present', [])][:max_venues]
    total = len(targets)

    def emit(done):
        if progress:
            progress(f'Visibilidad en IA: {done}/{total} sede(s) comprobadas…')

    prompts = {c['cluster_id']: _build_prompt(c, city, manual_category) for c in targets}

    def work(cluster):
        return cluster['cluster_id'], _venue_visibility(prompts[cluster['cluster_id']], name_q, runs, country, sess)

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, total or 1))) as pool:
        futures = [pool.submit(work, c) for c in targets]
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                cid, res = future.result()
                results[cid] = res
            except Exception:
                pass
            emit(done)

    per_venue, checks_total, hits_total, calls = {}, 0, 0, 0
    for cluster in targets:
        res = results.get(cluster['cluster_id']) or {'prompt': prompts[cluster['cluster_id']], 'runs': [], 'hits': 0}
        per_venue[cluster['cluster_id']] = {'prompt': res['prompt'], 'runs': res['runs'], 'hits': res['hits']}
        checks_total += len(res['runs'])
        hits_total += res['hits']
        calls += res.get('_calls', 0)

    cat_label = manual_category or '{categoría}'
    return {'engine': 'chatgpt', 'prompt_template': f'{cat_label} en {{zona}}, {city}',
            'category': manual_category, 'venues_checked': total, 'runs': runs,
            'checks_total': checks_total, 'hits_total': hits_total,
            'per_venue': per_venue, 'calls': calls}
