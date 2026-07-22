import time

import app as app_module


def _wait_for(condition_fn, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition_fn():
            return True
        time.sleep(0.01)
    return False


def test_search_start_without_name_returns_400():
    client = app_module.app.test_client()
    resp = client.post('/search/start', data={})
    assert resp.status_code == 400


def test_search_start_runs_audit_in_background_and_reports_progress(monkeypatch):
    def fake_run_audit(name, city, official_urls, csv_locations, csv_errors,
                       progress=None, status=None, should_cancel=None, check_llm_visibility=False):
        progress('Buscando en Google Maps…')
        progress('Google Maps: 3 sede(s) encontradas.')
        return {'google': [], 'apple': [], 'azure': [], 'official': [],
                'official_errors': [], 'official_findings': [], 'site_analysis': []}, \
               {'clusters': [], 'summary': {'total_locations': 0}}

    monkeypatch.setattr(app_module, '_run_audit', fake_run_audit)

    client = app_module.app.test_client()
    resp = client.post('/search/start', data={'name': 'Zara', 'city': 'Barcelona'})
    assert resp.status_code == 200
    job_id = resp.get_json()['job_id']

    assert _wait_for(lambda: app_module._jobs[job_id]['status'] == 'done')

    status = client.get(f'/jobs/{job_id}/status').get_json()
    assert status['status'] == 'done'
    assert 'Buscando en Google Maps…' in status['progress']
    assert 'Google Maps: 3 sede(s) encontradas.' in status['progress']
    assert status['result']['audit']['summary']['total_locations'] == 0


def test_job_status_supports_incremental_polling_via_since(monkeypatch):
    job_id = app_module._new_job()
    emit = app_module._job_progress_fn(job_id)
    emit('paso 1')
    emit('paso 2')

    client = app_module.app.test_client()
    first = client.get(f'/jobs/{job_id}/status').get_json()
    assert first['progress'] == ['paso 1', 'paso 2']
    assert first['progress_count'] == 2

    emit('paso 3')
    second = client.get(f'/jobs/{job_id}/status?since={first["progress_count"]}').get_json()
    assert second['progress'] == ['paso 3']


def test_job_status_unknown_job_returns_404():
    client = app_module.app.test_client()
    resp = client.get('/jobs/does-not-exist/status')
    assert resp.status_code == 404


def test_search_start_job_reports_error_status_on_exception(monkeypatch):
    def fake_run_audit(*a, **k):
        raise RuntimeError('boom')

    monkeypatch.setattr(app_module, '_run_audit', fake_run_audit)

    client = app_module.app.test_client()
    resp = client.post('/search/start', data={'name': 'Zara'})
    job_id = resp.get_json()['job_id']

    assert _wait_for(lambda: app_module._jobs[job_id]['status'] == 'error')
    status = client.get(f'/jobs/{job_id}/status').get_json()
    assert status['status'] == 'error'
    assert 'boom' in status['error']


def test_report_start_returns_pdf_as_base64_in_final_result(monkeypatch):
    def fake_run_audit(name, city, official_urls, csv_locations, csv_errors,
                       progress=None, status=None, should_cancel=None, check_llm_visibility=False):
        return {'google': [], 'apple': [], 'azure': [], 'official': [],
                'official_errors': [], 'official_findings': [], 'site_analysis': []}, \
               {'clusters': [], 'summary': {'total_locations': 0}}

    monkeypatch.setattr(app_module, '_run_audit', fake_run_audit)

    import report
    monkeypatch.setattr(report, 'render_report_pdf', lambda *a, **k: b'%PDF-fake-bytes')

    client = app_module.app.test_client()
    resp = client.post('/report/start', data={'name': 'Zara'})
    job_id = resp.get_json()['job_id']

    assert _wait_for(lambda: app_module._jobs[job_id]['status'] == 'done')
    status = client.get(f'/jobs/{job_id}/status').get_json()
    assert status['result']['filename'] == 'auditoria_Zara.pdf'

    import base64
    assert base64.b64decode(status['result']['pdf_base64']) == b'%PDF-fake-bytes'


def test_run_audit_raises_when_cancelled_before_any_source_call():
    import pytest
    # should_cancel True -> the first phase-boundary check bails out before any
    # network call, so this is safe to run without mocking the searches.
    with pytest.raises(app_module._AuditCancelled):
        app_module._run_audit('Zara', 'Barcelona', [], [], [], should_cancel=lambda: True)


def test_job_cancel_flags_running_job():
    job_id = app_module._new_job()
    client = app_module.app.test_client()
    resp = client.post(f'/jobs/{job_id}/cancel')
    assert resp.status_code == 200
    assert app_module._jobs[job_id]['cancelled'] is True


def test_job_cancel_unknown_job_returns_404():
    client = app_module.app.test_client()
    assert client.post('/jobs/nope/cancel').status_code == 404


def test_status_payload_includes_sources_and_percent():
    job_id = app_module._new_job()
    client = app_module.app.test_client()
    status = client.get(f'/jobs/{job_id}/status').get_json()
    assert set(status['sources']) == {'google', 'apple', 'azure', 'official', 'llm'}
    assert status['percent'] == 0
