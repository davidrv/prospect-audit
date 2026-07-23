import official


def test_flatten_jsonld_single_dict():
    node = {'@type': 'Restaurant', 'name': 'Foo'}
    assert official._flatten_jsonld(node) == [node]


def test_flatten_jsonld_itemlist():
    node = {
        '@type': 'ItemList',
        'itemListElement': [
            {'@type': 'ListItem', 'item': {'@type': 'Restaurant', 'name': 'A'}},
            {'@type': 'ListItem', 'item': {'@type': 'Restaurant', 'name': 'B'}},
        ],
    }
    flat = official._flatten_jsonld(node)
    assert [n['name'] for n in flat] == ['A', 'B']


def test_flatten_jsonld_graph():
    node = {'@graph': [{'@type': 'Restaurant', 'name': 'A'}]}
    assert official._flatten_jsonld(node)[0]['name'] == 'A'


def test_looks_like_business_whitelisted_type():
    assert official._looks_like_business({'@type': 'Restaurant'})


def test_looks_like_business_duck_typing_fallback():
    node = {'@type': 'SomeWeirdType', 'address': 'X', 'telephone': '123'}
    assert official._looks_like_business(node)


def test_looks_like_business_rejects_unrelated():
    assert not official._looks_like_business({'@type': 'Article', 'headline': 'hi'})


def test_format_hours_schema_org_url_days():
    spec = [{'dayOfWeek': 'https://schema.org/Monday', 'opens': '09:00', 'closes': '22:00'}]
    assert official._format_hours(spec) == ['Lunes: 09:00–22:00']


def test_format_hours_multiple_days_list():
    spec = [{'dayOfWeek': ['Monday', 'Tuesday'], 'opens': '09:00', 'closes': '20:00'}]
    assert official._format_hours(spec) == ['Lunes: 09:00–20:00', 'Martes: 09:00–20:00']


def test_format_address_dict():
    addr = {'streetAddress': 'Calle Pelai 62', 'postalCode': '08001', 'addressLocality': 'Barcelona'}
    assert official._format_address(addr) == 'Calle Pelai 62, 08001, Barcelona'


def test_format_address_string():
    assert official._format_address('Calle Pelai 62') == 'Calle Pelai 62'


def test_map_node_produces_normalized_record():
    node = {'@type': 'Restaurant', 'name': 'Foo', 'address': 'Calle X', 'telephone': '932123456'}
    record = official._map_node(node, 'https://example.com')
    assert record['source'] == 'official'
    assert record['name'] == 'Foo'
    assert record['phone'] == '34932123456'


def test_map_node_verify_url_is_source_page_not_website():
    # website (used for R6/R7 comparison) must NOT be conflated with the page
    # we scraped this from (verify_url) — a store-locator detail page isn't
    # necessarily the business's actual homepage.
    node = {'@type': 'Restaurant', 'name': 'Foo', 'address': 'Calle X'}
    record = official._map_node(node, 'https://example.com/stores/foo')
    assert record['verify_url'] == 'https://example.com/stores/foo'
    assert record['website'] is None


def test_map_node_uses_explicit_url_as_website():
    node = {'@type': 'Restaurant', 'name': 'Foo', 'address': 'Calle X', 'url': 'https://foo.com'}
    record = official._map_node(node, 'https://example.com/stores/foo')
    assert record['website_display'] == 'https://foo.com'
    assert record['verify_url'] == 'https://example.com/stores/foo'


def test_parse_official_csv_reads_optional_url_column():
    import io
    csv_content = 'name,address,url\nFoo,Calle X,https://example.com/foo\n'
    result = official.parse_official_csv(io.BytesIO(csv_content.encode()))
    assert result['locations'][0]['verify_url'] == 'https://example.com/foo'


def test_extract_one_no_structured_data(monkeypatch):
    class FakeResponse:
        ok = True
        text = '<html><body>no jsonld here</body></html>'

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    assert locations == []
    assert status == 'no_schema'
    assert 'schema.org' in error
    assert sub_analyses == []


