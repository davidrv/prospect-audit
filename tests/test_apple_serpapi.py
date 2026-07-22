import app as app_module
import normalize


# ── weekly_hours → normalized list ──────────────────────────────────────

def test_weekly_hours_dict_to_normalized_list():
    wh = {'Monday': '09:00 – 20:00', 'Tuesday': '09:00 – 20:00', 'Sunday': 'Closed'}
    out = app_module._serpapi_weekly_hours_to_list(wh)
    # Ordered by weekday, Spanish day labels, comparator-readable.
    assert out[0] == 'Lunes: 09:00–20:00'
    assert out[-1] == 'Domingo: Closed'
    # Round-trips through the same parser the accuracy comparator uses.
    schedule = normalize.parse_hours(out)
    assert schedule[0] == [(9 * 60, 20 * 60)]
    assert schedule[6] == 'closed'


def test_weekly_hours_list_of_objects():
    wh = [{'day': 'Friday', 'hours': '10:00 – 14:00'}]
    out = app_module._serpapi_weekly_hours_to_list(wh)
    assert out == ['Viernes: 10:00–14:00']


def test_weekly_hours_empty_returns_none():
    assert app_module._serpapi_weekly_hours_to_list(None) is None
    assert app_module._serpapi_weekly_hours_to_list({}) is None


# ── SerpApi lookup (mocked HTTP) ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, ok=True, status=200):
        self._payload, self.ok, self.status_code, self.text = payload, ok, status, ''
    def json(self):
        return self._payload


def test_serpapi_apple_lookup_picks_best_name_match(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_SERPAPI_KEY', 'fake')
    payload = {'local_results': [
        {'title': 'Otra Cosa', 'phone': '+34 900 000 000'},
        {'title': 'Alain Afflelou Óptico', 'phone': '+34 963 94 02 59', 'website': 'afflelou.es'},
    ]}
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResp(payload))
    match = app_module._serpapi_apple_lookup('Alain Afflelou', 39.47, -0.37)
    assert match['title'] == 'Alain Afflelou Óptico'


def test_serpapi_apple_lookup_rejects_weak_match(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_SERPAPI_KEY', 'fake')
    payload = {'local_results': [{'title': 'Completely Unrelated Shop'}]}
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResp(payload))
    assert app_module._serpapi_apple_lookup('Alain Afflelou', 39.47, -0.37) is None


def test_serpapi_apple_lookup_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', False)
    # Should not even hit the network.
    monkeypatch.setattr(app_module.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not call')))
    assert app_module._serpapi_apple_lookup('X', 39.47, -0.37) is None


# ── enrichment merge ─────────────────────────────────────────────────────

def test_enrich_apple_fills_missing_fields(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'phone': '+34 963 94 02 59', 'website': 'afflelou.es',
        'weekly_hours': {'Monday': '09:00 – 20:00'}, 'rating': 4.3, 'reviews': 120,
        'type': 'Optician',
    })
    apple = [{'id': 'a1', 'name': 'Alain Afflelou', 'lat': 39.47, 'lng': -0.37,
              'phone_number': None, 'url': None, 'category': None}]
    app_module._enrich_apple_with_serpapi(apple)
    item = apple[0]
    assert item['phone_number'] == '+34 963 94 02 59'
    assert item['url'] == 'afflelou.es'
    assert item['opening_hours'] == ['Lunes: 09:00–20:00']
    assert item['rating'] == 4.3
    assert item['review_count'] == 120
    assert item['serpapi_enriched'] is True


def test_enrich_apple_keeps_existing_values(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'phone': '+34 000', 'website': 'serpapi-site.es',
    })
    apple = [{'id': 'a1', 'name': 'X', 'lat': 1.0, 'lng': 2.0,
              'phone_number': '+34 999 EXISTING', 'url': None, 'category': None}]
    app_module._enrich_apple_with_serpapi(apple)
    assert apple[0]['phone_number'] == '+34 999 EXISTING'  # existing Server-API value wins
    assert apple[0]['url'] == 'serpapi-site.es'             # missing one gets filled


def test_enrich_apple_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', False)
    apple = [{'id': 'a1', 'name': 'X', 'lat': 1.0, 'lng': 2.0}]
    app_module._enrich_apple_with_serpapi(apple)
    assert 'serpapi_enriched' not in apple[0]


# ── from_apple carries the enriched fields ───────────────────────────────

def test_from_apple_carries_enriched_fields():
    rec = normalize.from_apple({
        'id': 'a1', 'name': 'Foo', 'formatted_address': 'X', 'lat': 1.0, 'lng': 2.0,
        'phone_number': '+34 963', 'url': 'foo.es', 'category': 'Optician',
        'opening_hours': ['Lunes: 09:00–20:00'], 'rating': 4.3, 'review_count': 120,
    })
    assert rec['opening_hours'] == ['Lunes: 09:00–20:00']
    assert rec['rating'] == 4.3
    assert rec['review_count'] == 120
    assert rec['phone'] == '34963'


def test_from_apple_defaults_without_enrichment():
    rec = normalize.from_apple({'id': 'a1', 'name': 'Foo', 'formatted_address': 'X'})
    assert rec['opening_hours'] is None
    assert rec['rating'] is None
    assert rec['review_count'] is None
