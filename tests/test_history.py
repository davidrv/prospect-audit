import history


def _fresh_history(tmp_path, monkeypatch):
    """Point history.py at a throwaway SQLite file and re-enable it (conftest
    disables it globally for hermeticity)."""
    monkeypatch.delenv('DISABLE_AUDIT_HISTORY', raising=False)
    monkeypatch.setenv('AUDIT_HISTORY_PATH', str(tmp_path / 'history.sqlite'))
    monkeypatch.setattr(history, '_conn', None)


def test_save_and_get_roundtrip(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    snapshot = {'audit': {'summary': {'total_locations': 3}}, 'google': []}
    history.save('job1', 'Movistar', 'Barcelona', 52, 3, snapshot)

    record = history.get('job1')
    assert record['name'] == 'Movistar'
    assert record['city'] == 'Barcelona'
    assert record['score'] == 52
    assert record['total_locations'] == 3
    assert record['snapshot'] == snapshot


def test_recent_orders_newest_first_and_excludes_snapshot(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    history.save('a', 'A', 'Madrid', 10, 1, {'x': 1})
    history.save('b', 'B', 'Girona', 20, 2, {'y': 2})

    recent = history.recent(limit=10)
    assert [r['id'] for r in recent] == ['b', 'a']  # newest first
    assert 'snapshot' not in recent[0]


def test_get_missing_returns_none(tmp_path, monkeypatch):
    _fresh_history(tmp_path, monkeypatch)
    assert history.get('nope') is None


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv('DISABLE_AUDIT_HISTORY', '1')
    monkeypatch.setenv('AUDIT_HISTORY_PATH', str(tmp_path / 'history.sqlite'))
    monkeypatch.setattr(history, '_conn', None)

    history.save('x', 'X', 'Y', 1, 1, {'z': 1})
    assert history.recent() == []
    assert history.get('x') is None
