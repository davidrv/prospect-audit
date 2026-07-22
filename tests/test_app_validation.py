import io

import app as app_module


def test_search_without_name_returns_400():
    client = app_module.app.test_client()
    resp = client.get('/search?official_url=https://example.com')
    assert resp.status_code == 400


def test_parse_request_params_reads_uploaded_csv():
    csv_content = 'name,address,phone\nZara Pelayo,Calle Pelai 58 Barcelona,932123456\n'
    client = app_module.app.test_client()
    data = {
        'name': 'Zara',
        'city': 'Barcelona',
        'official_csv': (io.BytesIO(csv_content.encode()), 'sedes.csv'),
    }
    with app_module.app.test_request_context(
            '/search', method='POST', data=data, content_type='multipart/form-data'):
        name, city, official_urls, csv_locations, csv_errors, official_comment = app_module._parse_request_params()

    assert name == 'Zara'
    assert official_urls == []
    assert len(csv_locations) == 1
    assert csv_locations[0]['name'] == 'Zara Pelayo'
    assert csv_errors == []
    assert official_comment == ''


def test_parse_request_params_reads_official_comment():
    client = app_module.app.test_client()
    with app_module.app.test_request_context(
            '/search', method='POST', data={'name': 'Zara', 'official_comment': '  le faltan fotos  '}):
        _, _, _, _, _, official_comment = app_module._parse_request_params()

    assert official_comment == 'le faltan fotos'