def test_extract_one_with_structured_data():
    html = '''<html><head><script type="application/ld+json">
    {"@type": "Restaurant", "name": "Foo", "address": "Calle X", "telephone": "932123456"}
    </script></head></html>'''

    class FakeResponse:
        ok = True
        text = html

    import unittest.mock
    with unittest.mock.patch.object(official.requests, 'get', return_value=FakeResponse()):
        locations, error, status, sub_analyses = official._extract_one('https://example.com')

    assert error is None
    assert status == 'found'
    assert len(locations) == 1
    assert locations[0]['name'] == 'Foo'


def test_extract_one_inaccessible_page(monkeypatch):
    import requests as requests_module

    def raise_conn_error(*a, **k):
        raise requests_module.exceptions.ConnectionError('blocked')

    monkeypatch.setattr(official.requests, 'get', raise_conn_error)
    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    assert locations == []
    assert status == 'inaccessible'


def test_extract_one_http_error_is_inaccessible(monkeypatch):
    class FakeResponse:
        ok = False
        status_code = 403
        text = ''

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    assert locations == []
    assert status == 'inaccessible'
    assert '403' in error


def test_extract_official_dedupes_and_collects_errors(monkeypatch):
    def fake_extract_one(url, city=None):
        if 'good' in url:
            return [official.make_record('official', f'{url}#a', name='Foo', formatted_address='X')], None, 'found', []
        return [], 'no data', 'inaccessible', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://good.com', 'https://bad.com'])
    assert len(result['locations']) == 1
    assert result['errors'] == [{'url': 'https://bad.com', 'reason': 'no data'}]
    assert result['findings'] == [{'url': 'https://bad.com', 'severity': 'minor',
        'message': 'No hemos podido acceder automáticamente a este store locator (bloqueo o '
                   'error de acceso). Prueba a subir un CSV con las sedes en su lugar.'}]


def test_extract_official_flags_moderate_finding_when_no_schema_markup(monkeypatch):
    def fake_extract_one(url, city=None):
        return [], 'sin datos estructurados schema.org', 'no_schema', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://example.com'])
    assert result['findings'][0]['severity'] == 'moderate'
    assert 'schema.org' in result['findings'][0]['message']


def test_extract_official_no_finding_when_schema_found(monkeypatch):
    def fake_extract_one(url, city=None):
        return [official.make_record('official', url, name='Foo', formatted_address='X')], None, 'found', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://example.com'])
    assert result['findings'] == []


def test_extract_official_keeps_moderate_finding_for_found_heuristic(monkeypatch):
    # Even when the heuristic scraper (or Playwright) DOES recover data, the
    # absence of real schema.org markup is still a real, separate finding.
    def fake_extract_one(url, city=None):
        return [official.make_record('official', url, name='Foo', formatted_address='X')], None, 'found_heuristic', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://example.com'])
    assert len(result['locations']) == 1
    assert result['findings'][0]['severity'] == 'moderate'


def test_extract_official_exposes_site_analysis_with_inferred_page_type(monkeypatch):
    def fake_extract_one(url, city=None):
        if 'index' in url:
            return [official.make_record('official', f'{url}#{i}', name=f'Store {i}', formatted_address='X')
                    for i in range(3)], None, 'found', []
        return [official.make_record('official', url, name='Store', formatted_address='X')], None, 'found', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://example.com/index', 'https://example.com/store/1'])

    by_url = {s['url']: s for s in result['site_analysis']}
    assert by_url['https://example.com/index'] == {
        'url': 'https://example.com/index', 'status': 'found', 'location_count': 3, 'page_type': 'index'}
    assert by_url['https://example.com/store/1'] == {
        'url': 'https://example.com/store/1', 'status': 'found', 'location_count': 1, 'page_type': 'store_page'}


# ── Heuristic HTML scraper ──────────────────────────────────────────────

_MOVISTAR_STYLE_HTML = '''
<html><body><ul>
<li>
  <strong>Espai Movistar</strong>
  <p>Plaça de Catalunya, 16</p>
  <p>08002 Barcelona (Barcelona)</p>
  <p>Tel: +34933010400</p>
  <a href="/store/espai-movistar">Más detalles</a>
</li>
<li>
  <strong>Tienda Movistar Girona</strong>
  <p>Carrer de Girona, 109</p>
  <p>08009 Barcelona (Barcelona)</p>
  <p>Tel: +34608854733</p>
  <a href="/store/girona">Más detalles</a>
</li>
</ul></body></html>
'''

