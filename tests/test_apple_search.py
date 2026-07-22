import app as app_module


def test_google_coords_extracts_lat_lng_skipping_incomplete_records():
    google_results = [
        {'geometry': {'location': {'lat': 41.38, 'lng': 2.17}}},
        {'geometry': {}},  # no location at all
        {},                # no geometry at all
        {'geometry': {'location': {'lat': 41.40, 'lng': 2.20}}},
    ]
    assert app_module._google_coords(google_results) == [(41.38, 2.17), (41.40, 2.20)]


class _FakeResponse:
    def __init__(self, results, page_token=None):
        self._results = results
        self._page_token = page_token
        self.ok = True

    def json(self):
        return {'results': self._results, 'pageToken': self._page_token}


def _apple_item(item_id, name, lat, lng):
    return {'id': item_id, 'name': name, 'coordinate': {'latitude': lat, 'longitude': lng},
            'formattedAddressLines': ['X'], 'phoneNumber': None, 'url': None, 'pointOfInterestCategory': None}


def test_search_apple_merges_city_wide_and_per_anchor_results(monkeypatch):
    monkeypatch.setattr(app_module, '_apple_access_token', lambda: 'fake-token')
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))

    # City-wide query (anchored at the city center) only finds one branch —
    # exactly the proximity-bias behavior confirmed against the real API.
    city_wide = [_apple_item('A', 'Foo Centro', 41.38, 2.17)]
    # Per-location anchored search (anchored at a specific branch's own
    # coordinates) finds a DIFFERENT branch the city-wide query missed.
    per_anchor = [_apple_item('B', 'Foo Poblenou', 41.40, 2.20)]

    def fake_get(url, params=None, headers=None, timeout=None):
        if params.get('searchLocation') == '41.38,2.17':
            return _FakeResponse(city_wide)
        return _FakeResponse(per_anchor)

    monkeypatch.setattr(app_module.requests, 'get', fake_get)

    results = app_module._search_apple('Foo', 'Barcelona', extra_anchors=[(41.40, 2.20)])
    ids = sorted(r['id'] for r in results)
    assert ids == ['A', 'B']


def test_search_apple_dedupes_by_id_across_anchors(monkeypatch):
    monkeypatch.setattr(app_module, '_apple_access_token', lambda: 'fake-token')
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))

    same_item = [_apple_item('A', 'Foo', 41.38, 2.17)]
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResponse(same_item))

    # Two anchors both happen to surface the same POI — must not duplicate it.
    results = app_module._search_apple('Foo', 'Barcelona', extra_anchors=[(41.38, 2.17), (41.39, 2.18)])
    assert len(results) == 1
    assert results[0]['id'] == 'A'


def test_search_apple_without_extra_anchors_is_just_the_city_wide_search(monkeypatch):
    monkeypatch.setattr(app_module, '_apple_access_token', lambda: 'fake-token')
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    monkeypatch.setattr(app_module.requests, 'get',
                         lambda *a, **k: _FakeResponse([_apple_item('A', 'Foo', 41.38, 2.17)]))

    results = app_module._search_apple('Foo', 'Barcelona')
    assert len(results) == 1


# ── _google_places (name+coord per Google location) ─────────────────────

def test_google_places_extracts_name_and_coords():
    google_results = [
        {'name': 'Foo Clot', 'geometry': {'location': {'lat': 41.41, 'lng': 2.18}}},
        {'name': 'No coords', 'geometry': {}},           # skipped: no location
        {'geometry': {'location': {'lat': 41.4, 'lng': 2.2}}},  # skipped: no name
    ]
    assert app_module._google_places(google_results) == [
        {'name': 'Foo Clot', 'lat': 41.41, 'lng': 2.18}]


# ── autocomplete-by-name second pass (recall for multi-brand stores) ─────

def test_search_apple_autocomplete_by_name_adds_missed_store(monkeypatch):
    monkeypatch.setattr(app_module, '_apple_access_token', lambda: 'fake-token')
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))

    # /v1/search only ever returns the generic brand POI, missing the
    # multi-brand store — the exact recall gap seen live.
    generic = [_apple_item('GEN', 'Lowi', 41.30, 2.10)]
    clot_poi = _apple_item('CLOT', 'Lowi/Vodafone Clot', 41.4188, 2.1819)

    def fake_get(url, params=None, headers=None, timeout=None):
        if 'searchAutocomplete' in url:
            return _FakeResponse([{  # a resolvable suggestion for the exact name
                'displayLines': ['Lowi/Vodafone Clot', 'Carrer X, Barcelona'],
                'completionUrl': '/v1/search?q=RESOLVE_CLOT',
            }])
        if params is None and 'RESOLVE_CLOT' in url:  # the resolved completionUrl
            return _FakeResponse([clot_poi])
        return _FakeResponse(generic)  # plain /v1/search

    monkeypatch.setattr(app_module.requests, 'get', fake_get)

    gp = [{'name': 'Lowi/Vodafone Clot', 'lat': 41.4188, 'lng': 2.1819}]
    without = app_module._search_apple('Lowi', 'Barcelona', extra_anchors=[(41.4188, 2.1819)])
    with_names = app_module._search_apple('Lowi', 'Barcelona', extra_anchors=[(41.4188, 2.1819)],
                                          google_places=gp)

    assert 'CLOT' not in {r['id'] for r in without}          # missed without the by-name pass
    assert 'CLOT' in {r['id'] for r in with_names}           # found with it


def test_search_apple_autocomplete_skips_unrelated_suggestions(monkeypatch):
    monkeypatch.setattr(app_module, '_apple_access_token', lambda: 'fake-token')
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    resolved = []

    def fake_get(url, params=None, headers=None, timeout=None):
        if 'searchAutocomplete' in url:
            return _FakeResponse([{'displayLines': ['Totally Unrelated Cafe'],
                                   'completionUrl': '/v1/search?q=NOPE'}])
        if params is None and 'NOPE' in url:
            resolved.append(url)  # must never be resolved
            return _FakeResponse([_apple_item('X', 'X', 0, 0)])
        return _FakeResponse([])

    monkeypatch.setattr(app_module.requests, 'get', fake_get)
    gp = [{'name': 'Lowi/Vodafone Clot', 'lat': 41.4188, 'lng': 2.1819}]
    app_module._search_apple('Lowi', 'Barcelona', google_places=gp)
    assert resolved == []  # weak name match → suggestion not resolved
