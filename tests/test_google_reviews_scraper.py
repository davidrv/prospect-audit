from datetime import datetime, timedelta, timezone

import google_reviews_scraper as scraper

_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _epoch(**kwargs):
    return int((_NOW - timedelta(**kwargs)).timestamp())


def test_parse_relative_time_dias():
    assert scraper._parse_relative_time('hace 3 días', now=_NOW) == _epoch(days=3)


def test_parse_relative_time_semanas():
    assert scraper._parse_relative_time('hace 2 semanas', now=_NOW) == _epoch(weeks=2)


def test_parse_relative_time_meses():
    assert scraper._parse_relative_time('hace 4 meses', now=_NOW) == _epoch(days=4 * 30)


def test_parse_relative_time_anos():
    assert scraper._parse_relative_time('hace 1 año', now=_NOW) == _epoch(days=365)


def test_parse_relative_time_ingles():
    assert scraper._parse_relative_time('3 weeks ago', now=_NOW) == _epoch(weeks=3)


def test_parse_relative_time_singular():
    assert scraper._parse_relative_time('hace un mes', now=_NOW) == _epoch(days=30)


def test_parse_relative_time_editado_no_rompe_el_parseo():
    assert scraper._parse_relative_time('hace 2 meses (editado)', now=_NOW) == _epoch(days=60)


def test_parse_relative_time_no_reconocido():
    assert scraper._parse_relative_time('recientemente', now=_NOW) is None
    assert scraper._parse_relative_time('', now=_NOW) is None
    assert scraper._parse_relative_time(None, now=_NOW) is None


def test_parse_rating():
    assert scraper._parse_rating('5 estrellas') == 5
    assert scraper._parse_rating('1 star') == 1
    assert scraper._parse_rating('Puntuación: 4,0 de 5 estrellas') == 4
    assert scraper._parse_rating(None) is None
    assert scraper._parse_rating('sin rating') is None


def test_parse_review_card_completo():
    review = scraper._parse_review_card(
        'Ana García', '5 estrellas', 'hace 2 meses', 'Muy buen servicio.', now=_NOW,
    )
    assert review == {
        'author_name': 'Ana García',
        'rating': 5,
        'text': 'Muy buen servicio.',
        'relative_time_description': 'hace 2 meses',
        'time': _epoch(days=60),
        'has_owner_reply': False,
    }


def test_parse_review_card_con_respuesta_del_propietario():
    review = scraper._parse_review_card(
        'Ana García', '5 estrellas', 'hace 2 meses', 'Muy buen servicio.',
        has_owner_reply=True, now=_NOW,
    )
    assert review['has_owner_reply'] is True


def test_parse_review_card_sin_texto():
    review = scraper._parse_review_card('Juan', '3 estrellas', 'hace 1 semana', '', now=_NOW)
    assert review['text'] == ''
    assert review['rating'] == 3


def test_parse_review_card_sin_autor():
    review = scraper._parse_review_card(None, '4 estrellas', 'hace 5 días', 'Bien.', now=_NOW)
    assert review['author_name'] is None


def test_build_place_url_desde_place_id():
    url = scraper._build_place_url('ChIJabc123', locale='es')
    assert url == 'https://www.google.com/maps/place/?q=place_id:ChIJabc123&hl=es'


def test_build_place_url_desde_url_completa_sin_query():
    full_url = 'https://www.google.com/maps/place/Mi+Negocio/@41.0,2.0,15z'
    assert scraper._build_place_url(full_url, locale='es') == f'{full_url}?hl=es'


def test_build_place_url_desde_url_con_query_existente():
    full_url = 'https://www.google.com/maps/place/Mi+Negocio?foo=bar'
    assert scraper._build_place_url(full_url, locale='es') == f'{full_url}&hl=es'


def test_build_place_url_no_duplica_hl_ya_presente():
    full_url = 'https://www.google.com/maps/place/Mi+Negocio?hl=en'
    assert scraper._build_place_url(full_url, locale='es') == full_url


def test_cutoff_de_tres_meses():
    cutoff = scraper._cutoff_epoch(3, now=_NOW)
    dentro_de_rango = scraper._parse_relative_time('hace 1 mes', now=_NOW)
    fuera_de_rango = scraper._parse_relative_time('hace 5 meses', now=_NOW)
    assert dentro_de_rango > cutoff
    assert fuera_de_rango < cutoff


# ── _classify_action_link ────────────────────────────────────────────────

def test_classify_action_link_reserva():
    assert scraper._classify_action_link('Reservar una mesa') == 'reservation'
    assert scraper._classify_action_link('Reserve a table') == 'reservation'
    assert scraper._classify_action_link('Pedir cita') == 'reservation'


def test_classify_action_link_pedido():
    assert scraper._classify_action_link('Pedir comida a domicilio') == 'order'
    assert scraper._classify_action_link('Order online') == 'order'


def test_classify_action_link_menu():
    assert scraper._classify_action_link('Ver el menú') == 'menu'
    assert scraper._classify_action_link('View menu') == 'menu'


def test_classify_action_link_entradas():
    assert scraper._classify_action_link('Comprar entradas') == 'tickets'
    assert scraper._classify_action_link('Buy tickets') == 'tickets'


def test_classify_action_link_ignora_botones_genericos():
    assert scraper._classify_action_link('Indicaciones') is None
    assert scraper._classify_action_link('Guardar') is None
    assert scraper._classify_action_link('Compartir') is None
    assert scraper._classify_action_link('Cerca') is None
    assert scraper._classify_action_link(None) is None
    assert scraper._classify_action_link('') is None


def test_classify_action_link_texto_no_reconocido():
    assert scraper._classify_action_link('Sugerir una edición') is None


# ── _parse_post_card ──────────────────────────────────────────────────────

def test_parse_post_card_completo():
    post = scraper._parse_post_card('Nueva carta de otoño ya disponible', 'hace 2 semanas', now=_NOW)
    assert post == {
        'text': 'Nueva carta de otoño ya disponible',
        'relative_time_description': 'hace 2 semanas',
        'time': _epoch(weeks=2),
    }


def test_parse_post_card_sin_fecha_reconocida():
    post = scraper._parse_post_card('Texto sin fecha', None, now=_NOW)
    assert post['time'] is None
    assert post['relative_time_description'] == ''


def test_parse_post_card_limpia_fuente_en_linea():
    post = scraper._parse_post_card('Oferta especial', 'hace 5 días en', now=_NOW)
    assert post['relative_time_description'] == 'hace 5 días'
    assert post['time'] == _epoch(days=5)
