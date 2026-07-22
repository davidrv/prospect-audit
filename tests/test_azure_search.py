import app as app_module


class _FakeResponse:
    def __init__(self, items):
        self._items = items
        self.ok = True

    def json(self):
        return {'results': self._items}


def _azure_item(item_id, name, lat, lng):
    return {
        'type': 'POI',
        'id': item_id,
        'poi': {'name': name, 'phone': None, 'url': None, 'categories': []},
        'address': {'freeformAddress': 'X'},
        'position': {'lat': lat, 'lon': lng},
    }


def test_search_azure_merges_city_wide_and_per_anchor_results(monkeypatch):
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    monkeypatch.setenv('AZURE_MAPS_SUBSCRIPTION_KEY', 'fake-key')

    # City-wide query (anchored at the city center, 50km radius) only finds
    # one branch — the same relevance-ranking gap already confirmed for
    # Apple's city-wide-only search.
    city_wide = [_azure_item('A', 'Foo Centro', 41.38, 2.17)]
    # Per-location anchored search (tight radius around a specific branch's
    # own coordinates) finds a DIFFERENT branch the city-wide query missed.
    per_anchor = [_azure_item('B', 'Foo Poblenou', 41.40, 2.20)]

    def fake_get(url, params=None, timeout=None):
        if params.get('radius') == 50_000:
            return _FakeResponse(city_wide)
        return _FakeResponse(per_anchor)

    monkeypatch.setattr(app_module.requests, 'get', fake_get)

    results = app_module._search_azure('Foo', 'Barcelona', extra_anchors=[(41.40, 2.20)])
    ids = sorted(r['id'] for r in results)
    assert ids == ['A', 'B']


def test_search_azure_dedupes_by_id_across_anchors(monkeypatch):
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    monkeypatch.setenv('AZURE_MAPS_SUBSCRIPTION_KEY', 'fake-key')

    same_item = [_azure_item('A', 'Foo', 41.38, 2.17)]
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResponse(same_item))

    # Two anchors both happen to surface the same POI — must not duplicate it.
    results = app_module._search_azure('Foo', 'Barcelona', extra_anchors=[(41.38, 2.17), (41.39, 2.18)])
    assert len(results) == 1
    assert results[0]['id'] == 'A'


def test_search_azure_without_extra_anchors_is_just_the_city_wide_search(monkeypatch):
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    monkeypatch.setenv('AZURE_MAPS_SUBSCRIPTION_KEY', 'fake-key')
    monkeypatch.setattr(app_module.requests, 'get',
                         lambda *a, **k: _FakeResponse([_azure_item('A', 'Foo', 41.38, 2.17)]))

    results = app_module._search_azure('Foo', 'Barcelona')
    assert len(results) == 1


def test_search_azure_filters_out_unrelated_fuzzy_noise(monkeypatch):
    # Bing's fuzzy search floods results with near-homograph/unrelated POIs
    # ("Low Cost", "Loli", "Liwi", Bird scooters...). Only real name matches
    # for the prospect should survive.
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: (41.38, 2.17))
    monkeypatch.setenv('AZURE_MAPS_SUBSCRIPTION_KEY', 'fake-key')

    items = [
        _azure_item('REAL1', 'Lowi', 41.38, 2.17),
        _azure_item('REAL2', 'Versus Mobile | DIGI • O2 • SIMYO • LOWI • PARLEM TELECOM', 41.39, 2.18),
        _azure_item('NOISE1', 'Low Cost Cartridge', 41.38, 2.17),
        _azure_item('NOISE2', 'Loli Garden', 41.38, 2.17),
        _azure_item('NOISE3', 'Liwi', 41.38, 2.17),
        _azure_item('NOISE4', 'Bird Barcelona C Venus', 41.38, 2.17),
        _azure_item('NOISE5', 'Donkey Republic Barcelona', 41.38, 2.17),
    ]
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResponse(items))

    results = app_module._search_azure('Lowi', 'Barcelona')
    ids = sorted(r['id'] for r in results)
    assert ids == ['REAL1', 'REAL2']  # only genuine name matches kept