_NO_LOCATION_DATA_HTML = '''
<html><body>
<nav><a href="/">Inicio</a></nav>
<h1>Bienvenido a nuestra tienda online</h1>
<p>Atención al cliente: 900 123 456</p>
<p>Ref producto 12345: envío gratis desde 08001</p>
<footer>© 2026 Empresa SA</footer>
</body></html>
'''


def test_extract_heuristic_single_location():
    records = official._extract_heuristic(_MOVISTAR_STYLE_HTML, 'https://example.com/barcelona')
    assert len(records) == 2
    espai = next(r for r in records if r['name'] == 'Espai Movistar')
    assert espai['formatted_address'] == 'Plaça de Catalunya, 16, 08002 Barcelona (Barcelona)'
    assert espai['phone_display'] == '+34933010400'


def test_extract_heuristic_does_not_mix_data_between_blocks():
    records = official._extract_heuristic(_MOVISTAR_STYLE_HTML, 'https://example.com/barcelona')
    girona = next(r for r in records if r['name'] == 'Tienda Movistar Girona')
    assert girona['phone_display'] == '+34608854733'
    assert 'Girona' in girona['formatted_address']
    assert 'Catalunya' not in girona['formatted_address']


def test_extract_heuristic_no_false_positives_on_unrelated_content():
    # A support phone with no nearby postal code, and a 5-digit number with
    # no capitalized locality after it, must not produce a fake location.
    records = official._extract_heuristic(_NO_LOCATION_DATA_HTML, 'https://example.com')
    assert records == []


def test_extract_heuristic_ignores_script_tag_contents():
    # Real bug found while testing Playwright end-to-end: a JS-rendered page
    # keeps its <script> tag (with the JS source that BUILT the visible
    # content) in the DOM even after rendering. If script/style aren't
    # stripped first, the address string literal inside the JS source itself
    # gets matched as a second, bogus location.
    html = '''
    <html><body>
    <script>
      document.getElementById('app').innerHTML = `
        <p>08002 Barcelona (Barcelona)</p>
      `;
    </script>
    <div id="app">
      <strong>Espai Movistar</strong>
      <p>Plaça de Catalunya, 16</p>
      <p>08002 Barcelona (Barcelona)</p>
      <p>Tel: +34933010400</p>
    </div>
    </body></html>
    '''
    records = official._extract_heuristic(html, 'https://example.com')
    assert len(records) == 1
    assert records[0]['name'] == 'Espai Movistar'


def test_extract_heuristic_no_name_discards_block():
    # The postal-code line is the very first content in its container — no
    # heading/bold/link/preceding text line to use as a name — must discard
    # rather than invent one.
    html = '<html><body><div>08002 Barcelona (Barcelona)</div></body></html>'
    assert official._extract_heuristic(html, 'https://example.com') == []


# ── _extract_one with the heuristic/Playwright fallback chain ──────────

def test_extract_one_uses_heuristic_before_playwright(monkeypatch):
    class FakeResponse:
        ok = True
        text = _MOVISTAR_STYLE_HTML

    def fail_if_called(*a, **k):
        raise AssertionError('Playwright should not be invoked when the raw-HTML heuristic already found data')

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    monkeypatch.setattr(official, '_render_with_playwright', fail_if_called)

    locations, error, status, sub_analyses = official._extract_one('https://example.com/barcelona')
    assert status == 'found_heuristic'
    assert len(locations) == 2


def test_extract_one_falls_back_to_playwright_when_raw_html_has_nothing(monkeypatch):
    class FakeResponse:
        ok = True
        text = _NO_LOCATION_DATA_HTML

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    monkeypatch.setattr(official, '_render_with_playwright', lambda url: (_MOVISTAR_STYLE_HTML, None))

    locations, error, status, sub_analyses = official._extract_one('https://example.com/barcelona')
    assert status == 'found_heuristic'
    assert len(locations) == 2


