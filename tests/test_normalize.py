import normalize


def test_name_norm_strips_accents_and_punctuation():
    assert normalize.name_norm("McDonald's - Pelayo") == 'mcdonald s pelayo'


def test_name_norm_empty():
    assert normalize.name_norm(None) == ''
    assert normalize.name_norm('') == ''


def test_address_norm_expands_abbreviations():
    assert normalize.address_norm('C/ Pelai, 62') == 'calle pelai 62'


def test_phone_norm_adds_country_code():
    assert normalize.phone_norm('932 12 34 56') == '34932123456'


def test_phone_norm_handles_00_prefix():
    assert normalize.phone_norm('0034 932123456') == '34932123456'


def test_phone_norm_none_or_empty():
    assert normalize.phone_norm(None) is None
    assert normalize.phone_norm('') is None


def test_website_norm_strips_protocol_and_www():
    assert normalize.website_norm('https://www.Mcdonalds.es/pelayo') == 'mcdonalds.es'


def test_website_norm_none():
    assert normalize.website_norm(None) is None


def test_make_record_shape():
    r = normalize.make_record('google', 'abc', name='Foo', formatted_address='Calle Bar 1',
                               lat=1.0, lng=2.0, phone='932123456', website='foo.com')
    assert r['source'] == 'google'
    assert r['name_norm'] == 'foo'
    assert r['phone'] == '34932123456'
    assert r['phone_display'] == '932123456'
    assert r['website'] == 'foo.com'


def test_from_google_maps_fields():
    place = {
        'place_id': 'g1', 'name': "McDonald's", 'formatted_address': 'Calle Pelai 62',
        'formatted_phone_number': '932123456', 'website': 'https://mcdonalds.es',
        'rating': 4.2, 'user_ratings_total': 300,
        'geometry': {'location': {'lat': 41.38, 'lng': 2.17}},
        'opening_hours': {'weekday_text': ['Lunes: 09:00–22:00']},
    }
    r = normalize.from_google(place)
    assert r['source'] == 'google'
    assert r['lat'] == 41.38 and r['lng'] == 2.17
    assert r['rating'] == 4.2
    assert r['opening_hours'] == ['Lunes: 09:00–22:00']


def test_from_apple_and_azure_share_shape():
    item = {'id': 'a1', 'name': 'Foo', 'formatted_address': 'Bar', 'phone_number': '932123456',
            'url': 'foo.com', 'category': 'Restaurant', 'lat': 1.0, 'lng': 2.0}
    a = normalize.from_apple(item)
    z = normalize.from_azure(item)
    assert a['source'] == 'apple' and z['source'] == 'azure'
    assert a['phone'] == z['phone'] == '34932123456'


def test_parse_hours_cross_language_days():
    schedule = normalize.parse_hours(['Monday: 9:00 AM – 10:00 PM', 'Domingo: Cerrado'])
    assert schedule[0] == [(540, 1320)]
    assert schedule[6] == 'closed'


def test_parse_hours_empty():
    assert normalize.parse_hours(None) == {}
    assert normalize.parse_hours([]) == {}


def test_parse_hours_unrecognized_day_skipped():
    assert normalize.parse_hours(['Not a day: whatever']) == {}


def test_google_maps_url_uses_place_id():
    url = normalize.google_maps_url('abc123', "McDonald's")
    assert 'query_place_id=abc123' in url
    assert 'query=' in url


def test_google_maps_url_none_without_place_id():
    assert normalize.google_maps_url(None, 'Foo') is None


def test_apple_maps_url_needs_coordinates():
    assert normalize.apple_maps_url(None, None, 'Foo') is None
    url = normalize.apple_maps_url(41.38, 2.17, 'Foo')
    assert 'll=41.38,2.17' in url


def test_bing_maps_url_needs_coordinates():
    assert normalize.bing_maps_url(None, None, 'Foo') is None
    url = normalize.bing_maps_url(41.38, 2.17, 'Foo')
    # Must use the /maps/search path with a real q= — confirmed live that
    # plain /maps?sp=point... (no q=, no /search) never opens a place card,
    # Bing just falls back to some unrelated default map center. sp=/cp= are
    # still included so the pin lands exactly on the known point.
    assert url.startswith('https://www.bing.com/maps/search?')
    assert 'q=Foo' in url
    assert 'sp=point.41.38_2.17_Foo' in url
    assert 'cp=41.38~2.17' in url


def test_bing_search_url_uses_the_search_path():
    # Confirmed live: plain /maps?q=... (no /search) never resolves a place;
    # /maps/search?q=... correctly shows a result or an empty/irrelevant list.
    url = normalize.bing_search_url('Foo Barcelona')
    assert url == 'https://www.bing.com/maps/search?q=Foo%20Barcelona'


def test_apple_search_url_without_coordinates_falls_back_to_plain_query():
    url = normalize.apple_search_url('Foo Pelayo 12, Barcelona')
    assert url == 'https://maps.apple.com/?q=Foo%20Pelayo%2012%2C%20Barcelona'


def test_apple_search_url_with_coordinates_anchors_and_drops_the_address():
    # Confirmed live: an unanchored name+address query can get its address
    # text mis-parsed by Apple's own fallback geocoder when the business
    # name itself contains a street-like fragment. Anchoring on ll= and
    # searching by name only avoids that.
    url = normalize.apple_search_url('Foo Pelayo 12, Barcelona', lat=41.38, lng=2.17, name='Foo Pelayo')
    assert url == 'https://maps.apple.com/?ll=41.38,2.17&q=Foo%20Pelayo'


def test_from_google_sets_verify_url():
    place = {'place_id': 'abc123', 'name': 'Foo', 'formatted_address': 'X'}
    r = normalize.from_google(place)
    assert 'query_place_id=abc123' in r['verify_url']


def test_from_apple_sets_verify_url_from_coordinates():
    item = {'id': 'a1', 'name': 'Foo', 'lat': 41.38, 'lng': 2.17}
    r = normalize.from_apple(item)
    assert 'maps.apple.com' in r['verify_url']


def test_from_azure_sets_verify_url_from_coordinates():
    item = {'id': 'z1', 'name': 'Foo', 'lat': 41.38, 'lng': 2.17}
    r = normalize.from_azure(item)
    assert 'bing.com/maps' in r['verify_url']
