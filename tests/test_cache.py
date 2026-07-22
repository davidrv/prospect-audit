import time

import cache


def _enable_temp_cache(monkeypatch, tmp_path):
    # conftest globally disables the cache; re-enable it here against a temp
    # SQLite file and force a fresh connection.
    monkeypatch.delenv('DISABLE_AUDIT_CACHE', raising=False)
    monkeypatch.setenv('AUDIT_CACHE_PATH', str(tmp_path / 'test_cache.sqlite'))
    monkeypatch.setattr(cache, '_conn', None)


def test_cache_set_get_roundtrip(monkeypatch, tmp_path):
    _enable_temp_cache(monkeypatch, tmp_path)
    cache.set('k1', {'reviews': [1, 2, 3], 'nested': {'a': True}})
    assert cache.get('k1') == {'reviews': [1, 2, 3], 'nested': {'a': True}}


def test_cache_miss_returns_none(monkeypatch, tmp_path):
    _enable_temp_cache(monkeypatch, tmp_path)
    assert cache.get('does-not-exist') is None


def test_cache_respects_ttl(monkeypatch, tmp_path):
    _enable_temp_cache(monkeypatch, tmp_path)
    cache.set('k2', {'v': 1})
    assert cache.get('k2', ttl=1000) == {'v': 1}     # fresh enough
    assert cache.get('k2', ttl=-1) is None            # already older than a negative ttl → expired


def test_cache_can_store_none_valued_match(monkeypatch, tmp_path):
    # The SerpApi "clean no-match" case stores {'match': None} — must be a HIT
    # (returns the dict), not treated as a miss.
    _enable_temp_cache(monkeypatch, tmp_path)
    cache.set('serpapi:x', {'match': None})
    assert cache.get('serpapi:x') == {'match': None}


def test_cache_disabled_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv('DISABLE_AUDIT_CACHE', '1')
    monkeypatch.setenv('AUDIT_CACHE_PATH', str(tmp_path / 'off.sqlite'))
    monkeypatch.setattr(cache, '_conn', None)
    cache.set('k', {'v': 1})
    assert cache.get('k') is None  # disabled → never stores/reads


def test_cache_get_never_raises_on_bad_state(monkeypatch, tmp_path):
    _enable_temp_cache(monkeypatch, tmp_path)
    # Non-serializable value is silently dropped, not raised.
    cache.set('bad', object())
    assert cache.get('bad') is None