def test_extract_one_playwright_recovers_real_schema_org(monkeypatch):
    class FakeResponse:
        ok = True
        text = _NO_LOCATION_DATA_HTML

    schema_html = '''<html><head><script type="application/ld+json">
    {"@type": "Restaurant", "name": "Foo", "address": "Calle X", "telephone": "932123456"}
    </script></head></html>'''

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    monkeypatch.setattr(official, '_render_with_playwright', lambda url: (schema_html, None))

    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    # JS-injected JSON-LD still keeps the "no schema.org" finding — see
    # module docstring: not every crawler/AI assistant executes JS reliably.
    assert status == 'found_heuristic'
    assert len(locations) == 1
    assert locations[0]['name'] == 'Foo'


def test_extract_one_playwright_failure_degrades_to_no_schema(monkeypatch):
    class FakeResponse:
        ok = True
        text = _NO_LOCATION_DATA_HTML

    monkeypatch.setattr(official.requests, 'get', lambda *a, **k: FakeResponse())
    monkeypatch.setattr(official, '_render_with_playwright', lambda url: (None, 'no Chromium'))

    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    assert locations == []
    assert status == 'no_schema'


def test_extract_one_inaccessible_never_tries_heuristic_or_playwright(monkeypatch):
    def raise_conn_error(*a, **k):
        raise official.requests.exceptions.ConnectionError('blocked')

    def fail_if_called(*a, **k):
        raise AssertionError('must not be called when the page is inaccessible (anti-bot out of scope)')

    monkeypatch.setattr(official.requests, 'get', raise_conn_error)
    monkeypatch.setattr(official, '_extract_heuristic', fail_if_called)
    monkeypatch.setattr(official, '_render_with_playwright', fail_if_called)

    locations, error, status, sub_analyses = official._extract_one('https://example.com')
    assert status == 'inaccessible'


