import base64

import app as app_module


def _basic_header(user, password):
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def test_auth_disabled_when_credentials_not_configured(monkeypatch):
    monkeypatch.setattr(app_module, '_BASIC_AUTH_USER', None)
    monkeypatch.setattr(app_module, '_BASIC_AUTH_PASS', None)
    client = app_module.app.test_client()
    resp = client.get('/')
    assert resp.status_code == 200


def test_auth_rejects_missing_credentials(monkeypatch):
    monkeypatch.setattr(app_module, '_BASIC_AUTH_USER', 'sales')
    monkeypatch.setattr(app_module, '_BASIC_AUTH_PASS', 'secret')
    client = app_module.app.test_client()
    resp = client.get('/')
    assert resp.status_code == 401


def test_auth_rejects_wrong_credentials(monkeypatch):
    monkeypatch.setattr(app_module, '_BASIC_AUTH_USER', 'sales')
    monkeypatch.setattr(app_module, '_BASIC_AUTH_PASS', 'secret')
    client = app_module.app.test_client()
    resp = client.get('/', headers=_basic_header('sales', 'wrong'))
    assert resp.status_code == 401


def test_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setattr(app_module, '_BASIC_AUTH_USER', 'sales')
    monkeypatch.setattr(app_module, '_BASIC_AUTH_PASS', 'secret')
    client = app_module.app.test_client()
    resp = client.get('/', headers=_basic_header('sales', 'secret'))
    assert resp.status_code == 200
