import app as app_module
import normalize
import pricing


class _FakeResp:
    def __init__(self, payload, ok=True, status=200):
        self._payload, self.ok, self.status_code, self.text = payload, ok, status, ''

    def json(self):
        return self._payload


def _enable(monkeypatch):
    monkeypatch.setattr(app_module, '_BING_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_SERPAPI_KEY', 'fake')


def _azure_rec(name='Zara Home'):
    return normalize.from_azure({'id': 'z1', 'name': name, 'formatted_address': 'X, Madrid',
                                 'lat': 40.4237, 'lng': -3.7108})


# ── horas de Bing → lista normalizada ────────────────────────────────────
def test_bing_hours_parses_ampm_and_specials():
    h = {'Monday': ['10 AM - 10 PM'], 'Tuesday': ['10:30 AM - 9 PM'],
         'Wednesday': ['Closed'], 'Thursday': ['Open 24 hours']}
    out = app_module._bing_hours_to_list(h)
    assert 'Lunes: 10:00–22:00' in out
    assert 'Martes: 10:30–21:00' in out
    assert 'Jueves: 00:00–24:00' in out
    assert all('Miércoles' not in x for x in out)          # 'Closed' se omite
    assert normalize.parse_hours(out)[0] == [(10 * 60, 22 * 60)]  # legible por el comparador


def test_bing_hours_empty():
    assert app_module._bing_hours_to_list(None) is None
    assert app_module._bing_hours_to_list({}) is None


# ── lookup (HTTP mockeado) ───────────────────────────────────────────────
_ITEMS = {'local_results': [{'items': [
    {'title': 'ZARA HOME', 'place_id': 'YN_FAR', 'phone': '900',
     'gps_coordinates': {'latitude': 41.0, 'longitude': 2.0}},              # lejos
    {'title': 'ZARA HOME', 'place_id': 'YN_NEAR', 'phone': '914 85', 'website': 'zarahome.com/es',
     'gps_coordinates': {'latitude': 40.4238, 'longitude': -3.7109}},       # cerca
    {'title': 'Otra Tienda', 'place_id': 'YN_X',
     'gps_coordinates': {'latitude': 40.4237, 'longitude': -3.7108}},       # cerca pero otro nombre
]}]}


def test_bing_lookup_picks_closest_name_match(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResp(_ITEMS))
    m = app_module._serpapi_bing_lookup('Zara Home', 'Madrid', 40.4237, -3.7108)
    assert m['place_id'] == 'YN_NEAR'


def test_bing_lookup_rejects_far_only_match(monkeypatch):
    _enable(monkeypatch)
    payload = {'local_results': [{'items': [
        {'title': 'ZARA HOME', 'place_id': 'YN_FAR',
         'gps_coordinates': {'latitude': 41.5, 'longitude': 2.2}}]}]}
    monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: _FakeResp(payload))
    assert app_module._serpapi_bing_lookup('Zara Home', 'Barcelona', 40.4237, -3.7108) is None


def test_bing_lookup_disabled(monkeypatch):
    monkeypatch.setattr(app_module, '_BING_SERPAPI_ENABLED', False)
    monkeypatch.setattr(app_module.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('no debería llamar')))
    assert app_module._serpapi_bing_lookup('X', 'Madrid', 40.0, -3.0) is None


# ── enriquecimiento del record ───────────────────────────────────────────
def test_enrich_bing_sets_ficha_link_and_anchors(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_bing_lookup', lambda name, city, lat, lng: {
        'place_id': 'YN79138', 'phone': '+34 914 85 90 55',
        'website': 'https://www.zarahome.com/es', 'type': 'Home goods store'})
    rec = _azure_rec()
    assert 'bing.com/maps/search' in rec['verify_url']          # partía del listado por punto
    app_module._enrich_bing_record(rec, 'Madrid')
    assert rec['verify_url'] == 'https://www.bing.com/maps?ss=ypid.YN79138'
    assert rec['raw']['bing_ypid'] == 'YN79138'
    assert rec['phone_display'] == '+34 914 85 90 55'
    assert 'zarahome' in rec['website_display']


def test_enrich_bing_noop_without_match(monkeypatch):
    monkeypatch.setattr(app_module, '_serpapi_bing_lookup', lambda *a, **k: None)
    rec = _azure_rec()
    before = rec['verify_url']
    app_module._enrich_bing_record(rec, 'Madrid')
    assert rec['verify_url'] == before
    assert 'bing_ypid' not in (rec.get('raw') or {})


# ── nivel cluster ────────────────────────────────────────────────────────
def test_enrich_bing_clusters_only_google_matched(monkeypatch):
    monkeypatch.setattr(app_module, '_BING_SERPAPI_ENABLED', True)
    called = []
    monkeypatch.setattr(app_module, '_serpapi_bing_lookup',
                        lambda name, city, lat, lng: called.append(name) or {'place_id': 'YN' + name})
    with_g = {'sources_present': ['google', 'azure'], 'by_source': {'azure': _azure_rec('Con Google')}}
    azure_only = {'sources_present': ['azure'], 'by_source': {'azure': _azure_rec('Sin Google')}}
    app_module._enrich_bing_clusters([with_g, azure_only], 'Madrid')
    assert called == ['Con Google']
    assert with_g['by_source']['azure']['raw']['bing_ypid'] == 'YNCon Google'


def test_attach_bing_hours_worst_capped(monkeypatch):
    monkeypatch.setattr(app_module, '_BING_SERPAPI_ENABLED', True)
    monkeypatch.setattr(app_module, '_BING_HOURS_MAX_VENUES', 1)
    monkeypatch.setattr(app_module, '_serpapi_bing_details', lambda ypid: {'Monday': ['10 AM - 10 PM']})

    def _c(cid):
        rec = _azure_rec()
        rec['raw']['bing_ypid'] = 'YN' + cid
        return {'cluster_id': cid, 'sources_present': ['google', 'azure'], 'by_source': {'azure': rec}}

    clusters = [_c('A'), _c('B')]
    n = app_module._attach_bing_hours_worst(clusters, 'Madrid')
    assert n == 1                                              # cap = 1 → solo la 1ª
    assert clusters[0]['by_source']['azure']['opening_hours'][0].startswith('Lunes')
    assert clusters[1]['by_source']['azure'].get('opening_hours') is None


# ── url helper + pricing ─────────────────────────────────────────────────
def test_bing_place_url():
    assert normalize.bing_place_url('YNABC') == 'https://www.bing.com/maps?ss=ypid.YNABC'
    assert normalize.bing_place_url(None) is None


def test_pricing_includes_bing_calls():
    est = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3,
                               action_links_venues=5, bing_hours_venues=5)
    assert est['assumptions']['bing_hours_venues'] == 5
