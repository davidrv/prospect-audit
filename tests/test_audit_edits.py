import app as app_module
import history


def _fresh_history(tmp_path, monkeypatch):
    monkeypatch.delenv('DISABLE_AUDIT_HISTORY', raising=False)
    monkeypatch.setenv('AUDIT_HISTORY_PATH', str(tmp_path / 'h.sqlite'))
    monkeypatch.setattr(history, '_conn', None)


def _seed(comment=None):
    snap = {'official_comment': '', 'audit': {'clusters': [
        {'cluster_id': 'L1', 'presenter_comment': comment,
         'by_source': {'google': {'source_id': 'pid1', 'raw': {'place_id': 'pid1'}}}}]}}
    history.save('a1', 'Movistar', 'Barcelona', 50, 1, snap)


def test_edits_saves_venue_and_official_comments(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    c = app_module.app.test_client()
    r = c.post('/audits/a1/edits', json={'comments': {'L1': 'Confirmado por teléfono'}, 'official_comment': 'Nota locator'})
    assert r.status_code == 200
    rec = history.get('a1')
    assert rec['snapshot']['audit']['clusters'][0]['presenter_comment'] == 'Confirmado por teléfono'
    assert rec['snapshot']['official_comment'] == 'Nota locator'


def test_edits_empty_comment_clears_it(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed(comment='viejo')
    c = app_module.app.test_client()
    c.post('/audits/a1/edits', json={'comments': {'L1': ''}})
    assert history.get('a1')['snapshot']['audit']['clusters'][0]['presenter_comment'] is None


def test_edits_404_when_audit_missing(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    c = app_module.app.test_client()
    assert c.post('/audits/nope/edits', json={'comments': {}}).status_code == 404


def test_delete_audit_removes_it(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch); _seed()
    c = app_module.app.test_client()
    assert c.delete('/audits/a1').status_code == 200
    assert history.get('a1') is None


def test_delete_audit_idempotent(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    c = app_module.app.test_client()
    assert c.delete('/audits/nope').status_code == 200  # no existe → ok igualmente


def test_cache_clear_endpoint_ok():
    c = app_module.app.test_client()
    assert c.post('/cache/clear').status_code == 200
