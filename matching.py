"""Entity resolution: groups normalized records from different sources that
represent the same physical location, via geographic proximity + name/address
similarity (Union-Find over pairwise matches).
"""
import math

from rapidfuzz import fuzz

STRICT_DISTANCE_M = 30      # same building — match regardless of name
MATCH_DISTANCE_M = 200       # typical geocoder discrepancy between providers —
                             # verified against a real chain (Movistar, Barcelona):
                             # a genuine same-store Google/Apple pair sat 187m apart
                             # (different street geocoded per provider), while the
                             # closest two genuinely distinct real branches sat 466.8m
                             # apart — 200m covers the former with room to spare
                             # before risking the latter.
MATCH_NAME_SIM_MIN = 60
FALLBACK_NAME_SIM_MIN = 85   # used when neither record has coordinates
FALLBACK_ADDR_SIM_MIN = 70


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def _is_match(a, b):
    dist_m = None
    if None not in (a['lat'], a['lng'], b['lat'], b['lng']):
        dist_m = haversine_m(a['lat'], a['lng'], b['lat'], b['lng'])

    name_sim = fuzz.token_sort_ratio(a['name_norm'], b['name_norm']) if a['name_norm'] and b['name_norm'] else 0

    if dist_m is not None:
        if dist_m <= STRICT_DISTANCE_M:
            return True
        return dist_m <= MATCH_DISTANCE_M and name_sim >= MATCH_NAME_SIM_MIN

    addr_sim = fuzz.token_sort_ratio(a['address_norm'], b['address_norm']) if a['address_norm'] and b['address_norm'] else 0
    return name_sim >= FALLBACK_NAME_SIM_MIN and addr_sim >= FALLBACK_ADDR_SIM_MIN


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


def cluster_records(records):
    """Agrupa records de la misma sede física, **anclado a Google** (la fuente
    de referencia): cada record de Google siembra su propia sede (dos Google
    NUNCA se fusionan), y cada record de otra fuente se asigna a la sede Google
    que casa (`_is_match`) y está MÁS CERCA. Esto evita el sobre-agrupamiento
    por transitividad del Union-Find (p.ej. dos sedes Google unidas por un POI
    puente de Apple/Bing) y garantiza comparar la ficha correcta contra Google.

    Los records que no casan con ningún Google se agrupan entre sí con Union-Find
    (comportamiento anterior) → sedes sin ficha en Google."""
    google = [r for r in records if r['source'] == 'google']
    others = [r for r in records if r['source'] != 'google']

    groups = [[g] for g in google]  # una sede por ficha de Google
    unassigned = []
    for record in others:
        best_i, best_key = None, None
        for i, g in enumerate(google):
            if not _is_match(record, g):
                continue
            key = _match_distance_key(record, g)  # menor = mejor
            if best_i is None or key < best_key:
                best_i, best_key = i, key
        if best_i is not None:
            groups[best_i].append(record)
        else:
            unassigned.append(record)

    groups.extend(_union_find_groups(unassigned))  # sedes sin Google (ghost)

    return [_build_cluster(i + 1, group) for i, group in enumerate(groups)]


def _match_distance_key(record, anchor):
    """Clave de "cercanía" a un ancla (menor = mejor). Los matches con
    coordenadas (nivel 0) siempre ganan a los de solo-nombre (nivel 1);
    dentro de cada nivel, por distancia en metros / por menor disparidad de
    nombre."""
    if None not in (record['lat'], record['lng'], anchor['lat'], anchor['lng']):
        return (0, haversine_m(record['lat'], record['lng'], anchor['lat'], anchor['lng']))
    name_sim = (fuzz.token_sort_ratio(record['name_norm'], anchor['name_norm'])
                if record['name_norm'] and anchor['name_norm'] else 0)
    return (1, 100 - name_sim)


def _union_find_groups(records):
    """Agrupamiento transitivo (Union-Find) para records sin match con Google.
    No compara dos records de la misma fuente (igual que antes)."""
    uf = _UnionFind(len(records))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if records[i]['source'] == records[j]['source']:
                continue
            if _is_match(records[i], records[j]):
                uf.union(i, j)
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(uf.find(i), []).append(record)
    return list(groups.values())


_LABEL_SOURCE_PRIORITY = ('google', 'official', 'apple', 'azure')


def _best_record(recs, anchor):
    """De varios records de la MISMA fuente en un cluster, el más cercano al
    ancla Google (o el primero si no hay ancla/coincidencia de coords)."""
    if len(recs) == 1 or anchor is None:
        return recs[0]
    return min(recs, key=lambda r: _match_distance_key(r, anchor))


def _build_cluster(index, group):
    anchor = next((r for r in group if r['source'] == 'google'), None)
    per_source = {}
    for r in group:
        per_source.setdefault(r['source'], []).append(r)

    # Best-per-source: si una fuente aportó ≥2 fichas, nos quedamos con la más
    # cercana a Google (no la primera del proveedor). Los extras siguen en
    # `records`. `ambiguous` marca ese caso (posible ficha duplicada).
    by_source = {source: _best_record(recs, anchor) for source, recs in per_source.items()}
    ambiguous = any(len(recs) > 1 for recs in per_source.values())
    chosen = list(by_source.values())

    return {
        'cluster_id': f'L{index}',
        'records': group,
        'by_source': by_source,
        'sources_present': sorted(by_source.keys()),
        'ambiguous': ambiguous,
        'canonical_label': _pick_by_priority(chosen, 'name') or 'Sede sin nombre',
        'canonical_address': _pick_by_priority(chosen, 'formatted_address'),
        'lat': _pick_by_priority(chosen, 'lat'),
        'lng': _pick_by_priority(chosen, 'lng'),
    }


def _pick_by_priority(group, field):
    for source in _LABEL_SOURCE_PRIORITY:
        for r in group:
            if r['source'] == source and r.get(field):
                return r[field]
    for r in group:
        if r.get(field):
            return r[field]
    return None
