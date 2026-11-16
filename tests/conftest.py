"""Gemeenschappelijke testinstellingen.

De app weigert standaard schrijfverzoeken zonder `Origin`/`Referer` (strikte
oorsprongcontrole). Echte browsers sturen `Origin` altijd mee; de TestClient
niet. Daarom krijgt elke TestClient standaard dezelfde `Origin` als een browser
op de testserver. Tests die de controle zelf toetsen, overschrijven de header.
"""

import starlette.testclient

_original_init = starlette.testclient.TestClient.__init__


def _init(self, *args, headers=None, **kwargs):
    merged = {"origin": "http://testserver"}
    merged.update(headers or {})
    _original_init(self, *args, headers=merged, **kwargs)


starlette.testclient.TestClient.__init__ = _init
