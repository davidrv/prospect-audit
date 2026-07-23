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

def _apple_rec(name='Alain Afflelou', phone_display=None, website_display=None):
    return normalize.from_apple({'id': 'a1', 'name': name, 'formatted_address': 'X',
                                 'lat': 39.47, 'lng': -0.37,
                                 'phone_number': phone_display, 'url': website_display})


def test_enrich_apple_record_fills_missing_fields(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'phone': '+34 963 94 02 59', 'website': 'afflelou.es',
        'weekly_hours': {'Monday': '09:00 – 20:00'}, 'rating': 4.3, 'reviews': 120,
        'type': 'Optician',
    })
    rec = _apple_rec()
    app_module._enrich_apple_record(rec)
    assert rec['phone_display'] == '+34 963 94 02 59'
    assert rec['website_display'] == 'afflelou.es'
    assert rec['opening_hours'] == ['Lunes: 09:00–20:00']
    assert rec['rating'] == 4.3
    assert rec['review_count'] == 120


def test_enrich_apple_record_keeps_existing_values(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'phone': '+34 000', 'website': 'serpapi-site.es',
    })
    rec = _apple_rec(phone_display='+34 999 EXISTING')
    app_module._enrich_apple_record(rec)
    assert rec['phone_display'] == '+34 999 EXISTING'   # el valor propio del Server API gana
    assert rec['website_display'] == 'serpapi-site.es'  # el que faltaba se rellena


def test_enrich_apple_sets_real_ficha_link_from_serpapi(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'place_id': 'I623A2B788E0D1E10', 'provider_id': '9902',
        'link': 'https://maps.apple.com/place?place-id=I623A2B788E0D1E10&_provider=9902',
    })
    rec = _apple_rec()
    assert 'maps.apple.com/?ll=' in rec['verify_url']  # partía del deep-link por coordenadas
    app_module._enrich_apple_record(rec)
    assert rec['verify_url'] == 'https://maps.apple.com/place?place-id=I623A2B788E0D1E10&_provider=9902'
    assert rec['raw']['apple_place_id'] == 'I623A2B788E0D1E10'
    assert rec['raw']['apple_provider_id'] == '9902'


def test_enrich_apple_builds_ficha_link_when_serpapi_link_absent(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda name, lat, lng: {
        'title': name, 'place_id': 'IABC', 'provider_id': '9902'})  # sin 'link'
    rec = _apple_rec()
    app_module._enrich_apple_record(rec)
    assert rec['verify_url'] == 'https://maps.apple.com/place?place-id=IABC&_provider=9902'


def test_enrich_apple_keeps_coord_link_when_no_place_id(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup',
                        lambda name, lat, lng: {'title': name, 'phone': '+34 1'})  # sin place_id/link
    rec = _apple_rec()
    before = rec['verify_url']
    app_module._enrich_apple_record(rec)
    assert rec['verify_url'] == before  # se conserva el fallback por coordenadas
    assert 'apple_place_id' not in rec['raw']


def test_enrich_apple_clusters_only_enriches_google_matched(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', True)
    called = []
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup',
                        lambda name, lat, lng: called.append(name) or {'title': name, 'phone': '+34 111'})
    with_google = {'sources_present': ['google', 'apple'],
                   'by_source': {'apple': _apple_rec(name='Con Google')}}
    apple_only = {'sources_present': ['apple'],
                  'by_source': {'apple': _apple_rec(name='Sin Google')}}
    app_module._enrich_apple_clusters([with_google, apple_only])
    assert called == ['Con Google']                       # solo el que machea con Google
    assert with_google['by_source']['apple']['phone_display'] == '+34 111'
    assert apple_only['by_source']['apple']['phone_display'] is None


def test_enrich_apple_clusters_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(app_module, '_APPLE_SERPAPI_ENABLED', False)
    called = []
    monkeypatch.setattr(app_module, '_serpapi_apple_lookup', lambda *a: called.append(1) or None)
    cluster = {'sources_present': ['google', 'apple'], 'by_source': {'apple': _apple_rec()}}
    app_module._enrich_apple_clusters([cluster])
    assert called == []


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
