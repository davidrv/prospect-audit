import app as app_module
import normalize
import report
import venue_metrics


def _cluster(cid, sources=('google',), rating=None, review_count=None,
             flags=None, negative_samples=None):
    return {
        'cluster_id': cid,
        'sources_present': list(sources),
        'flags': flags or [],
        'reputation': {'rating': rating, 'review_count': review_count,
                       'negative_samples': negative_samples or []},
    }


def _audit(clusters):
    return {'clusters': clusters, 'summary': app_module._audit_summary(clusters)}


def test_apply_report_edits_drops_deleted_clusters():
    audit = _audit([_cluster('L1'), _cluster('L2'), _cluster('L3')])
    app_module._apply_report_edits(audit, ['L2'], {})
    assert [c['cluster_id'] for c in audit['clusters']] == ['L1', 'L3']


def test_apply_report_edits_recomputes_summary_after_delete():
    # Two Google venues, one with a critical flag; deleting it should drop
    # both the total and the critical count in the recomputed summary.
    crit = _cluster('L1', flags=[{'severity': 'critical'}])
    ok = _cluster('L2')
    audit = _audit([crit, ok])
    assert audit['summary']['total_locations'] == 2
    assert audit['summary']['locations_with_critical_flags'] == 1

    app_module._apply_report_edits(audit, ['L1'], {})
    assert audit['summary']['total_locations'] == 1
    assert audit['summary']['locations_with_critical_flags'] == 0


def test_apply_report_edits_attaches_comments_to_kept_clusters():
    audit = _audit([_cluster('L1'), _cluster('L2')])
    app_module._apply_report_edits(audit, [], {'L1': '  Cliente prioritario  ', 'L2': ''})
    by_id = {c['cluster_id']: c for c in audit['clusters']}
    assert by_id['L1']['presenter_comment'] == 'Cliente prioritario'  # trimmed
    assert by_id['L2']['presenter_comment'] is None                    # empty -> None


def test_apply_report_edits_does_not_comment_a_deleted_cluster():
    audit = _audit([_cluster('L1'), _cluster('L2')])
    app_module._apply_report_edits(audit, ['L1'], {'L1': 'no debería aparecer', 'L2': 'ok'})
    ids = [c['cluster_id'] for c in audit['clusters']]
    assert 'L1' not in ids
    assert audit['clusters'][0]['presenter_comment'] == 'ok'


def test_apply_report_edits_caps_comment_length():
    audit = _audit([_cluster('L1')])
    app_module._apply_report_edits(audit, [], {'L1': 'x' * 5000})
    assert len(audit['clusters'][0]['presenter_comment']) == app_module._MAX_COMMENT_LEN


def test_report_from_data_renders_pdf_without_recompute(monkeypatch):
    captured = {}

    def fake_render(name, city, results, audit, official_comment):
        captured['name'] = name
        captured['clusters'] = [c['cluster_id'] for c in audit['clusters']]
        captured['comment_L1'] = audit['clusters'][0].get('presenter_comment')
        return b'%PDF-fake'

    monkeypatch.setattr('report.render_report_pdf', fake_render)

    client = app_module.app.test_client()
    resp = client.post('/report/from_data', json={
        'name': 'Prospect', 'city': 'Barcelona', 'official_comment': '',
        'audit': _audit([_cluster('L1'), _cluster('L2')]),
        'deleted_cluster_ids': ['L2'],
        'row_comments': {'L1': 'Nota de venta'},
    })

    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data == b'%PDF-fake'
    assert captured['clusters'] == ['L1']              # L2 dropped
    assert captured['comment_L1'] == 'Nota de venta'   # comment attached


def test_report_from_data_rejects_missing_audit():
    client = app_module.app.test_client()
    resp = client.post('/report/from_data', json={'name': 'X'})
    assert resp.status_code == 400


