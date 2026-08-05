import json
from io import BytesIO

import app as app_module
import matching
import normalize


def _google_cluster(cid_index=1, lat=40.4, lng=-3.7, name='Zara Home'):
    g = normalize.make_record('google', 'p1', name=name, formatted_address='Calle X 1, Madrid',
                              lat=lat, lng=lng, phone='+34 900 000 000', website='zarahome.com',
                              opening_hours=['Lunes: 10:00–20:00'])
    cluster = matching._build_cluster(cid_index, [g])
    cluster['reputation'] = {'rating': None, 'review_count': None, 'negative_samples': [], 'score': None}
    cluster['flags'] = []  # detect_inconsistencies lo rellena en el pipeline; el endpoint lo recomputa
    return cluster


def _audit(clusters):
    return {'clusters': clusters, 'summary': app_module._audit_summary(clusters)}


def _post_csv(client, audit, csv_bytes, city='Madrid'):
    return client.post('/audit/add_csv', content_type='multipart/form-data', data={
        'audit': json.dumps(audit), 'city': city,
        'official_csv': (BytesIO(csv_bytes), 'sedes.csv')})


def _fake_geocode(coords):
    def _fill(records):
        for r in records:
            r['lat'], r['lng'] = coords
        return records
    return _fill


def test_add_csv_matches_and_attaches_official(monkeypatch):
    monkeypatch.setattr(app_module, '_fill_missing_coords', _fake_geocode((40.4, -3.7)))
    cluster = _google_cluster()
    audit = _audit([cluster])
    assert 'official' not in cluster['sources_present']
    c = app_module.app.test_client()
    r = _post_csv(c, audit, b'name,address\nZara Home,Calle X 1 Madrid\n')
    assert r.status_code == 200
    body = r.get_json()
    assert body['matched'] == 1 and body['unmatched'] == 0
    out = body['audit']['clusters'][0]
    assert out['cluster_id'] == 'L1'                       # id preservado
    assert 'official' in out['sources_present']
    assert out['by_source']['official']['name'] == 'Zara Home'


def test_add_csv_unmatched_row_reported(monkeypatch):
    monkeypatch.setattr(app_module, '_fill_missing_coords', _fake_geocode((41.9, 2.8)))  # lejos
    audit = _audit([_google_cluster()])
    c = app_module.app.test_client()
    body = _post_csv(c, audit, b'name,address\nOtra Marca,Sitio lejano\n').get_json()
    assert body['matched'] == 0 and body['unmatched'] == 1
    assert 'official' not in body['audit']['clusters'][0]['sources_present']


def test_add_csv_preserves_per_venue_llm(monkeypatch):
    monkeypatch.setattr(app_module, '_fill_missing_coords', _fake_geocode((40.4, -3.7)))
    cluster = _google_cluster()
    cluster['venue_metrics'] = {'llm_visibility': {'prompt': 'p', 'runs': [{'appears': True}], 'hits': 1}}
    audit = _audit([cluster])
    c = app_module.app.test_client()
    body = _post_csv(c, audit, b'name,address\nZara Home,Calle X 1 Madrid\n').get_json()
    out = body['audit']['clusters'][0]
    assert out['venue_metrics']['llm_visibility']['hits'] == 1        # IA no se pierde al recomputar
    assert body['audit']['summary']['llm_visibility']['hits_total'] == 1


def test_add_csv_rejects_missing_audit():
    c = app_module.app.test_client()
    r = c.post('/audit/add_csv', content_type='multipart/form-data',
               data={'official_csv': (BytesIO(b'name,address\nX,Y\n'), 'a.csv')})
    assert r.status_code == 400


def test_add_csv_rejects_missing_csv():
    c = app_module.app.test_client()
    r = c.post('/audit/add_csv', content_type='multipart/form-data',
               data={'audit': json.dumps(_audit([_google_cluster()]))})
    assert r.status_code == 400


def test_add_csv_empty_csv_is_400(monkeypatch):
    monkeypatch.setattr(app_module, '_fill_missing_coords', lambda records: records)
    audit = _audit([_google_cluster()])
    c = app_module.app.test_client()
    r = _post_csv(c, audit, b'name,address\n')  # solo cabecera
    assert r.status_code == 400
