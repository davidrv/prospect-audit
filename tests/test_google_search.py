import contextlib

import app as app_module


def test_matches_prospect_name_accepts_branded_variants():
    query = app_module.normalize.name_norm('Movistar')
    assert app_module._matches_prospect_name(query, 'Tienda Movistar')
    assert app_module._matches_prospect_name(query, 'Movistar')


def test_matches_prospect_name_rejects_competitors_and_unrelated_shops():
    query = app_module.normalize.name_norm('Movistar')
    for competitor in ('Tienda Orange', 'Vodafone Barcelona - Manso', 'Rogent Telefonia',
                        'MR MOBILS', 'World Mobile', 'Ems Informática'):
        assert not app_module._matches_prospect_name(query, competitor)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_search_google_filters_out_unrelated_results_before_fetching_details(monkeypatch):
    monkeypatch.setattr(app_module, '_geocode_city', lambda city: None)
    monkeypatch.setattr(app_module, '_google_review_summary', lambda place_id, key: None)
    monkeypatch.setattr(app_module, '_attach_scraped_reviews', lambda details, progress=None: None)
    monkeypatch.setenv('GOOGLE_PLACES_API_KEY', 'fake-key')

    places = [
        {'place_id': 'g1', 'name': 'Tienda Movistar', 'formatted_address': 'X'},
        {'place_id': 'g2', 'name': 'Tienda Orange', 'formatted_address': 'Y'},
        {'place_id': 'g3', 'name': 'Vodafone Barcelona - Manso', 'formatted_address': 'Z'},
    ]
    details_calls = []

    def fake_get(url, params=None, timeout=None):
        if 'textsearch' in url:
            return _FakeResponse({'status': 'OK', 'results': places, 'next_page_token': None})
        details_calls.append(params['place_id'])
        place = next(p for p in places if p['place_id'] == params['place_id'])
        return _FakeResponse({'status': 'OK', 'result': dict(place)})

    monkeypatch.setattr(app_module.requests, 'get', fake_get)

    results = app_module._search_google('Movistar', 'Barcelona')

    # Only the genuine match should ever reach the (quota-costing) Details call.
    assert details_calls == ['g1']
    assert [r['name'] for r in results] == ['Tienda Movistar']


def _fake_signals(reviews=None, action_links=None, posts=None):
    return {'reviews': reviews or [], 'action_links': action_links or [], 'posts': posts or []}


@contextlib.contextmanager
def _fake_browser_cm():
    yield object()  # a stub browser; scrape_place_signals is mocked, so it's never used


def _stub_browser(monkeypatch):
    # Force the Playwright fallback path (these tests mock scrape_place_signals)
    # and stub the browser so no real Chromium launches.
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', False)
    monkeypatch.setattr(app_module, '_scrape_browser', _fake_browser_cm)


def test_attach_scraped_reviews_stores_result_per_place(monkeypatch):
    _stub_browser(monkeypatch)
    monkeypatch.setattr(app_module, 'scrape_place_signals',
                         lambda place_id, **kwargs: _fake_signals(
                             reviews=[{'author_name': f'reviewer-{place_id}'}],
                             action_links=[{'type': 'reservation', 'label': 'Reservar'}],
                             posts=[{'text': f'post-{place_id}'}]))
    places = [{'place_id': 'g1'}, {'place_id': 'g2'}]

    app_module._attach_scraped_reviews(places)

    assert places[0]['scraped_reviews'] == [{'author_name': 'reviewer-g1'}]
    assert places[0]['scraped_action_links'] == [{'type': 'reservation', 'label': 'Reservar'}]
    assert places[0]['scraped_posts'] == [{'text': 'post-g1'}]
    assert places[1]['scraped_reviews'] == [{'author_name': 'reviewer-g2'}]