def _llm_cluster(cid, hits, runs_n=3):
    c = _cluster(cid)
    runs = ([{'appears': True, 'position': 1, 'label': 'x'}] * hits
            + [{'appears': False, 'position': None, 'label': None}] * (runs_n - hits))
    c['venue_metrics'] = {'llm_visibility': {'prompt': cid + ' prompt', 'runs': runs, 'hits': hits}}
    return c


def _audit_with_llm(clusters, hits_total):
    audit = _audit(clusters)
    per = {c['cluster_id']: c['venue_metrics']['llm_visibility'] for c in clusters}
    audit['summary']['llm_visibility'] = {
        'engine': 'chatgpt', 'prompt_template': 'tienda X en {zona}, Barcelona', 'category': 'tienda X',
        'runs': 3, 'venues_checked': len(per), 'checks_total': 3 * len(per), 'hits_total': hits_total,
        'per_venue': per, 'calls': 0}
    return audit


def test_delete_recomputes_llm_aggregate():
    # Borrar una sede analizada debe recalcular hits/comprobaciones/sedes del
    # agregado de IA — si no, seguiría contando la sede borrada.
    audit = _audit_with_llm([_llm_cluster('L1', 3), _llm_cluster('L2', 1)], hits_total=4)
    app_module._apply_report_edits(audit, ['L2'], {})
    llm = audit['summary']['llm_visibility']
    assert llm['venues_checked'] == 1
    assert llm['checks_total'] == 3
    assert llm['hits_total'] == 3                 # solo quedan los 3 hits de L1
    assert 'L2' not in llm['per_venue']
    assert llm['prompt_template'] == 'tienda X en {zona}, Barcelona'  # plantilla conservada


def test_delete_all_llm_venues_clears_aggregate():
    audit = _audit_with_llm([_llm_cluster('L1', 3)], hits_total=3)
    app_module._apply_report_edits(audit, ['L1'], {})
    assert audit['summary']['llm_visibility'] is None


def test_recompute_endpoint_returns_updated_summary():
    client = app_module.app.test_client()
    audit = _audit_with_llm([_llm_cluster('L1', 3), _llm_cluster('L2', 2)], hits_total=5)
    resp = client.post('/audit/recompute',
                       json={'audit': audit, 'deleted_cluster_ids': ['L2'], 'row_comments': {}})
    assert resp.status_code == 200
    summary = resp.get_json()['summary']
    assert summary['total_locations'] == 1
    assert summary['llm_visibility']['hits_total'] == 3
    assert summary['llm_visibility']['venues_checked'] == 1


def test_recompute_endpoint_rejects_missing_audit():
    client = app_module.app.test_client()
    assert client.post('/audit/recompute', json={}).status_code == 400


def test_venue_rows_propagate_presenter_comment():
    rec = normalize.make_record('google', 'g1', name='Foo', formatted_address='X')
    cluster = {
        'cluster_id': 'L1',
        'sources_present': ['google'],
        'by_source': {'google': rec},
        'reputation': {},
        'canonical_label': 'Foo',
        'canonical_address': 'X',
        'presenter_comment': 'Buen fit para el pitch',
    }
    venue_metrics.compute_venue_metrics([cluster], has_official_data=False, city='Barcelona')
    rows = report._venue_cards([cluster])
    assert rows[0]['presenter_comment'] == 'Buen fit para el pitch'


def test_venue_rows_presenter_comment_defaults_to_none():
    rec = normalize.make_record('google', 'g1', name='Foo', formatted_address='X')
    cluster = {
        'cluster_id': 'L1',
        'sources_present': ['google'],
        'by_source': {'google': rec},
        'reputation': {},
        'canonical_label': 'Foo',
        'canonical_address': 'X',
    }
    venue_metrics.compute_venue_metrics([cluster], has_official_data=False, city='Barcelona')
    rows = report._venue_cards([cluster])
    assert rows[0]['presenter_comment'] is None
