import normalize
import venue_metrics


def _cluster(by_source, reputation=None, canonical_label='Foo', canonical_address=None):
    return {
        'sources_present': sorted(by_source.keys()),
        'by_source': by_source,
        'reputation': reputation or {},
        'canonical_label': canonical_label,
        'canonical_address': canonical_address,
    }


def _rec(source, name='Foo', address='X', phone=None, website=None, opening_hours=None):
    return normalize.make_record(source, f'{source}-id', name=name, formatted_address=address,
                                  phone=phone, website=website, opening_hours=opening_hours)


def _compute(cluster, has_official_data=False, city='Barcelona'):
    venue_metrics.compute_venue_metrics([cluster], has_official_data, city)
    return cluster['venue_metrics']


# ── presence_pct ─────────────────────────────────────────────────────

def test_presence_pct_without_official_data_has_three_source_denominator():
    cluster = _cluster({'google': _rec('google'), 'apple': _rec('apple')})
    m = _compute(cluster, has_official_data=False)
    # present in 2 of 3 checked sources (google, apple, azure) -> 67%, not
    # penalized for a store-locator that was never provided this audit.
    assert m['presence_pct'] == 67


def test_presence_pct_with_official_data_has_four_source_denominator():
    cluster = _cluster({'google': _rec('google'), 'apple': _rec('apple')})
    m = _compute(cluster, has_official_data=True)
    assert m['presence_pct'] == 50


def test_presence_pct_full_coverage_is_100():
    cluster = _cluster({s: _rec(s) for s in ('google', 'apple', 'azure')})
    m = _compute(cluster, has_official_data=False)
    assert m['presence_pct'] == 100


# ── presence_detail: verify links for present sources, search links for absent ──

def test_presence_detail_present_source_carries_its_verify_url():
    google = normalize.from_google({'place_id': 'g1', 'name': 'Foo', 'formatted_address': 'X'})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['presence_detail']['google']['present'] is True
    assert 'query_place_id=g1' in m['presence_detail']['google']['url']


def test_presence_detail_missing_map_source_gets_a_search_url_with_full_address():
    cluster = _cluster({'google': _rec('google')}, canonical_label='Foo Pelayo',
                        canonical_address='Carrer de Pelayo 12, Barcelona')
    m = _compute(cluster, has_official_data=False, city='Barcelona')
    apple_detail = m['presence_detail']['apple']
    assert apple_detail['present'] is False
    assert apple_detail['url'] == normalize.apple_search_url('Foo Pelayo Carrer de Pelayo 12, Barcelona')
    azure_detail = m['presence_detail']['azure']
    assert azure_detail['present'] is False
    assert azure_detail['url'] == normalize.bing_search_url('Foo Pelayo Carrer de Pelayo 12, Barcelona')


def test_presence_detail_missing_map_source_falls_back_to_city_without_address():
    cluster = _cluster({'google': _rec('google')}, canonical_label='Foo Pelayo', canonical_address=None)
    m = _compute(cluster, has_official_data=False, city='Barcelona')
    apple_detail = m['presence_detail']['apple']
    assert apple_detail['url'] == normalize.apple_search_url('Foo Pelayo Barcelona')


def test_presence_detail_official_has_no_search_url_when_absent():
    cluster = _cluster({'google': _rec('google')})
    m = _compute(cluster, has_official_data=True)  # official checked this audit, but absent from this cluster
    assert m['presence_detail']['official'] == {'present': False, 'url': None}


def test_presence_detail_excludes_official_entirely_when_not_checked():
    cluster = _cluster({'google': _rec('google')})
    m = _compute(cluster, has_official_data=False)
    assert 'official' not in m['presence_detail']


# ── accuracy: equal-share scoring across comparison platforms only ──────
# Google is the anchor/base — it doesn't get its own bucket or count toward
# the denominator. Each of apple/azure/official is worth an equal share of
# 100% (33.33% each when all 3 are checked, 50% each if official wasn't —
# same adaptive-denominator principle as presence_pct).

def test_accuracy_100_when_all_checked_platforms_match():
    cluster = _cluster({
        'google': _rec('google', phone='932000000'),
        'apple': _rec('apple', phone='932000000'),
        'azure': _rec('azure', phone='932000000'),
        'official': _rec('official', phone='932000000'),
    })
    m = _compute(cluster, has_official_data=True)
    assert m['accuracy_phone']['avg'] == 100


