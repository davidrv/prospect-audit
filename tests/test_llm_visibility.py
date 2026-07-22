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


def test_category_prefers_specific_over_generic_store():
    c = _cluster('c1', types=('store', 'cell_phone_store', 'point_of_interest'))
    assert lv._category_for(c) == 'tienda de telefonía móvil'


def test_category_falls_back_to_other_source_when_google_has_no_useful_type():
    c = _cluster('c1', types=('point_of_interest', 'establishment'))
    c['by_source']['apple'] = {'category': 'Telecomunicaciones'}
    assert lv._category_for(c) == 'Telecomunicaciones'


def test_category_humanizes_specific_type_before_negocio():
    c = _cluster('c1', types=('point_of_interest', 'insurance_agency_XYZ'))
    # not in map, no other source -> humanize the specific type (not 'negocio')
    assert lv._category_for(c) == 'insurance agency XYZ'


def test_category_last_resort_negocio():
    c = _cluster('c1', types=('point_of_interest', 'establishment'))
    assert lv._category_for(c) == 'negocio'


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
    import threading
    monkeypatch.setattr(lv, '_key', lambda: 'fake')
    lock = threading.Lock()
    calls = {'n': 0}

    def fake_get(session, prompt, country):
        with lock:
            calls['n'] += 1
        return {'sources': [{'position': 2, 'url': 'https://movistar.es', 'label': 'Movistar'}],
                'text': 'Movistar'}

    monkeypatch.setattr(lv, '_get_result', fake_get)
    # direcciones distintas -> prompts distintos (evita colisión de clave de caché)
    clusters = [_cluster('c%d' % i, address='Calle %d, Barcelona' % i) for i in range(8)]
    out = lv.fetch_llm_visibility(clusters, 'Movistar', 'Barcelona', runs=3, max_venues=5,
                                  country='es', workers=5)

    assert out['venues_checked'] == 5           # capped at max_venues
    assert out['checks_total'] == 15            # 5 venues × 3 runs
    assert out['calls'] == 15
    assert calls['n'] == 15
    assert len(out['per_venue']) == 5
    assert all('runs' in v and len(v['runs']) == 3 for v in out['per_venue'].values())


def test_fetch_emits_progress_per_venue(monkeypatch):
    monkeypatch.setattr(lv, '_key', lambda: 'fake')
    monkeypatch.setattr(lv, '_get_result', lambda *a, **k: {'text': 'x'})
    msgs = []
    clusters = [_cluster('c%d' % i, address='Calle %d, Barcelona' % i) for i in range(3)]
    lv.fetch_llm_visibility(clusters, 'Movistar', 'Barcelona', runs=1, max_venues=3,
                            workers=3, progress=lambda m: msgs.append(m))
    assert len(msgs) == 3                        # una línea de progreso por sede
    assert '3/3' in msgs[-1]


def test_fetch_best_effort_on_http_failure(monkeypatch):
    monkeypatch.setattr(lv, '_key', lambda: 'fake')
    monkeypatch.setattr(lv, '_get_result', lambda *a, **k: None)  # every call fails
    out = lv.fetch_llm_visibility([_cluster('c1')], 'Movistar', 'Barcelona', runs=3, max_venues=5)
    v = out['per_venue']['c1']
    assert v['hits'] == 0
    assert all(r['appears'] is None for r in v['runs'])  # unknown, not False
    assert out['hits_total'] == 0
