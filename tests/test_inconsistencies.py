import inconsistencies
import matching
import normalize


def _rec(source, name=None, address=None, lat=None, lng=None, phone=None, website=None,
         opening_hours=None, verify_url=None):
    return normalize.make_record(source, f'{source}-id', name=name, formatted_address=address,
                                  lat=lat, lng=lng, phone=phone, website=website,
                                  opening_hours=opening_hours, verify_url=verify_url)


def _cluster(records):
    return matching.cluster_records(records)[0]


def test_other_source_without_google_flags_r1():
    # Apple/Azure/Official present, but no Google match at all — critical,
    # since Google is the anchor everything else is checked against.
    a = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0)
    b = _rec('azure', name='Foo', address='X', lat=1.0, lng=1.0)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R1' for f in flags)


def test_google_only_produces_no_finding():
    # A Google-only location isn't inherently a finding on its own — flagging
    # it would just reintroduce platform-vs-platform-style noise when no
    # other source was even checked.
    cluster = _cluster([_rec('google', name='Foo', address='X')])
    flags = inconsistencies._flags_for_cluster(cluster)
    assert flags == []


def test_google_plus_one_other_missing_rest_flags_r11():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    r11 = [f for f in flags if f['rule'] == 'R11']
    assert r11 and {'azure', 'official'} <= set(r11[0]['sources'])


def test_google_plus_all_others_no_coverage_flag():
    google = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    apple = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    azure = _rec('azure', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    official = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    flags = inconsistencies._flags_for_cluster(_cluster([google, apple, azure, official]))
    assert not any(f['rule'] in ('R1', 'R11') for f in flags)


def test_matching_phone_no_conflict():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert not any(f['rule'] in ('R4', 'R5') for f in flags)


def test_conflicting_phone_vs_google_flags_r5():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, phone='934999999')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R5' for f in flags)


def test_missing_phone_vs_google_flags_r4():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, phone=None)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R4' for f in flags)


def test_apple_vs_azure_would_not_be_compared_directly():
    # Both apple and azure are compared to google, never to each other — so
    # with no google match at all, there's no phone-conflict rule triggered,
    # just the R1 "no google match" finding.
    a = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('azure', name='Foo', address='X', lat=1.0, lng=1.0, phone='934999999')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert not any(f['rule'] in ('R4', 'R5', 'R6', 'R7', 'R9') for f in flags)
    assert any(f['rule'] == 'R1' for f in flags)


def test_hours_contradiction_vs_google_flags_r14b():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Sunday: Closed'])
    b = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Domingo: 10:00–20:00'])
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R14b' for f in flags)


def test_hours_small_diff_vs_google_flags_r14_minor():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Monday: 9:00 AM – 10:00 PM'])
    b = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Lunes: 09:00–22:45'])
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    r14 = [f for f in flags if f['rule'] == 'R14']
    assert r14 and r14[0]['severity'] == 'minor'


def test_hours_matching_no_flag():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Monday: 9:00 AM – 10:00 PM'])
    b = _rec('official', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Lunes: 09:00–22:00'])
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert not any(f['rule'] in ('R14', 'R14b') for f in flags)


def test_apple_has_no_hours_support_so_no_flag():
    # Apple's Maps Server API has no opening-hours field anywhere (confirmed
    # against Apple's own Place object reference) — even if a hypothetical
    # record carried hours, FIELD_SUPPORT deliberately excludes 'apple' so
    # this never gets compared.
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Domingo: 10:00–20:00'])
    b = _rec('apple', name='Foo', address='X', lat=1.0, lng=1.0)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert not any(f['rule'] in ('R14', 'R14b') for f in flags)


def test_azure_hours_are_compared_when_present():
    # Unlike Apple, Azure DOES support hours (opted in via
    # openingHours=nextSevenDays in app.py's _search_azure) — once a record
    # actually carries opening_hours, it's compared like any other source.
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Domingo: 10:00–20:00'])
    b = _rec('azure', name='Foo', address='X', lat=1.0, lng=1.0, opening_hours=['Domingo: Closed'])
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R14b' for f in flags)


def test_name_variation_vs_google_flags_r9():
    a = _rec('google', name='Zara', address='X', lat=1.0, lng=1.0)
    b = _rec('official', name='Zara Pelayo', address='X', lat=1.0, lng=1.0)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert any(f['rule'] == 'R9' for f in flags)


def test_name_case_difference_alone_does_not_flag_r9():
    # rapidfuzz's ratio is case-sensitive ("ZARA" vs "Zara" scores 25, looks
    # "critical") — comparison must use name_norm so pure capitalization
    # differences (very common: Google often returns ALL-CAPS names) aren't
    # reported as a critical name mismatch.
    a = _rec('google', name='ZARA', address='X', lat=1.0, lng=1.0)
    b = _rec('apple', name='Zara', address='X', lat=1.0, lng=1.0)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    assert not any(f['rule'] == 'R9' for f in flags)


def test_ambiguous_match_flags_r3():
    a = _rec('google', name='Foo A', address='X', lat=41.0, lng=2.0)
    b = _rec('google', name='Foo B', address='Y', lat=41.0, lng=2.0)
    c = _rec('official', name='Foo', address='Z', lat=41.0, lng=2.0)
    flags = inconsistencies._flags_for_cluster(_cluster([a, b, c]))
    assert any(f['rule'] == 'R3' for f in flags)


def test_conflicting_phone_flag_carries_verify_links():
    a = _rec('google', name='Foo', address='Calle Google 1', lat=1.0, lng=1.0, phone='932123456',
             verify_url='https://maps.google.com/?q=foo')
    b = _rec('official', name='Foo', address='Calle Oficial 1', lat=1.0, lng=1.0, phone='934999999',
             verify_url='https://example.com/official-page')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    r5 = next(f for f in flags if f['rule'] == 'R5')

    links_by_source = {l['source']: l for l in r5['links']}
    assert links_by_source['official']['url'] == 'https://example.com/official-page'
    assert links_by_source['official']['address'] == 'Calle Oficial 1'
    assert links_by_source['google']['url'] == 'https://maps.google.com/?q=foo'
    assert links_by_source['google']['address'] == 'Calle Google 1'


def test_flag_links_are_none_when_verify_url_missing():
    a = _rec('google', name='Foo', address='X', lat=1.0, lng=1.0, phone='932123456')
    b = _rec('official', name='Foo', address='Y', lat=1.0, lng=1.0, phone='934999999')
    flags = inconsistencies._flags_for_cluster(_cluster([a, b]))
    r5 = next(f for f in flags if f['rule'] == 'R5')
    assert all(l['url'] is None for l in r5['links'])


def test_detect_inconsistencies_sets_flags_on_all_clusters():
    clusters = matching.cluster_records([_rec('official', name='Foo', address='X')])
    inconsistencies.detect_inconsistencies(clusters)
    assert 'flags' in clusters[0]
