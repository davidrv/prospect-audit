"""Reputation signals per matched location, from Google-only snapshot data
(rating, review count, up to 5 sample reviews). No historical trend in v1 —
see docs/plan.md backlog.
"""

RATING_CRITICAL = 3.5
RATING_HIGH = 4.0
RATING_VERY_HIGH = 4.3
FEW_REVIEWS_RATIO = 0.2
NEGATIVE_REVIEW_THRESHOLD = 2
NEGATIVE_REVIEW_WEIGHT = 15
NEGATIVE_REVIEW_CAP = 30


def compute_reputation(clusters):
    """Adds a 'reputation' dict to each cluster in place."""
    review_counts = [
        c['by_source']['google'].get('review_count')
        for c in clusters
        if 'google' in c['by_source'] and c['by_source']['google'].get('review_count')
    ]
    median_reviews = _median(review_counts) if len(review_counts) >= 3 else None

    for cluster in clusters:
        cluster['reputation'] = _reputation_for_cluster(cluster, median_reviews)
    return clusters


def _reputation_for_cluster(cluster, median_reviews):
    google = cluster['by_source'].get('google')

    if google is None:
        flags = []
        score = 0
        if cluster['sources_present']:
            flags.append({'code': 'NO_GOOGLE_PRESENCE', 'weight': 100,
                          'message': 'No tiene ficha en Google — no se puede encontrar ni dejar reseñas ahí.'})
            score = 100
        return {'score': score, 'flags': flags, 'rating': None, 'review_count': None,
                'sample_reviews': [], 'negative_samples': [], 'ai_summary': None}

    rating = google.get('rating')
    review_count = google.get('review_count')
    sample_reviews = (google.get('raw') or {}).get('reviews') or []
    ai_summary = (google.get('raw') or {}).get('review_summary')

    flags, score = [], 0

    if not review_count:
        flags.append({'code': 'NO_REVIEWS', 'weight': 35, 'message': 'Sin ninguna reseña en Google.'})
        score += 35

    if rating is not None:
        weight = _rating_weight(rating)
        if weight:
            flags.append({'code': 'LOW_RATING', 'weight': weight, 'message': f'Rating de {rating} en Google.'})
            score += weight

    if median_reviews and review_count and review_count < median_reviews * FEW_REVIEWS_RATIO:
        gap_ratio = review_count / (median_reviews * FEW_REVIEWS_RATIO)
        weight = min(20, round(20 * (1 - gap_ratio)))
        flags.append({'code': 'FEW_REVIEWS_RELATIVE', 'weight': weight,
                      'message': f'Solo {review_count} reseñas, muy por debajo de la mediana de la '
                                 f'cadena ({median_reviews:.0f}).'})
        score += weight

    negative = [r for r in sample_reviews if (r.get('rating') or 5) <= NEGATIVE_REVIEW_THRESHOLD]
    if negative:
        weight = min(NEGATIVE_REVIEW_CAP, NEGATIVE_REVIEW_WEIGHT * len(negative))
        flags.append({'code': 'NEGATIVE_RECENT_SAMPLE', 'weight': weight,
                      'message': f'{len(negative)} reseña(s) negativa(s) recientes en la muestra.'})
        score += weight

    return {
        'score': min(100, score),
        'flags': flags,
        'rating': rating,
        'review_count': review_count,
        'sample_reviews': sample_reviews,
        'negative_samples': negative,
        'ai_summary': ai_summary,
    }


def _rating_weight(rating):
    if rating < RATING_CRITICAL:
        return 40
    if rating < RATING_HIGH:
        return 25
    if rating < RATING_VERY_HIGH:
        return 10
    return 0


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