def test_attach_scraped_reviews_never_raises_on_failure(monkeypatch):
    _stub_browser(monkeypatch)

    def _boom(place_id, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(app_module, 'scrape_place_signals', _boom)
    places = [{'place_id': 'g1'}]

    app_module._attach_scraped_reviews(places)  # must not raise

    assert 'scraped_reviews' not in places[0]


def test_attach_scraped_reviews_skips_places_without_place_id(monkeypatch):
    _stub_browser(monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, 'scrape_place_signals',
                         lambda place_id, **kwargs: calls.append(place_id) or _fake_signals())
    places = [{'name': 'No place_id here'}]

    app_module._attach_scraped_reviews(places)

    assert calls == []
    assert 'scraped_reviews' not in places[0]


def test_attach_scraped_reviews_uses_api_path_when_serpapi_enabled(monkeypatch):
    # With the API path on, it must call google_signals.fetch_place_signals
    # (HTTP) and NOT launch a browser.
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', True)
    monkeypatch.setattr(app_module, '_scrape_browser',
                        lambda: (_ for _ in ()).throw(AssertionError('browser must not launch in API mode')))
    monkeypatch.setattr(app_module.google_signals, 'fetch_place_signals',
                        lambda place_id, **kw: {'reviews': [{'author_name': f'r-{place_id}', 'has_owner_reply': True}],
                                                'action_links': [{'type': 'menu', 'label': 'Menú'}], 'posts': []})
    places = [{'place_id': 'g1', 'name': 'Foo'}, {'place_id': 'g2', 'name': 'Bar'}]

    app_module._attach_scraped_reviews(places)

    assert places[0]['scraped_reviews'] == [{'author_name': 'r-g1', 'has_owner_reply': True}]
    assert places[0]['scraped_action_links'] == [{'type': 'menu', 'label': 'Menú'}]
    assert places[1]['scraped_reviews'] == [{'author_name': 'r-g2', 'has_owner_reply': True}]


def test_attach_scraped_reviews_api_path_never_raises(monkeypatch):
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', True)
    monkeypatch.setattr(app_module, '_scrape_browser',
                        lambda: (_ for _ in ()).throw(AssertionError('browser must not launch in API mode')))

    def _boom(place_id, **kw):
        raise RuntimeError('serpapi down')

    monkeypatch.setattr(app_module.google_signals, 'fetch_place_signals', _boom)
    places = [{'place_id': 'g1', 'name': 'Foo'}]

    app_module._attach_scraped_reviews(places)  # must not raise
    assert 'scraped_reviews' not in places[0]


# ── action links: solo las N peores sedes ───────────────────────────

def test_attach_action_links_worst_only_top_n(monkeypatch):
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', True)
    monkeypatch.setattr(app_module, '_REVIEW_SCRAPING_ENABLED', True)
    monkeypatch.setattr(app_module, '_ACTION_LINKS_MAX_VENUES', 2)

    fetched = []

    def fake_links(place_id, session=None):
        fetched.append(place_id)
        return [{'type': 'reservation', 'label': 'Reservar'}]

    monkeypatch.setattr(app_module.google_signals, 'fetch_action_links', fake_links)

    # 4 clusters con Google, ya en orden peor→mejor (p1 peor)
    def _c(i):
        return {'sources_present': ['google'],
                'by_source': {'google': {'raw': {'place_id': f'p{i}'}}},
                'venue_metrics': {'action_links_google': {'value': 'N/D'}}}
    clusters = [_c(i) for i in range(4)]

    app_module._attach_action_links_worst(clusters)

    assert fetched == ['p0', 'p1']                              # solo las 2 peores
    assert clusters[0]['venue_metrics']['action_links_google']['value'] == 'Reservar'
    assert clusters[2]['venue_metrics']['action_links_google']['value'] == 'N/D'  # intacta


def test_attach_action_links_worst_noop_without_serpapi(monkeypatch):
    monkeypatch.setattr(app_module, '_GOOGLE_SIGNALS_VIA_SERPAPI', False)
    called = []
    monkeypatch.setattr(app_module.google_signals, 'fetch_action_links',
                        lambda *a, **k: called.append(1) or [])
    clusters = [{'sources_present': ['google'], 'by_source': {'google': {'raw': {'place_id': 'p0'}}},
                 'venue_metrics': {'action_links_google': {'value': 'N/D'}}}]
    app_module._attach_action_links_worst(clusters)
    assert called == []