def test_accuracy_each_of_three_comparison_platforms_is_one_third():
    cluster = _cluster({
        'google': _rec('google', phone='932000000'),
        'apple': _rec('apple', phone='932000000'),    # matches
        'azure': _rec('azure', phone='932999999'),    # conflicts -> 0
        'official': _rec('official', phone='932000000'),
    })
    m = _compute(cluster, has_official_data=True)
    # 3 comparison platforms checked (apple/azure/official — NOT google),
    # 1 of them fails -> 2/3 -> 67% (each platform is worth ~33.33%).
    assert m['accuracy_phone']['avg'] == 67
    assert m['accuracy_phone']['anchor_value'] == '932000000'
    assert m['accuracy_phone']['breakdown']['apple'] == {'verdict': 'match', 'score': 100, 'value': '932000000'}
    assert m['accuracy_phone']['breakdown']['azure'] == {'verdict': 'conflict', 'score': 0, 'value': '932999999'}


def test_accuracy_each_platform_is_one_half_when_official_not_checked():
    # Same shape as above but with no official data supplied at all —
    # 'official' is excluded from the average entirely (not a failure), so
    # only apple/azure count and each is worth 50%.
    cluster = _cluster({
        'google': _rec('google', phone='932000000'),
        'apple': _rec('apple', phone='932000000'),
        'azure': _rec('azure', phone='932999999'),
    })
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_phone']['avg'] == 50
    assert m['accuracy_phone']['breakdown']['official'] == {'verdict': 'na', 'score': None, 'value': None}


def test_accuracy_platform_missing_entirely_counts_the_same_as_a_conflict():
    with_conflict = _cluster({
        'google': _rec('google', phone='932000000'),
        'apple': _rec('apple', phone='932999999'),  # conflict
    })
    without_apple = _cluster({
        'google': _rec('google', phone='932000000'),
        # no apple record at all -> 'missing'
    })
    m1 = _compute(with_conflict, has_official_data=False)
    m2 = _compute(without_apple, has_official_data=False)
    assert m2['accuracy_phone']['breakdown']['apple'] == {'verdict': 'missing', 'score': 0, 'value': None}
    # Both cases: apple fails (conflict or missing), azure is also absent
    # ('missing') in both clusters -> 0/2 -> 0% either way.
    assert m1['accuracy_phone']['avg'] == m2['accuracy_phone']['avg'] == 0


def test_accuracy_website_matches_on_normalized_host():
    cluster = _cluster({
        'google': _rec('google', website='https://www.foo.com/es'),
        'apple': _rec('apple', website='http://foo.com/en/store'),  # same host, different path -> match
    })
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_website']['breakdown']['apple']['verdict'] == 'match'


def test_accuracy_is_zero_when_google_itself_has_no_value_to_compare_against():
    # Google lacking a value doesn't cost points directly (it's not scored
    # itself anymore) — but it means nothing CAN be confirmed as matching,
    # so every comparison platform ends up 'sin_dato'/'missing' and the
    # average is still 0.
    cluster = _cluster({'google': _rec('google', phone=None), 'apple': _rec('apple', phone=None)})
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_phone']['anchor_value'] is None
    assert m['accuracy_phone']['breakdown']['apple'] == {'verdict': 'sin_dato', 'score': 0, 'value': None}
    assert m['accuracy_phone']['avg'] == 0


def test_accuracy_breakdown_carries_the_display_value_regardless_of_verdict():
    # phone_display keeps the raw (non-digits-only) string — kept in the
    # data model for completeness/debugging even though the popover no
    # longer surfaces it directly (shows score + link instead, per feedback
    # that raw values in the popover were noisy).
    cluster = _cluster({
        'google': _rec('google', phone='93 200 00 00'),
        'apple': _rec('apple', phone='93 299 99 99'),
    })
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_phone']['anchor_value'] == '93 200 00 00'
    assert m['accuracy_phone']['breakdown']['apple']['value'] == '93 299 99 99'


def test_accuracy_hours_scores_official_match_and_azure_conflict():
    hours_ok = ['Lunes: 10:00–22:00']
    hours_off = ['Lunes: 10:00–20:00']  # 120min drift -> flagged -> counts as disagreement
    cluster = _cluster({
        'google': _rec('google', opening_hours=hours_ok),
        'official': _rec('official', opening_hours=hours_ok),  # matches
        'azure': _rec('azure', opening_hours=hours_off),       # disagrees
    })
    m = _compute(cluster, has_official_data=True)
    # 3 comparison platforms: official (match) succeeds, azure (conflict) +
    # apple ('missing', no record in this cluster at all) fail -> 1/3 -> 33%.
    assert m['accuracy_hours']['avg'] == 33
    assert m['accuracy_hours']['breakdown']['official']['verdict'] == 'match'
    assert m['accuracy_hours']['breakdown']['azure']['verdict'] == 'conflict'


