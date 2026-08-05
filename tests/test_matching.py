import matching
import normalize


def _rec(source, name, address, lat=None, lng=None):
    return normalize.make_record(source, f'{source}-{name}', name=name, formatted_address=address, lat=lat, lng=lng)


def test_haversine_zero_distance():
    assert matching.haversine_m(41.38, 2.17, 41.38, 2.17) == 0


def test_haversine_known_distance():
    d = matching.haversine_m(0, 0, 1, 0)  # ~111km per degree of latitude
    assert 110_000 < d < 112_000


def test_cluster_records_matches_by_close_coordinates():
    a = _rec('google', "McDonald's", 'Calle Pelai 62', lat=41.3854, lng=2.1697)
    b = _rec('apple', "McDonald's", 'Calle Pelai 62', lat=41.38545, lng=2.16965)  # ~6m away
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 1
    assert clusters[0]['sources_present'] == ['apple', 'google']


def test_cluster_records_no_match_when_far_apart():
    a = _rec('google', "McDonald's", 'Calle Pelai 62', lat=41.3854, lng=2.1697)
    b = _rec('apple', "McDonald's", 'Calle Diagonal 400', lat=41.40, lng=2.20)
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 2


def test_cluster_records_fallback_without_coordinates():
    a = _rec('official', "McDonald's Pelayo", 'Calle Pelai, 62, Barcelona')
    b = _rec('google', "McDonald's Pelayo", 'Calle Pelai, 62, Barcelona')
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 1


def test_two_google_records_never_merge():
    # Anclado a Google: dos fichas de Google son dos sedes distintas aunque
    # compartan coords; el Apple se asigna a una (no las fusiona en un cluster
    # ambiguo como hacía el Union-Find transitivo).
    a = _rec('google', "McDonald's Uno", 'X', lat=41.0, lng=2.0)
    b = _rec('google', "McDonald's Dos", 'Y', lat=41.0, lng=2.0)
    c = _rec('apple', "McDonald's", 'Z', lat=41.0, lng=2.0)
    clusters = matching.cluster_records([a, b, c])
    assert len(clusters) == 2
    assert all(c['ambiguous'] is False for c in clusters)


def test_ambiguous_when_two_same_source_fichas_for_one_google():
    # Dos fichas de Apple para una misma sede de Google → 1 cluster, ambiguo,
    # y by_source['apple'] es la MÁS CERCANA a Google (no la primera).
    g = _rec('google', 'McDonalds Centro', 'X', lat=41.0, lng=2.0)
    near = _rec('apple', 'McDonalds Centro', 'X', lat=41.0, lng=2.0)          # 0 m
    far = _rec('apple', 'McDonalds Centre', 'Y', lat=41.0, lng=2.0018)        # ~150 m
    clusters = matching.cluster_records([g, far, near])                       # 'far' primero a propósito
    assert len(clusters) == 1
    assert clusters[0]['ambiguous'] is True
    assert clusters[0]['by_source']['apple']['name'] == 'McDonalds Centro'    # la más cercana gana


def test_bridging_record_goes_to_nearest_google():
    # Un Apple que casa con DOS sedes Google se asigna a la más cercana; las
    # dos sedes Google quedan separadas (no fusionadas por el puente).
    g1 = _rec('google', 'McDonalds Uno', 'X', lat=41.0, lng=2.0)
    g2 = _rec('google', 'McDonalds Dos', 'Y', lat=41.0, lng=2.003)           # ~252 m de g1
    a = _rec('apple', 'McDonalds', 'Z', lat=41.0, lng=2.001)                 # ~84 m de g1, ~168 de g2
    clusters = matching.cluster_records([g1, g2, a])
    assert len(clusters) == 2
    by_label = {c['canonical_label']: c for c in clusters}
    assert 'apple' in by_label['McDonalds Uno']['sources_present']
    assert 'apple' not in by_label['McDonalds Dos']['sources_present']


def test_records_without_google_still_cluster():
    # Sedes sin ficha en Google: se agrupan entre sí (camino Union-Find).
    a = _rec('apple', 'Tienda X', 'Calle Y', lat=41.0, lng=2.0)
    b = _rec('azure', 'Tienda X', 'Calle Y', lat=41.0, lng=2.0)
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 1
    assert clusters[0]['sources_present'] == ['apple', 'azure']
    assert clusters[0]['ambiguous'] is False


def test_canonical_label_prefers_google_over_apple():
    a = _rec('apple', 'Apple Name', 'X', lat=1.0, lng=1.0)
    b = _rec('google', 'Google Name', 'X', lat=1.0, lng=1.0)
    clusters = matching.cluster_records([a, b])
    assert clusters[0]['canonical_label'] == 'Google Name'


def test_canonical_label_prefers_google_over_official():
    a = _rec('official', 'Official Name', 'X', lat=1.0, lng=1.0)
    b = _rec('google', 'Google Name', 'X', lat=1.0, lng=1.0)
    clusters = matching.cluster_records([a, b])
    assert clusters[0]['canonical_label'] == 'Google Name'


def test_clusters_get_stable_sequential_ids():
    a = _rec('google', 'A', 'X', lat=1.0, lng=1.0)
    b = _rec('google', 'B', 'Y', lat=50.0, lng=50.0)
    clusters = matching.cluster_records([a, b])
    ids = sorted(c['cluster_id'] for c in clusters)
    assert ids == ['L1', 'L2']


def test_canonical_address_picked_by_source_priority():
    a = _rec('official', 'Foo', 'Official Address', lat=1.0, lng=1.0)
    b = _rec('google', 'Foo', 'Google Address', lat=1.0, lng=1.0)
    clusters = matching.cluster_records([a, b])
    assert clusters[0]['canonical_address'] == 'Google Address'


def test_cluster_gets_canonical_coordinates_picked_by_source_priority():
    a = _rec('apple', 'Foo', 'X', lat=41.1100, lng=2.1100)
    b = _rec('google', 'Foo', 'X', lat=41.1101, lng=2.1101)  # ~14m away, same cluster
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 1
    assert clusters[0]['lat'] == 41.1101
    assert clusters[0]['lng'] == 2.1101


def test_cluster_records_matches_same_business_geocoded_to_different_streets():
    # Real case (Movistar, Barcelona): Google and Apple geocode the same
    # store to two different, nearby streets, 187m apart — a plausible
    # per-provider geocoding discrepancy, not two different stores. The
    # closest two genuinely distinct real branches of this same chain sat
    # 466.8m apart, so 200m safely covers this case without risking merging
    # real, distinct locations.
    a = _rec('google', 'Tienda Movistar', "Carrer de Potosí, 2, Barcelona", lat=41.4425713, lng=2.200075)
    b = _rec('apple', 'Movistar', 'Passeig de Potosí, 2, Barcelona', lat=41.4420647, lng=2.1979351)
    clusters = matching.cluster_records([a, b])
    assert len(clusters) == 1
    assert clusters[0]['sources_present'] == ['apple', 'google']
