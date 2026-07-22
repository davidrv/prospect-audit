import os
from datetime import datetime, timezone

import google_signals as gs


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _review(iso, rating=5, name='Ana', text='Genial', response=None):
    r = {'user': {'name': name}, 'rating': rating, 'snippet': text, 'iso_date': iso, 'date': 'hace 1 semana'}
    if response:
        r['response'] = {'snippet': response}
    return r


def test_map_review_shape():
    m = gs._map_review(_review('2026-07-20T10:00:00Z', rating=4, name='Bob', response='Gracias'))
    assert m['author_name'] == 'Bob'
    assert m['rating'] == 4
    assert m['has_owner_reply'] is True
    assert m['time'] == int(datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc).timestamp())


def test_action_links_from_place_maps_structured_fields():
    place = {'reservation': [{'link': 'x'}], 'order_online_link': 'y', 'menu': {'link': 'z'}}
    types = sorted(l['type'] for l in gs._action_links_from_place(place))
    assert types == ['menu', 'order', 'reservation']


def test_action_links_empty_when_none_present():
    assert gs._action_links_from_place({'website': 'x'}) == []


def _fake_get_json(pages_by_token):
    """pages_by_token: dict token(or None) -> (reviews_list, next_token)."""
    def _impl(session, params):
        if params.get('engine') == 'google_maps':
            return {'place_results': {'order_online_link': 'y'}}
        reviews, nxt = pages_by_token[params.get('next_page_token')]
        out = {'reviews': reviews}
        if nxt:
            out['serpapi_pagination'] = {'next_page_token': nxt}
        return out
    return _impl


def test_fetch_reviews_paginates_and_filters_window(monkeypatch):
    monkeypatch.setenv('SERPAPI_KEY', 'fake')
    now = datetime.now(timezone.utc)
    recent = _iso(now)
    old = '2024-03-28T09:00:00Z'  # far outside the 3-month window
    # page 1: 2 recent; page 2 STARTS with an out-of-order old review then 2 recent;
    # page 3: all old -> stop.
    pages = {
        None:  ([_review(recent), _review(recent)], 't2'),
        't2':  ([_review(old), _review(recent), _review(recent)], 't3'),
        't3':  ([_review(old), _review(old)], None),
    }
    monkeypatch.setattr(gs, '_get_json', _fake_get_json(pages))
    import requests
    revs = gs._fetch_reviews('PID', gs._cutoff_epoch(3), requests)
    # 2 (page1) + 2 in-window (page2, the leading old one filtered out) = 4;
    # page3 all-old contributes nothing and stops pagination.
    assert len(revs) == 4


def test_fetch_place_signals_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv('SERPAPI_KEY', raising=False)
    assert gs.fetch_place_signals('PID') == {'reviews': [], 'action_links': [], 'posts': []}


def test_fetch_place_signals_combines_reviews_and_links(monkeypatch):
    monkeypatch.setenv('SERPAPI_KEY', 'fake')
    now = datetime.now(timezone.utc)
    pages = {None: ([_review(_iso(now), response='ok')], None)}
    monkeypatch.setattr(gs, '_get_json', _fake_get_json(pages))
    out = gs.fetch_place_signals('PID', months=3)
    assert len(out['reviews']) == 1
    assert out['reviews'][0]['has_owner_reply'] is True
    assert out['action_links'] == [{'type': 'order', 'label': 'Pedir online'}]
    assert out['posts'] == []
