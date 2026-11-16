"""De versie heeft één bron (app/__init__.py); andere plekken lezen die waarde."""

import re
import tomllib
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_leest_versie_uit_app():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "app.__version__"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_api_en_koppeling_gebruiken_dezelfde_versie():
    from app.main import app
    from app import koppeling

    assert app.version == __version__
    assert koppeling.info()["versie"] == __version__
