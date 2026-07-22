"""Tiny persistent key/value cache (SQLite) with TTL.

Purpose: the two most expensive per-location steps of an audit — Playwright
review/signals scraping (slow) and SerpApi Apple enrichment (paid) — are
keyed by a stable identity (Google place_id / name+coords). Caching their
results across audits makes re-auditing the same prospect near-instant and
near-free, which is the main lever for both speed and the ≤3–5€/report cost
target.

Design notes:
- One SQLite file (path from AUDIT_CACHE_PATH, default alongside the app).
- Thread-safe: a single connection with `check_same_thread=False` guarded by
  a lock — fine for this app's modest concurrency (audits fan out to ~5–10
  threads). Not meant for multi-process; if the app ever runs multiple
  gunicorn workers, SQLite WAL or a shared store would be the next step.
- Enable/disable and TTL are read from the environment AT CALL TIME so tests
  (and ops) can flip `DISABLE_AUDIT_CACHE=1` without caring about import order.
- Values are JSON, so only store JSON-serializable data.
"""
import json
import os
import sqlite3
import threading
import time

_DEFAULT_TTL_DAYS = 14
_lock = threading.Lock()
_conn = None


def _enabled():
    return os.environ.get('DISABLE_AUDIT_CACHE', '').strip() != '1'


def _db_path():
    return os.environ.get('AUDIT_CACHE_PATH',
                          os.path.join(os.path.dirname(__file__), '.audit_cache.sqlite'))


def default_ttl():
    try:
        days = float(os.environ.get('AUDIT_CACHE_TTL_DAYS', _DEFAULT_TTL_DAYS))
    except ValueError:
        days = _DEFAULT_TTL_DAYS
    return days * 86400


def _connection():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.execute('CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts REAL)')
        _conn.commit()
    return _conn


def get(key, ttl=None):
    """Returns the cached value for `key` if present and not older than `ttl`
    seconds (default from env), else None. Never raises — a broken cache
    degrades to a miss."""
    if not _enabled():
        return None
    ttl = default_ttl() if ttl is None else ttl
    try:
        with _lock:
            row = _connection().execute('SELECT v, ts FROM cache WHERE k = ?', (key,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    value_json, ts = row
    if ttl is not None and (time.time() - ts) > ttl:
        return None
    try:
        return json.loads(value_json)
    except (ValueError, TypeError):
        return None


def set(key, value):
    """Stores `value` (JSON-serializable) under `key` with the current
    timestamp. Never raises."""
    if not _enabled():
        return
    try:
        payload = json.dumps(value)
    except (TypeError, ValueError):
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute('INSERT OR REPLACE INTO cache (k, v, ts) VALUES (?, ?, ?)',
                         (key, payload, time.time()))
            conn.commit()
    except Exception:
        pass