def test_accuracy_hours_is_sin_dato_for_apple_since_its_api_never_returns_hours():
    # Apple's Maps Server API has no hours field anywhere — from_apple never
    # sets opening_hours, so this is always 'sin_dato' for apple, regardless
    # of what other sources have. Azure, by contrast, is now requested with
    # openingHours=nextSevenDays (see app.py) and CAN carry real hours — a
    # record simply lacking them (e.g. outside Azure's coverage) still shows
    # 'sin_dato' too, but that's per-record, not a hard API limitation.
    cluster = _cluster({
        'google': _rec('google', opening_hours=['Lunes: 10:00–22:00']),
        'apple': _rec('apple'),
        'azure': _rec('azure'),
    })
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_hours']['breakdown']['apple']['verdict'] == 'sin_dato'
    assert m['accuracy_hours']['breakdown']['azure']['verdict'] == 'sin_dato'


def test_accuracy_hours_compares_azure_when_its_record_carries_hours():
    cluster = _cluster({
        'google': _rec('google', opening_hours=['Lunes: 10:00–22:00']),
        'azure': _rec('azure', opening_hours=['Lunes: 10:00–22:00']),
    })
    m = _compute(cluster, has_official_data=False)
    assert m['accuracy_hours']['breakdown']['azure'] == {'verdict': 'match', 'score': 100, 'value': 'Lunes: 10:00–22:00'}
    # 2 comparison platforms checked (official not audited): azure matches,
    # apple ('missing', not in this cluster at all) fails -> 1/2 -> 50%.
    assert m['accuracy_hours']['avg'] == 50


# ── accuracy: name ───────────────────────────────────────────────────────

def test_accuracy_name_conflict_costs_its_equal_share():
    cluster = _cluster({
        'google': _rec('google', name='McDonald\'s'),
        'apple': _rec('apple', name='McDonald\'s'),
        'azure': _rec('azure', name='McDonald\'s'),
        'official': _rec('official', name='Totally Different Brand Name'),  # conflicts
    })
    m = _compute(cluster, has_official_data=True)
    # 3 comparison platforms, 1 conflicts -> 2/3 -> 67%.
    assert m['accuracy_name']['avg'] == 67
    assert m['accuracy_name']['breakdown']['official']['verdict'] == 'conflict'


# ── venue_score ordering invariant ──────────────────────────────────────

def test_google_matched_venues_always_sort_ahead_of_venues_with_no_google_match():
    # A Bing-only venue is almost certainly a stale/closed duplicate lingering
    # on a less-maintained source, not a real gap worth a rep's attention —
    # it must never outrank a real (Google-matched) venue, even one with a
    # much worse venue_score (missing everything else, terrible rating).
    no_google_but_otherwise_perfect = _cluster(
        {'azure': _rec('azure', name='Foo', phone='932000000')},
        reputation={'rating': 5.0, 'review_count': 500},
    )
    google_matched_but_worst_possible = _cluster(
        {'google': _rec('google', name='Foo', phone='932000000')},
        reputation={'rating': 1.0, 'review_count': 0},
    )
    clusters = venue_metrics.compute_venue_metrics(
        [no_google_but_otherwise_perfect, google_matched_but_worst_possible], has_official_data=False, city='Barcelona')
    assert clusters == [google_matched_but_worst_possible, no_google_but_otherwise_perfect]


def test_full_presence_and_accuracy_always_outranks_missing_source_regardless_of_rating():
    complete = _cluster(
        {'google': _rec('google'), 'apple': _rec('apple'), 'azure': _rec('azure')},
        reputation={'rating': 3.0, 'review_count': 5},  # mediocre rating
    )
    missing_apple = _cluster(
        {'google': _rec('google'), 'azure': _rec('azure')},
        reputation={'rating': 5.0, 'review_count': 500},  # perfect rating
    )
    clusters = venue_metrics.compute_venue_metrics([complete, missing_apple], has_official_data=False, city='Barcelona')
    # sorted worst-first: the one missing a source must sort before the
    # complete one, no matter how good its rating is.
    assert clusters[0] is missing_apple
    assert clusters[1] is complete


def test_compute_venue_metrics_sorts_clusters_ascending_by_score():
    worst = _cluster({'google': _rec('google')})
    best = _cluster({s: _rec(s) for s in ('google', 'apple', 'azure')})
    clusters = venue_metrics.compute_venue_metrics([best, worst], has_official_data=False, city='Barcelona')
    assert clusters == [worst, best]


