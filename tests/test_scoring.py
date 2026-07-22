import scoring


# ── venue_score ─────────────────────────────────────────────────────

def test_venue_score_all_components_weighted():
    # presence 100 (.40), consistency 100 (.35), reputation 100-0=100 (.25) -> 100
    score, sev = scoring.venue_score(100, 100, 0)
    assert score == 100
    assert sev == 'ok'


def test_venue_score_bad_venue_is_critical():
    # presence 33, consistency 0, reputation 100-100=0 -> ~13
    score, sev = scoring.venue_score(33, 0, 100)
    assert score < 33
    assert sev == 'critico'


def test_venue_score_renormalises_over_available_components():
    # only presence known -> score equals presence, weights renormalise to 1
    score, _ = scoring.venue_score(80, None, None)
    assert score == 80


def test_venue_score_no_data_is_zero():
    score, sev = scoring.venue_score(None, None, None)
    assert score == 0
    assert sev == 'critico'


def test_severity_bands():
    assert scoring.severity_for(20) == 'critico'
    assert scoring.severity_for(40) == 'alto'
    assert scoring.severity_for(60) == 'medio'
    assert scoring.severity_for(75) == 'ok'
    assert scoring.severity_for(None) == 'sin_datos'


# ── audit_score / summary_stats ─────────────────────────────────────

def _cluster(present, score=50, presence_pct=100, accuracy_avg=50, rep_score=0, platform_state=None,
             reply_rate=None):
    return {
        'sources_present': present,
        'reputation': {'score': rep_score},
        'venue_metrics': {
            'score': score, 'presence_pct': presence_pct, 'accuracy_avg': accuracy_avg,
            'platform_state': platform_state or {}, 'reply_rate_3m': reply_rate or {'value': 'N/D'},
        },
    }


def test_audit_score_averages_only_google_present():
    clusters = [
        _cluster(['google', 'apple'], score=40),
        _cluster(['google'], score=80),
        _cluster(['apple'], score=0),  # no Google -> excluded
    ]
    result = scoring.audit_score(clusters)
    assert result['score'] == 60  # (40 + 80) / 2
    assert result['venues_scored'] == 2


def test_audit_score_empty_when_no_google():
    result = scoring.audit_score([_cluster(['apple'])])
    assert result['score'] is None
    assert result['venues_scored'] == 0


def test_summary_stats_counts_issue_and_off():
    clusters = [
        _cluster(['google'], platform_state={'apple': 'issue', 'azure': 'ok', 'official': 'off'},
                 reply_rate={'value': 20}),
        _cluster(['google'], platform_state={'apple': 'off', 'azure': 'off', 'official': 'off'},
                 reply_rate={'value': 0}),
        _cluster(['google'], platform_state={'apple': 'ok', 'azure': 'ok', 'official': 'ok'},
                 reply_rate={'value': 'N/D'}),
    ]
    stats = scoring.summary_stats(clusters)
    assert stats['inconsistent_locations'] == 1   # only the first has an 'issue'
    assert stats['missing_some_platform'] == 1     # only the second is off in apple/azure
    assert stats['reply_rate_overall'] == 10       # mean of 20 and 0 (N/D excluded)
