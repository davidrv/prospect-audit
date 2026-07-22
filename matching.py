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
    """Groups records representing the same physical location across sources."""
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

    return [_build_cluster(i + 1, group) for i, group in enumerate(groups.values())]


_LABEL_SOURCE_PRIORITY = ('google', 'official', 'apple', 'azure')


def _build_cluster(index, group):
    sources_in_group = [r['source'] for r in group]
    by_source = {}
    for r in group:
        by_source.setdefault(r['source'], r)  # first record per source wins if ambiguous

    return {
        'cluster_id': f'L{index}',
        'records': group,
        'by_source': by_source,
        'sources_present': sorted(by_source.keys()),
        'ambiguous': len(sources_in_group) != len(set(sources_in_group)),
        'canonical_label': _pick_by_priority(group, 'name') or 'Sede sin nombre',
        'canonical_address': _pick_by_priority(group, 'formatted_address'),
        'lat': _pick_by_priority(group, 'lat'),
        'lng': _pick_by_priority(group, 'lng'),
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