# ── N/D placeholders ──────────────────────────────────────────────────

def test_unavailable_metrics_are_reported_as_nd_with_a_reason():
    # With no scraped data attached, the scraper-backed fields
    # (reply_rate_3m/action_links_google/posts_3m) also report N/D, alongside
    # the permanently-unavailable API/OAuth-gated ones.
    cluster = _cluster({'google': _rec('google')})
    m = _compute(cluster, has_official_data=False)
    for field in ('action_links_google', 'action_links_apple', 'reply_rate_3m',
                  'posts_3m', 'products_3m'):
        assert m[field]['value'] == 'N/D'
        assert m[field]['reason']


def test_review_rate_is_none_without_google_reviews():
    cluster = _cluster({'google': _rec('google')})
    m = _compute(cluster, has_official_data=False)
    assert m['review_rate_3m'] is None


def test_review_rate_counts_recent_reviews_from_google_sample():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'reviews': [{'time': 1000}, {'time': 1000 - 200 * 24 * 60 * 60}]})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['review_rate_3m'] == {'value': 1, 'sample_size': 2, 'approx': True, 'source': 'api_sample'}


# ── scraped review/reply metrics (google_reviews_scraper.py) ────────────

def test_scraped_reviews_override_the_api_sample_for_review_rate():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'reviews': [{'time': 1000}], 'scraped_reviews': [
            {'has_owner_reply': False}, {'has_owner_reply': False}, {'has_owner_reply': False},
        ]})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['review_rate_3m'] == {'value': 3, 'sample_size': 3, 'approx': True, 'source': 'scraped'}


def test_reply_rate_computed_from_scraped_owner_replies():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_reviews': [
            {'has_owner_reply': True}, {'has_owner_reply': True}, {'has_owner_reply': False},
        ]})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['reply_rate_3m'] == {'value': 67, 'sample_size': 3, 'approx': True, 'source': 'scraped'}


def test_reply_rate_is_nd_when_scraping_returned_nothing():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_reviews': []})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['reply_rate_3m']['value'] == 'N/D'


# ── scraped action links / Posts (google_reviews_scraper.py) ────────────

def test_action_links_from_scraped_data():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_action_links': [
            {'type': 'reservation', 'label': 'Reservar una mesa'},
            {'type': 'menu', 'label': 'Ver el menú'},
        ]})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['action_links_google']['source'] == 'scraped'
    assert m['action_links_google']['value'] == 'Menú, Reservar'  # sorted, deduped by type label
    assert len(m['action_links_google']['links']) == 2


def test_action_links_none_detected_is_distinct_from_nd():
    # Scrape ran and found no action links — a checked, real "none", not the
    # "couldn't check" N/D you get when scraping wasn't attempted at all.
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_action_links': []})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['action_links_google']['value'] == 'Ninguno detectado'
    assert m['action_links_google']['source'] == 'scraped'


def test_action_links_nd_when_scraping_not_attempted():
    cluster = _cluster({'google': _rec('google')})  # no scraped_action_links key at all
    m = _compute(cluster, has_official_data=False)
    assert m['action_links_google']['value'] == 'N/D'
    assert m['action_links_google']['reason']


def test_posts_count_from_scraped_data():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_posts': [
            {'text': 'Post 1', 'time': 1000}, {'text': 'Post 2', 'time': 2000},
        ]})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['posts_3m'] == {'value': 2, 'posts': [
        {'text': 'Post 1', 'time': 1000}, {'text': 'Post 2', 'time': 2000},
    ], 'approx': True, 'source': 'scraped'}


def test_posts_zero_is_distinct_from_nd():
    google = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
        raw={'scraped_posts': []})
    cluster = _cluster({'google': google})
    m = _compute(cluster, has_official_data=False)
    assert m['posts_3m']['value'] == 0
    assert m['posts_3m']['source'] == 'scraped'


def test_posts_nd_when_scraping_not_attempted():
    cluster = _cluster({'google': _rec('google')})  # no scraped_posts key at all
    m = _compute(cluster, has_official_data=False)
    assert m['posts_3m']['value'] == 'N/D'
    assert m['posts_3m']['reason']


# ── rating / review_count stay separate fields ──────────────────────────

def test_rating_and_review_count_are_separate_fields():
    cluster = _cluster({'google': _rec('google')}, reputation={'rating': 4.2, 'review_count': 87})
    m = _compute(cluster, has_official_data=False)
    assert m['rating'] == 4.2
    assert m['review_count'] == 87
