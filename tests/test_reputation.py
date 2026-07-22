import matching
import normalize
import reputation


def _google_cluster(rating=None, review_count=None, reviews=None):
    record = normalize.make_record('google', 'g1', name='Foo', formatted_address='X',
                                    rating=rating, review_count=review_count,
                                    raw={'reviews': reviews or []})
    return matching.cluster_records([record])[0]


def test_no_google_presence_is_auto_critical():
    apple_record = normalize.make_record('apple', 'a1', name='Foo', formatted_address='X')
    cluster = matching.cluster_records([apple_record])[0]
    reputation.compute_reputation([cluster])
    assert cluster['reputation']['score'] == 100
    assert cluster['reputation']['flags'][0]['code'] == 'NO_GOOGLE_PRESENCE'


def test_no_reviews_flag():
    cluster = _google_cluster(rating=None, review_count=0)
    reputation.compute_reputation([cluster])
    codes = [f['code'] for f in cluster['reputation']['flags']]
    assert 'NO_REVIEWS' in codes


def test_low_rating_flag():
    cluster = _google_cluster(rating=2.5, review_count=50)
    reputation.compute_reputation([cluster])
    codes = [f['code'] for f in cluster['reputation']['flags']]
    assert 'LOW_RATING' in codes
    assert cluster['reputation']['score'] >= 40


def test_high_rating_no_low_rating_flag():
    cluster = _google_cluster(rating=4.8, review_count=500)
    reputation.compute_reputation([cluster])
    codes = [f['code'] for f in cluster['reputation']['flags']]
    assert 'LOW_RATING' not in codes


def test_negative_sample_flag():
    reviews = [{'rating': 1, 'text': 'malo'}, {'rating': 5, 'text': 'bien'}]
    cluster = _google_cluster(rating=4.0, review_count=100, reviews=reviews)
    reputation.compute_reputation([cluster])
    codes = [f['code'] for f in cluster['reputation']['flags']]
    assert 'NEGATIVE_RECENT_SAMPLE' in codes
    assert len(cluster['reputation']['negative_samples']) == 1


def test_few_reviews_relative_needs_at_least_three_datapoints():
    clusters = [_google_cluster(rating=4.5, review_count=500),
                _google_cluster(rating=4.5, review_count=480)]
    reputation.compute_reputation(clusters)
    for c in clusters:
        codes = [f['code'] for f in c['reputation']['flags']]
        assert 'FEW_REVIEWS_RELATIVE' not in codes


def test_few_reviews_relative_triggers_with_enough_datapoints():
    clusters = [_google_cluster(rating=4.5, review_count=500),
                _google_cluster(rating=4.5, review_count=480),
                _google_cluster(rating=4.5, review_count=520),
                _google_cluster(rating=4.5, review_count=10)]
    reputation.compute_reputation(clusters)
    codes = [f['code'] for f in clusters[-1]['reputation']['flags']]
    assert 'FEW_REVIEWS_RELATIVE' in codes
