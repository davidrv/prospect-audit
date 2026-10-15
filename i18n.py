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
