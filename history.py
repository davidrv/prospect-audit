"""Persistencia de auditorías (SQLite) — habilita la lista "Auditorías
recientes" del input y reabrir una auditoría sin recomputarla.

Mismo patrón que cache.py (una conexión SQLite protegida por lock, config
leída del entorno en tiempo de llamada), pero con tabla, fichero y flag
propios para no acoplarse a la caché:
- fichero: AUDIT_HISTORY_PATH (por defecto junto a la app).
- deshabilitar: DISABLE_AUDIT_HISTORY=1 (los tests lo activan en conftest para
  mantener la hermeticidad; test_history.py lo reactiva apuntando a un tmp).

Cada fila guarda los metadatos para el listado + el snapshot JSON completo del
resultado del job (`{**results, audit, official_comment}`) para poder reabrir
la pantalla de Resultados tal cual. Best-effort: nunca lanza excepción.
"""
import json
import os
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn = None


def _enabled():
    return os.environ.get('DISABLE_AUDIT_HISTORY', '').strip() != '1'


def _db_path():
    return os.environ.get('AUDIT_HISTORY_PATH',
                          os.path.join(os.path.dirname(__file__), '.audit_history.sqlite'))


def _connection():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.execute(
            'CREATE TABLE IF NOT EXISTS audits ('
            'id TEXT PRIMARY KEY, name TEXT, city TEXT, score INTEGER, '
            'total_locations INTEGER, created_at REAL, snapshot TEXT)')
        _conn.commit()
    return _conn


def save(audit_id, name, city, score, total_locations, snapshot):
    """Guarda (o reemplaza) una auditoría. `snapshot` debe ser JSON-serializable
    (el resultado del job). Never raises."""
    if not _enabled():
        return
    try:
        payload = json.dumps(snapshot)
    except (TypeError, ValueError):
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute(
                'INSERT OR REPLACE INTO audits '
                '(id, name, city, score, total_locations, created_at, snapshot) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (audit_id, name, city, score, total_locations, time.time(), payload))
            conn.commit()
    except Exception:
        pass


def recent(limit=10):
    """Metadatos de las últimas `limit` auditorías (sin el snapshot), de más
    reciente a más antigua. Never raises — devuelve [] si algo falla."""
    if not _enabled():
        return []
    try:
        with _lock:
            rows = _connection().execute(
                'SELECT id, name, city, score, total_locations, created_at '
                'FROM audits ORDER BY created_at DESC LIMIT ?', (int(limit),)).fetchall()
    except Exception:
        return []
    return [
        {'id': r[0], 'name': r[1], 'city': r[2], 'score': r[3],
         'total_locations': r[4], 'created_at': r[5]}
        for r in rows
    ]


def delete(audit_id):
    """Elimina una auditoría del histórico. Never raises."""
    if not _enabled():
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute('DELETE FROM audits WHERE id = ?', (audit_id,))
            conn.commit()
    except Exception:
        pass


def get(audit_id):
    """El snapshot completo de una auditoría (para reabrir Resultados), o None."""
    if not _enabled():
        return None
    try:
        with _lock:
            row = _connection().execute(
                'SELECT name, city, score, total_locations, created_at, snapshot '
                'FROM audits WHERE id = ?', (audit_id,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        snapshot = json.loads(row[5])
    except (ValueError, TypeError):
        return None
    return {'id': audit_id, 'name': row[0], 'city': row[1], 'score': row[2],
            'total_locations': row[3], 'created_at': row[4], 'snapshot': snapshot}
