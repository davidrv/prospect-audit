import app as app_module
import history
import llm_visibility
import google_signals


def _fresh_history(tmp_path, monkeypatch):
    monkeypatch.delenv('DISABLE_AUDIT_HISTORY', raising=False)
    monkeypatch.setenv('AUDIT_HISTORY_PATH', str(tmp_path / 'h.sqlite'))
    monkeypatch.setattr(history, '_conn', None)


def _seed():
    snap = {'audit': {'summary': {}, 'clusters': [
        {'cluster_id': 'L1',
         'venue_metrics': {'presence_detail': {'google': {'present': True}}},
         'by_source': {'google': {'source_id': 'pid1', 'raw': {'place_id': 'pid1'}}}}]}}
    history.save('a1', 'Movistar', 'Barcelona', 50, 1, snap)


# ── LLM on-demand ─────────────────────────────────────────────────────────
def test_venue_llm_computes_persists_and_recomputes_summary(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_LLM_VISIBILITY_ENABLED', True)
    per = {'prompt': 'p', 'runs': [{'appears': True, 'position': 1, 'label': 'x'}], 'hits': 1}
    monkeypatch.setattr(llm_visibility, 'fetch_llm_visibility',
                        lambda clusters, name, city, **kw: {'per_venue': {'L1': per}})
    c = app_module.app.test_client()
    r = c.post('/venue/a1/L1/llm', json={'category': 'tienda de móviles'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['llm_visibility']['hits'] == 1
    assert body['summary_llm']['venues_checked'] == 1
    assert body['summary_llm']['hits_total'] == 1
    # persisted in the snapshot
    rec = history.get('a1')
    assert rec['snapshot']['audit']['clusters'][0]['venue_metrics']['llm_visibility']['hits'] == 1
    assert rec['snapshot']['audit']['summary']['llm_visibility']['checks_total'] == 1


def test_venue_llm_400_when_disabled(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_LLM_VISIBILITY_ENABLED', False)
    c = app_module.app.test_client()
    assert c.post('/venue/a1/L1/llm', json={}).status_code == 400


def test_venue_llm_404_when_missing(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, '_LLM_VISIBILITY_ENABLED', True)
    c = app_module.app.test_client()
    assert c.post('/venue/nope/L1/llm', json={}).status_code == 404


def test_venue_llm_502_when_no_result(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_LLM_VISIBILITY_ENABLED', True)
    monkeypatch.setattr(llm_visibility, 'fetch_llm_visibility',
                        lambda *a, **k: {'per_venue': {}})
    c = app_module.app.test_client()
    assert c.post('/venue/a1/L1/llm', json={}).status_code == 502


# ── Action links on-demand ────────────────────────────────────────────────
def test_venue_action_links_computes_and_persists(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', True)
    monkeypatch.setattr(app_module, '_REVIEW_SCRAPING_ENABLED', True)
    monkeypatch.setattr(google_signals, 'fetch_action_links',
                        lambda pid, session=None: [{'type': 'reservations', 'link': 'http://x'}])
    c = app_module.app.test_client()
    r = c.post('/venue/a1/L1/action-links', json={})
    assert r.status_code == 200
    al = r.get_json()['action_links_google']
    assert al['source'] == 'scraped' and al['links']
    rec = history.get('a1')
    assert rec['snapshot']['audit']['clusters'][0]['venue_metrics']['action_links_google']['links']


def test_venue_action_links_empty_is_none_detected(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', True)
    monkeypatch.setattr(app_module, '_REVIEW_SCRAPING_ENABLED', True)
    monkeypatch.setattr(google_signals, 'fetch_action_links', lambda pid, session=None: [])
    c = app_module.app.test_client()
    al = c.post('/venue/a1/L1/action-links', json={}).get_json()['action_links_google']
    assert al['value'] == 'Ninguno detectado'


def test_venue_action_links_400_when_disabled(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', False)
    c = app_module.app.test_client()
    assert c.post('/venue/a1/L1/action-links', json={}).status_code == 400