def test_render_with_playwright_missing_package_returns_none(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'playwright.sync_api':
            raise ImportError('no module named playwright')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    html, error = official._render_with_playwright('https://example.com')
    assert html is None
    assert 'Playwright' in error


# ── Link discovery + crawl (index page -> individual store pages) ──────

_INDEX_ONLY_HTML = '''
<html><body>
<nav><a href="/">Inicio</a><a href="/contacto">Contacto</a><a href="/legal">Aviso legal</a></nav>
<h1>Encuentra tu tienda</h1>
<ul>
  <li><a href="/tienda/barcelona-centro">Barcelona Centro</a></li>
  <li><a href="/tienda/girona">Girona</a></li>
  <li><a href="https://otherdomain.com/spy">Publicidad externa</a></li>
</ul>
</body></html>
'''


def test_discover_links_filters_nav_and_off_domain_links():
    links = official._discover_links(_INDEX_ONLY_HTML, 'https://example.com/tiendas')
    assert links == ['https://example.com/tienda/barcelona-centro', 'https://example.com/tienda/girona']


def test_discover_links_deduplicates_and_ignores_fragment_only_variants():
    html = '''<html><body>
    <a href="/tienda/x">X</a><a href="/tienda/x#top">X otra vez</a>
    </body></html>'''
    links = official._discover_links(html, 'https://example.com')
    assert links == ['https://example.com/tienda/x']


def test_discover_links_respects_max_candidate_cap(monkeypatch):
    monkeypatch.setattr(official, '_MAX_CANDIDATE_LINKS', 3)
    html = '<html><body>' + ''.join(f'<a href="/store/{i}">Store {i}</a>' for i in range(10)) + '</body></html>'
    links = official._discover_links(html, 'https://example.com')
    assert len(links) == 3


def test_crawl_candidate_links_merges_and_discards_irrelevant_pages(monkeypatch):
    store_html = '''<html><head><script type="application/ld+json">
    {"@type": "Restaurant", "name": "Foo Barcelona", "address": "Calle X", "telephone": "932123456"}
    </script></head></html>'''

    def fake_get(url, headers=None, timeout=None):
        class FakeResponse:
            ok = True
            text = store_html if 'barcelona-centro' in url else '<html><body>nada relevante aquí</body></html>'
        return FakeResponse()

    monkeypatch.setattr(official.requests, 'get', fake_get)

    locations, sub_analyses = official._crawl_candidate_links(_INDEX_ONLY_HTML, 'https://example.com/tiendas')
    assert len(locations) == 1
    assert locations[0]['name'] == 'Foo Barcelona'
    # Only the link that actually resolved to a location is reported —
    # "Girona" (found nothing) is silently discarded, not a false "no schema" finding.
    assert len(sub_analyses) == 1
    assert sub_analyses[0] == {
        'url': 'https://example.com/tienda/barcelona-centro', 'status': 'found',
        'location_count': 1, 'page_type': 'store_page'}


def test_crawl_candidate_links_returns_empty_when_no_candidate_links():
    locations, sub_analyses = official._crawl_candidate_links('<html><body>sin enlaces</body></html>', 'https://example.com')
    assert locations == []
    assert sub_analyses == []


def test_extract_one_crawls_index_page_with_no_direct_data(monkeypatch):
    # The index page itself has no schema.org and no extractable address
    # (just a directory of links) — real data only lives on each store page.
    store_html = '''<html><head><script type="application/ld+json">
    {"@type": "Restaurant", "name": "Foo Barcelona", "address": "Calle X", "telephone": "932123456"}
    </script></head></html>'''

    def fake_get(url, headers=None, timeout=None):
        class FakeResponse:
            ok = True
            text = _INDEX_ONLY_HTML if url == 'https://example.com/tiendas' else store_html
        return FakeResponse()

    def fail_if_called(*a, **k):
        raise AssertionError('Playwright should not be invoked when crawling already found data')

    monkeypatch.setattr(official.requests, 'get', fake_get)
    monkeypatch.setattr(official, '_render_with_playwright', fail_if_called)

    locations, error, status, sub_analyses = official._extract_one('https://example.com/tiendas')
    assert status == 'found_heuristic'
    assert error is None
    assert len(locations) == 2  # both /tienda/barcelona-centro and /tienda/girona resolve via the shared fake_get
    assert len(sub_analyses) == 2


def test_extract_official_reports_findings_for_crawled_store_pages(monkeypatch):
    def fake_extract_one(url, city=None):
        return (
            [official.make_record('official', 'a', name='Foo', formatted_address='X')],
            None, 'found_heuristic',
            [{'url': 'https://example.com/tienda/barcelona-centro', 'status': 'found',
              'location_count': 1, 'page_type': 'store_page'},
             {'url': 'https://example.com/tienda/girona', 'status': 'no_schema',
              'location_count': 1, 'page_type': 'store_page'}],
        )

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://example.com/tiendas'])

    urls_with_findings = {f['url'] for f in result['findings']}
    assert 'https://example.com/tienda/girona' in urls_with_findings  # no_schema -> moderate finding
    assert 'https://example.com/tienda/barcelona-centro' not in urls_with_findings  # found -> no finding
    # The crawled index page itself (e.g. a province-level listing like
    # tiendas.movistar.es/alava) is not a location page — it shouldn't get
    # its own "missing schema.org" finding just because crawling it found
    # real store pages, which already get their own findings above.
    assert 'https://example.com/tiendas' not in urls_with_findings
    by_url = {s['url']: s for s in result['site_analysis']}
    assert by_url['https://example.com/tienda/barcelona-centro']['page_type'] == 'store_page'
    assert by_url['https://example.com/tiendas']['page_type'] == 'index'


def test_extract_official_no_spurious_finding_for_single_page_listing_many_locations(monkeypatch):
    # Some pages list several distinct locations directly (no crawling
    # needed) without schema.org — e.g. an "alava" province page recovered
    # heuristically that yields multiple cities' worth of addresses. That's
    # still fundamentally an index/listing page, not one location's own
    # page, so it shouldn't get the per-store "missing schema.org" finding.
    def fake_extract_one(url, city=None):
        return (
            [official.make_record('official', f'{url}#{i}', name=f'Store {i}', formatted_address='X')
             for i in range(3)],
            None, 'found_heuristic', [],
        )

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://tiendas.movistar.es/alava'])

    assert result['findings'] == []


# ── City scoping of the official store-locator data ─────────────────────

def _loc(name, address):
    return official.make_record('official', name, name=name, formatted_address=address)


def test_location_matches_city_by_locality_after_postal_code():
    girona = official.name_norm('Girona')
    assert official._location_matches_city(
        _loc('T1', 'Gran Via de Jaume I, 70, 17001 Girona (Girona)'), girona)
    assert official._location_matches_city(
        _loc('T2', 'Carrer de la Creu, 30, 17002 Girona (Girona)'), girona)


def test_location_matches_city_excludes_province_suffix():
    # Salt is in Girona province ("(Girona)") but is NOT Girona city.
    girona = official.name_norm('Girona')
    assert not official._location_matches_city(
        _loc('T3', "Camí dels Carlins, s/n, 17190 Salt (Girona)"), girona)
    assert not official._location_matches_city(
        _loc('T4', 'Carrer de Peralada, 14, 17600 Figueres (Girona)'), girona)


def test_location_matches_city_excludes_street_named_like_city():
    # A Barcelona store on a street literally named "Carrer de Girona".
    girona = official.name_norm('Girona')
    assert not official._location_matches_city(
        _loc('T5', 'Carrer de Girona, 109, Local B-2, 08009 Barcelona (Barcelona)'), girona)


def test_location_matches_city_fallback_without_postal_province_shape():
    # No "postal City (Province)" shape → looser whole-address word match.
    girona = official.name_norm('Girona')
    assert official._location_matches_city(_loc('T6', 'Plaça del Vi, Girona'), girona)
    assert not official._location_matches_city(_loc('T7', 'Gran Via, Barcelona'), girona)


def test_extract_official_filters_locations_by_city(monkeypatch):
    def fake_extract_one(url, city=None):
        locs = [
            _loc('Girona Centre', 'Gran Via de Jaume I, 70, 17001 Girona (Girona)'),
            _loc('Salt', 'Camí dels Carlins, s/n, 17190 Salt (Girona)'),
            _loc('BCN Carrer Girona', 'Carrer de Girona, 109, 08009 Barcelona (Barcelona)'),
        ]
        return locs, None, 'found_heuristic', []

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://tiendas.movistar.es'], city='Girona')
    names = sorted(l['name'] for l in result['locations'])
    assert names == ['Girona Centre']  # Salt (province) and the Barcelona street both excluded


def test_extract_official_without_city_returns_all_locations(monkeypatch):
    def fake_extract_one(url, city=None):
        return ([_loc('A', '17001 Girona (Girona)'), _loc('B', '17190 Salt (Girona)')],
                None, 'found_heuristic', [])

    monkeypatch.setattr(official, '_extract_one', fake_extract_one)
    result = official.extract_official(['https://x'])  # no city → no scoping
    assert len(result['locations']) == 2


# ── City-guided deep crawl to individual store pages ────────────────────

_PROVINCE_INDEX_HTML = '''<html><body>
  <a href="/barcelona">Barcelona</a>
  <a href="/madrid">Madrid</a>
</body></html>'''

_CITY_INDEX_HTML = '''<html><body>
  <a href="/barcelona/barcelona">Barcelona</a>
  <a href="/barcelona/badalona">Badalona</a>
</body></html>'''

# City page: store cards link out via a generic "Más detalles" anchor —
# exactly the case that must NOT be filtered as generic nav.
_STORE_LIST_HTML = '''<html><body>
  <a href="/barcelona/barcelona/tienda-uno">Más detalles</a>
  <a href="/barcelona/barcelona/tienda-dos">Más detalles</a>
  <a href="/barcelona/badalona">Badalona</a>
</body></html>'''

def _store_schema_html(name, locality):
    return ('<html><head><script type="application/ld+json">'
            '{"@type":"LocalBusiness","name":"%s",'
            '"address":{"@type":"PostalAddress","streetAddress":"Calle X 1",'
            '"postalCode":"08001","addressLocality":"%s"},'
            '"telephone":"+34930000000",'
            '"openingHoursSpecification":[{"@type":"OpeningHoursSpecification",'
            '"dayOfWeek":"Monday","opens":"10:00","closes":"20:00"}]}'
            '</script></head></html>') % (name, locality)


def _fake_site(pages):
    class FakeResp:
        def __init__(self, text): self.ok, self.text, self.status_code = True, text, 200
    def fake_get(url, headers=None, timeout=None):
        path = url.split('tiendas.example.es')[-1].split('?')[0].rstrip('/') or '/'
        return FakeResp(pages.get(path, '<html></html>'))
    return fake_get


def test_city_crawl_reaches_individual_store_pages_with_schema(monkeypatch):
    pages = {
        '/': _PROVINCE_INDEX_HTML,
        '/barcelona': _CITY_INDEX_HTML,
        '/barcelona/barcelona': _STORE_LIST_HTML,
        '/barcelona/barcelona/tienda-uno': _store_schema_html('Tienda Uno', 'Barcelona'),
        '/barcelona/barcelona/tienda-dos': _store_schema_html('Tienda Dos', 'Barcelona'),
    }
    monkeypatch.setattr(official.requests, 'get', _fake_site(pages))
    locs, subs = official._crawl_for_city_stores(_PROVINCE_INDEX_HTML,
                                                  'https://tiendas.example.es/', 'Barcelona')
    names = sorted(l['name'] for l in locs)
    assert names == ['Tienda Dos', 'Tienda Uno']
    # Rich data pulled from each store's own schema.org page:
    uno = next(l for l in locs if l['name'] == 'Tienda Uno')
    assert uno['verify_url'] == 'https://tiendas.example.es/barcelona/barcelona/tienda-uno'
    assert uno['opening_hours']  # hours came from the detail page, not the listing


def test_city_crawl_does_not_descend_into_other_provinces(monkeypatch):
    # Madrid province must never be fetched when auditing Barcelona.
    fetched = []
    pages = {
        '/': _PROVINCE_INDEX_HTML,
        '/barcelona': _CITY_INDEX_HTML,
        '/barcelona/barcelona': _STORE_LIST_HTML,
        '/barcelona/barcelona/tienda-uno': _store_schema_html('Tienda Uno', 'Barcelona'),
        '/barcelona/barcelona/tienda-dos': _store_schema_html('Tienda Dos', 'Barcelona'),
    }
    base = _fake_site(pages)
    def tracking_get(url, headers=None, timeout=None):
        fetched.append(url)
        return base(url, headers, timeout)
    monkeypatch.setattr(official.requests, 'get', tracking_get)
    official._crawl_for_city_stores(_PROVINCE_INDEX_HTML, 'https://tiendas.example.es/', 'Barcelona')
    assert not any('/madrid' in u for u in fetched)


def test_city_crawl_falls_back_to_heuristic_when_no_schema(monkeypatch):
    # A city page that lists an address inline but has no per-store schema
    # pages still yields the heuristic listing (no regression for such sites).
    city_html = ('<html><body><a href="/barcelona/barcelona">Barcelona</a></body></html>')
    listing = ('<html><body><div>Tienda Test<br>Calle Mayor, 5, 08001 Barcelona</div>'
               '<div>Otra Tienda<br>Calle Menor, 9, 08002 Barcelona</div></body></html>')
    pages = {'/': city_html, '/barcelona/barcelona': listing}
    monkeypatch.setattr(official.requests, 'get', _fake_site(pages))
    locs, subs = official._crawl_for_city_stores(city_html, 'https://tiendas.example.es/', 'Barcelona')
    assert len(locs) >= 1  # heuristic fallback still works


def test_discover_links_with_text_keeps_generic_detail_anchors():
    # "Más detalles" must survive discovery here (it's the store-detail link).
    links = official._discover_links_with_text(_STORE_LIST_HTML, 'https://tiendas.example.es/barcelona/barcelona')
    hrefs = [u for u, t in links]
    assert any('tienda-uno' in u for u in hrefs)
    assert any('tienda-dos' in u for u in hrefs)


# ── extruct: microdata / RDFa LocalBusiness ─────────────────────────────

def test_schema_businesses_reads_microdata():
    # A LocalBusiness marked up as microdata (no JSON-LD) is now recognised.
    html = (
        '<div itemscope itemtype="http://schema.org/Store">'
        '<span itemprop="name">Tienda Uno</span>'
        '<span itemprop="telephone">933 50 12 40</span>'
        '<div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
        '<span itemprop="streetAddress">Carrer Gran 1</span>'
        '<span itemprop="addressLocality">Barcelona</span></div></div>')
    businesses = official._schema_businesses_from_html(html)
    assert any((b.get('name') == 'Tienda Uno') for b in businesses)


# ── Sitemap probe ───────────────────────────────────────────────────────

class _SM:
    def __init__(self, text, ok=True):
        self.text, self.ok, self.status_code = text, ok, 200 if ok else 404


def test_analyze_sitemap_finds_urls_via_robots(monkeypatch):
    monkeypatch.delenv('DISABLE_SITEMAP_FETCH', raising=False)
    sitemap_xml = ('<urlset><url><loc>https://x.es/tiendas/bcn-pelayo</loc></url>'
                   '<url><loc>https://x.es/</loc></url></urlset>')

    def fake_get(url, headers=None, timeout=None):
        if url.endswith('robots.txt'):
            return _SM('Sitemap: https://x.es/sitemap.xml')
        if url.endswith('sitemap.xml'):
            return _SM(sitemap_xml)
        return _SM('', ok=False)

    monkeypatch.setattr(official.requests, 'get', fake_get)
    result = official._analyze_sitemap('https://x.es/tiendas')
    assert result['present'] is True
    assert result['total_urls'] == 2
    assert result['store_like'] == 1  # only the 2-segment path counts


def test_analyze_sitemap_disabled_returns_absent(monkeypatch):
    monkeypatch.setenv('DISABLE_SITEMAP_FETCH', '1')
    assert official._analyze_sitemap('https://x.es')['present'] is False


# ── build_locator_report ────────────────────────────────────────────────

def test_build_locator_report_no_urls_is_no_data():
    report = official.build_locator_report([], [], [])
    assert report['has_data'] is False


def test_build_locator_report_flags_gaps(monkeypatch):
    monkeypatch.setenv('DISABLE_SITEMAP_FETCH', '1')  # sitemap absent
    site_analysis = [{'url': 'https://x.es', 'status': 'no_schema',
                      'location_count': 5, 'page_type': 'index'}]
    locations = [official.make_record('official', f'https://x.es#{i}', name=f'S{i}',
                                       formatted_address='X', raw={'_via': 'heuristic'})
                 for i in range(5)]
    report = official.build_locator_report(['https://x.es'], site_analysis, locations)
    by_key = {c['key']: c for c in report['checks']}
    assert by_key['jsonld_localbusiness']['status'] == 'bad'   # reachable, no schema
    assert by_key['sitemap_present']['status'] == 'bad'
    assert report['optimized'] is False


def test_build_locator_report_all_ok(monkeypatch):
    monkeypatch.delenv('DISABLE_SITEMAP_FETCH', raising=False)
    monkeypatch.setattr(official, '_analyze_sitemap',
                        lambda root: {'present': True, 'url': 's', 'total_urls': 3, 'store_like': 3})
    site_analysis = [{'url': 'https://x.es/tienda/1', 'status': 'found',
                      'location_count': 1, 'page_type': 'store_page'}]
    locations = [official.make_record(
        'official', 'https://x.es/tienda/1#s', name='S', formatted_address='X',
        phone='933501240', lat=41.0, lng=2.0,
        opening_hours=['Lunes: 09:00–20:00'], verify_url='https://x.es/tienda/1',
        raw={'_source_url': 'https://x.es/tienda/1'})]
    report = official.build_locator_report(['https://x.es'], site_analysis, locations)
    by_key = {c['key']: c for c in report['checks']}
    assert by_key['jsonld_localbusiness']['status'] == 'ok'
    assert by_key['schema_completeness']['status'] == 'ok'
    assert by_key['store_pages_coverage']['status'] == 'ok'  # 1 individual of 1
    assert report['optimized'] is True


def test_locator_report_not_analyzable_when_inaccessible(monkeypatch):
    monkeypatch.setenv('DISABLE_SITEMAP_FETCH', '1')
    site_analysis = [{'url': 'https://x.es', 'status': 'inaccessible', 'location_count': 0, 'page_type': 'store_page'}]
    report = official.build_locator_report(['https://x.es'], site_analysis, [])
    assert report['analyzable'] is False


def test_locator_report_analyzable_when_reachable(monkeypatch):
    monkeypatch.setenv('DISABLE_SITEMAP_FETCH', '1')
    site_analysis = [{'url': 'https://x.es', 'status': 'no_schema', 'location_count': 3, 'page_type': 'index'}]
    report = official.build_locator_report(['https://x.es'], site_analysis, [])
    assert report['analyzable'] is True
