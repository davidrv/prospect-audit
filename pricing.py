"""Modelo de coste por auditoría — para mostrar un "coste máximo" en el input
antes de ejecutar.

El nº de sedes no se conoce antes de correr, así que el estimador trabaja con
un supuesto FIJO: `google_max` sedes (el cap `GOOGLE_MAX_RESULTS`, 25). Es un
techo: si se encuentran menos, el coste real es menor.

Precios por llamada en EUR como **defaults configurables por env** (valores
orientativos por proveedor; ajústalos a tus planes). Se leen en tiempo de
llamada para que tests/ops puedan sobreescribirlos.
"""
import math
import os

_DEFAULT_PRICES = {
    'textsearch': ('PRICE_GOOGLE_TEXTSEARCH', 0.030),   # Google Places Text Search
    'details': ('PRICE_GOOGLE_DETAILS', 0.023),         # Google Place Details (basic+contact+atmosphere)
    'geocode': ('PRICE_GOOGLE_GEOCODE', 0.005),         # Google Geocoding
    'azure': ('PRICE_AZURE_SEARCH', 0.0005),            # Azure Maps Fuzzy Search
    'serpapi': ('PRICE_SERPAPI', 0.010),                # SerpApi (por búsqueda)
    'cloro': ('PRICE_CLORO', 0.020),                    # Cloro (por llamada a ChatGPT)
}


def _prices():
    out = {}
    for key, (env, default) in _DEFAULT_PRICES.items():
        try:
            out[key] = float(os.environ.get(env, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


def estimate_max(*, google_max, reviews_pages, cloro_venues, cloro_runs):
    """Coste máximo por auditoría (EUR) con el supuesto de `google_max` sedes.
    Devuelve `maps_max` (todo menos IA) y `llm_delta` (la fase de visibilidad IA,
    que el input suma al marcar el checkbox)."""
    p = _prices()
    ts_pages = max(1, math.ceil(google_max / 20))  # ~20 resultados por página de Text Search

    maps = (
        ts_pages * p['textsearch']
        + google_max * p['details']
        + 1 * p['geocode']
        + (2 + google_max) * p['azure']                       # Azure: 2 city-wide + 1 por ancla Google
        + google_max * (reviews_pages + 1) * p['serpapi']     # Google signals: reviews + action links por sede
        + google_max * p['serpapi']                           # Apple enrich: ≤1 por sede con match en Google
    )
    llm = cloro_venues * cloro_runs * p['cloro']

    return {
        'currency': 'EUR',
        'maps_max': round(maps, 2),
        'llm_delta': round(llm, 2),
        'assumptions': {
            'google_max': google_max, 'reviews_pages': reviews_pages,
            'cloro_venues': cloro_venues, 'cloro_runs': cloro_runs,
        },
        'prices': p,
    }
