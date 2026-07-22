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


def test_cluster_records_ambiguous_when_same_source_merges():
    a = _rec('google', "McDonald's A", 'X', lat=41.0, lng=2.0)
    b = _rec('google', "McDonald's B", 'Y', lat=41.0, lng=2.0)
    c = _rec('apple', "McDonald's", 'Z', lat=41.0, lng=2.0)
    clusters = matching.cluster_records([a, b, c])
    assert len(clusters) == 1
    assert clusters[0]['ambiguous'] is True


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
