"""Mensajes traducibles por la UI.

El backend sigue generando el texto en español (es lo que se loguea, se guarda
en el histórico y pinta el PDF), pero cada mensaje lleva además una clave y sus
parámetros para que el frontend lo renderice en el idioma elegido (diccionario
LANGS en public/index.html). Si la UI no conoce la clave, cae al texto español.
"""


class Msg(str):
    """Un `str` (el texto en español) que además lleva `key` y `params` — así
    viaja por los `emit()`/`progress()` existentes sin cambiar su firma."""

    def __new__(cls, key, text, **params):
        obj = super().__new__(cls, text)
        obj.key = key
        obj.params = params
        return obj


def as_dict(message):
    """{key, params} de un Msg, o None si es un str normal (sin traducción)."""
    key = getattr(message, 'key', None)
    if key is None:
        return None
    return {'key': key, 'params': dict(message.params)}


# ── Versión en inglés de los mensajes que también salen en el PDF ──
# (hallazgos por sede, checks del store locator y hallazgos sobre los datos
# oficiales). Espejo de MSG_EN en public/index.html: si cambias un texto aquí,
# cámbialo también allí. El log de progreso solo se traduce en la UI.

_SRC_EN = {'google': 'Google Maps', 'apple': 'Apple Maps', 'azure': 'Bing Maps',
           'official': 'the official website', 'llm': 'AI visibility'}
_DAY_EN = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_FIELD_EN = {'phone': 'phone number', 'website': 'website', 'hours': 'hours', 'geo': 'geo'}


def _src(s, cap=False):
    label = _SRC_EN.get(s, s)
    if cap:
        label = label.removeprefix('the ')
        label = label[:1].upper() + label[1:]
    return label


def _srcs(sources):
    return ', '.join(_src(s) for s in sources or [])


def _vs(p):
    return f"{_src(p['anchor'], True)}: {p['anchor_value']} vs {_src(p['other'], True)}: {p['other_value']}"


_EN = {
    'flag_r1': lambda p: (f"Present on {_srcs(p['present'])} but not on {_src(p['anchor'])} — check whether it is "
                          f"an outdated or duplicate listing, or a location that has closed."),
    'flag_r11': lambda p: (f"Not found on {_srcs(p['missing'])} (present on {_src(p['anchor'])} and "
                           f"{_srcs(p['present'])})."),
    'flag_r3': lambda p: (f"Possible duplicate listing on {_srcs(p['dup']) or 'a platform'}: several listings for "
                          f"this venue — the one closest to Google is compared. Worth reviewing/merging the duplicates."),
    'flag_r8': lambda p: (f"Very different address on {_src(p['other'])} compared to {_src(p['anchor'])} "
                          f"— possibly a wrong or duplicate listing."),
    'flag_missing_field': lambda p: (f"No {_FIELD_EN[p['field']]} published on {_src(p['lacks'])} "
                                     f"({_src(p['has'])} has one)."),
    'flag_conflict_field': lambda p: f"Different {_FIELD_EN[p['field']]} — {_vs(p)}.",
    'flag_r9': lambda p: f"Name differs from {_src(p['anchor'])} — {_vs(p)}.",
    'flag_r9_strong': lambda p: f"Name differs significantly from {_src(p['anchor'])} — {_vs(p)}.",
    'flag_r14b': lambda p: (f"Contradictory hours on {_DAY_EN[p['day']]} — "
                            f"{_src(p['anchor'], True)}: {'closed' if p['anchor_closed'] else 'open'}, "
                            f"{_src(p['other'], True)}: {'closed' if p['other_closed'] else 'open'}."),
    'flag_r14': lambda p: (f"Different hours on {_DAY_EN[p['day']]} (~{p['diff']} min difference) — "
                           f"{_src(p['anchor'], True)}: {p['anchor_value']}, "
                           f"{_src(p['other'], True)}: {p['other_value']}."),
    'chk_present': lambda p: 'present',
    'chk_not_found_pl': lambda p: 'not found',
    'chk_not_found': lambda p: 'not found',
    'chk_not_analyzed': lambda p: 'not analyzed',
    'chk_x_of_n': lambda p: f"{p['x']} of {p['n']}",
    'chk_no_venues': lambda p: 'no venues',
    'chk_yes': lambda p: 'yes',
    'chk_yes_urls': lambda p: f"yes ({p['n']} venue URLs)",
    'chk_no_schema': lambda p: 'no schema to evaluate',
    'chk_complete': lambda p: 'complete',
    'chk_missing': lambda p: 'missing ' + ', '.join(_FIELD_EN.get(f, f) for f in p.get('fields') or []),
    'finding_no_schema': lambda p: ('The store locator is accessible but has no schema.org structured data '
                                    '(LocalBusiness) — implementing it is recommended to improve visibility in '
                                    'search engines and AI assistants (local SEO / GEO).'),
    'finding_inaccessible': lambda p: ('We could not access this store locator automatically (blocked or '
                                       'access error). Try uploading a CSV with the locations instead.'),
}


def render(text, key, params, lang):
    """El texto en `lang`: el español tal cual, o la versión inglesa de `key`.
    Clave desconocida/ausente (p. ej. auditoría antigua) → texto español."""
    if lang != 'en' or not key or key not in _EN:
        return text
    try:
        return _EN[key](params or {})
    except (KeyError, IndexError, TypeError):
        return text
