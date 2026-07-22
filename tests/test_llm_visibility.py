import llm_visibility as lv


def _cluster(cid, name='Movistar', address='Gran Via 10, Barcelona', types=('cell_phone_store', 'store'),
             present=('google',)):
    return {
        'cluster_id': cid,
        'sources_present': list(present),
        'canonical_label': name,
        'canonical_address': address,
        'by_source': {'google': {'name': name, 'raw': {'types': list(types)}}},
    }


# ── prompt building ──────────────────────────────────────────────────

def test_build_prompt_humanizes_category_and_uses_street_area():
    p = lv._build_prompt(_cluster('c1', address='Gran Via de les Corts 10, Barcelona'), 'Barcelona')
    assert p == 'tienda de telefonía móvil en Gran Via de les Corts, Barcelona'


def test_build_prompt_falls_back_to_city_without_area():
    p = lv._build_prompt(_cluster('c1', address='Barcelona'), 'Barcelona')
    assert p == 'tienda de telefonía móvil en Barcelona'


def test_humanize_category_falls_back():
    rec = {'raw': {'types': ['point_of_interest', 'establishment']}}
    assert lv._humanize_category(rec) == 'negocio'


# ── detection ────────────────────────────────────────────────────────

def test_detect_appears_in_sources_with_position():
    result = {'sources': [{'position': 3, 'url': 'https://movistar.es/tienda', 'label': 'Movistar'},
                          {'position': 1, 'url': 'https://orange.es', 'label': 'Orange'}]}
    d = lv._detect(result, 'movistar')
    assert d['appears'] is True
    assert d['position'] == 3


def test_detect_appears_in_text_only_no_position():
    result = {'sources': [], 'text': 'Puedes ir a la tienda Movistar del centro.'}
    d = lv._detect(result, 'movistar')
    assert d['appears'] is True
    assert d['position'] is None


def test_detect_not_present():
    result = {'sources': [{'position': 1, 'url': 'https://orange.es', 'label': 'Orange'}],
              'text': 'Te recomiendo Orange y Vodafone.'}
    assert lv._detect(result, 'movistar')['appears'] is False


def test_detect_via_entities():
    result = {'entities': [{'name': 'Movistar', 'type': 'company'}]}
    assert lv._detect(result, 'movistar')['appears'] is True


# ── fetch_llm_visibility ─────────────────────────────────────────────

def test_fetch_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(lv, '_key', lambda: '')
    out = lv.fetch_llm_visibility([_cluster('c1')], 'Movistar', 'Barcelona')
    assert out['venues_checked'] == 0
    assert out['per_venue'] == {}


def test_fetch_aggregates_and_caps_venues(monkeypatch):
    monkeypatch.setattr(lv, '_key', lambda: 'fake')
    calls = {'n': 0}

    def fake_get(session, prompt, country):
        calls['n'] += 1
        # aparece 2 de cada 3 veces (alterna)
        appears = calls['n'] % 3 != 0
        return {'sources': ([{'position': 2, 'url': 'https://movistar.es', 'label': 'Movistar'}] if appears else
                            [{'position': 1, 'url': 'https://orange.es', 'label': 'Orange'}]),
                'text': 'Movistar' if appears else 'Orange'}

    monkeypatch.setattr(lv, '_get_result', fake_get)
    clusters = [_cluster('c%d' % i) for i in range(8)]
    out = lv.fetch_llm_visibility(clusters, 'Movistar', 'Barcelona', runs=3, max_venues=5, country='es')

    assert out['venues_checked'] == 5           # capped at max_venues
    assert out['checks_total'] == 15            # 5 venues × 3 runs
    assert out['calls'] == 15
    assert len(out['per_venue']) == 5
    assert all('runs' in v and len(v['runs']) == 3 for v in out['per_venue'].values())


def test_fetch_best_effort_on_http_failure(monkeypatch):
    monkeypatch.setattr(lv, '_key', lambda: 'fake')
    monkeypatch.setattr(lv, '_get_result', lambda *a, **k: None)  # every call fails
    out = lv.fetch_llm_visibility([_cluster('c1')], 'Movistar', 'Barcelona', runs=3, max_venues=5)
    v = out['per_venue']['c1']
    assert v['hits'] == 0
    assert all(r['appears'] is None for r in v['runs'])  # unknown, not False
    assert out['hits_total'] == 0
