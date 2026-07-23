import pricing


def test_estimate_max_shape_and_positive():
    est = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3)
    assert est['currency'] == 'EUR'
    assert est['maps_max'] > 0
    assert est['llm_delta'] > 0
    assert est['assumptions']['google_max'] == 25


def test_llm_delta_matches_cloro_price(monkeypatch):
    monkeypatch.setenv('PRICE_CLORO', '0.02')
    est = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3)
    assert est['llm_delta'] == round(5 * 3 * 0.02, 2)  # venues × runs × precio


def test_maps_scales_with_google_max(monkeypatch):
    small = pricing.estimate_max(google_max=5, reviews_pages=3, cloro_venues=5, cloro_runs=3)
    big = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3)
    assert big['maps_max'] > small['maps_max']


def test_fewer_review_pages_is_cheaper():
    p3 = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3)
    p5 = pricing.estimate_max(google_max=25, reviews_pages=5, cloro_venues=5, cloro_runs=3)
    assert p3['maps_max'] < p5['maps_max']


def test_prices_configurable_by_env(monkeypatch):
    monkeypatch.setenv('PRICE_SERPAPI', '1.00')
    est = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3,
                               action_links_venues=5)
    # SerpApi: 25×3 reseñas + 5 action links (solo peores) + 25 apple = 105 búsquedas × 1.00
    assert est['prices']['serpapi'] == 1.00
    assert est['maps_max'] >= 105
    assert est['assumptions']['action_links_venues'] == 5


def test_action_links_capped_reduces_cost(monkeypatch):
    monkeypatch.setenv('PRICE_SERPAPI', '1.00')
    few = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3,
                               action_links_venues=5)
    many = pricing.estimate_max(google_max=25, reviews_pages=3, cloro_venues=5, cloro_runs=3,
                                action_links_venues=25)
    assert few['maps_max'] < many['maps_max']  # 20 llamadas de links menos
