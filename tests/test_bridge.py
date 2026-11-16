"""Priva Bridge als afnemer, getest tegen een nep-Bridge volgens het API-contract (plan sectie 8).

De nep-Bridge draait als httpx.MockTransport: Bearer-token met scope read:data, problem+json,
cursor-paginering, ontbreekt-regels (value null), revisies, 429 met Retry-After en timeouts.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import bridge, importer
from app.auth import router as auth_router
from app.core import router as core_router, seed_data
from app.db import Base, get_db
from app.models import AuditLog, utcnow
from app.settings import AppSettings, SettingsVersion, router as settings_router

ADMIN = {"username": "beheer", "password": "lang-wachtwoord-test"}
TOKEN = "pb_geheim-token-voor-de-gewichtsapp-123"
URL = "http://10.0.0.5:8000"
DAG = date(2026, 11, 10)


def problem(status, title, detail=""):
    return httpx.Response(
        status,
        headers={"content-type": "application/problem+json"},
        content=json.dumps({"type": "about:blank", "title": title, "status": status, "detail": detail}).encode(),
    )


class NepBridge:
    """Minimale Bridge volgens het contract; alleen wat de gewichtsapp gebruikt."""

    def __init__(self, page_size=7):
        self.token, self.scopes_token = TOKEN, {"read:data"}
        self.page_size = page_size
        self.rows: dict[tuple, dict] = {}
        self.calls: list[httpx.Request] = []
        self.rate_limit, self.retry_after = 0, "1"
        self.timeout = False
        self.catalog = list(bridge.WANTED_METRICS)
        self.freshness = {"last_import_at": "2026-11-16T04:07:00+01:00", "last_complete_date": "2026-11-15"}

    def zet(self, scope, day, metric, value, quality="ok", coverage=1.0, revision=1, unit=None, import_id="imp_0001"):
        units = {"lichtsom_lampen_kap": "mol/m²/dag", "temp_etmaal_avg": "°C", "rv_avg": "%", "lamp_brandseconden": "s"}
        self.rows[(scope, day.isoformat(), metric)] = dict(
            value=value,
            quality=quality,
            coverage=coverage,
            revision=revision,
            unit=unit or units.get(metric, "s"),
            import_id=import_id,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.timeout:
            raise httpx.ReadTimeout("traag", request=request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return problem(401, "Niet geautoriseerd")
        if "read:data" not in self.scopes_token:
            return problem(403, "Scope ontbreekt", "read:data vereist")
        if self.rate_limit > 0:
            self.rate_limit -= 1
            response = problem(429, "Te veel verzoeken")
            response.headers["Retry-After"] = self.retry_after
            return response
        path = request.url.path
        if path == "/api/v1/status":
            return httpx.Response(
                200,
                json={
                    **self.freshness,
                    "missing_dates": [],
                    "source_mode": "manual",
                    "warnings": [{"code": "unknown_column", "detail": "Klimaat: kolom X", "since": "2026-11-16"}],
                },
            )
        if path == "/api/v1/metrics":
            return httpx.Response(200, json={"data": [{"metric_key": k, "unit": "s"} for k in self.catalog]})
        if path != "/api/v1/daily":
            return problem(404, "Niet gevonden")
        params = request.url.params
        scopes, metrics = params.get_list("scope"), params.get_list("metric")
        if not scopes:
            return problem(400, "scope verplicht")
        start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        inherit = params.get("inherit") == "true"
        data = []
        for scope in scopes:
            day = start
            while day <= end:
                for metric in metrics:
                    found, source = self.rows.get((scope, day.isoformat(), metric)), scope
                    if found is None and inherit and scope.startswith("kap:"):
                        source = f"afdeling:{(int(scope[4:]) - 1) // 10 + 1}"
                        found = self.rows.get((source, day.isoformat(), metric))
                    if found is None:
                        data.append(
                            dict(
                                date=day.isoformat(),
                                scope=scope,
                                metric=metric,
                                value=None,
                                unit=None,
                                aggregation="sum",
                                coverage=0.0,
                                quality="ontbreekt",
                                resolved_from=scope,
                                sources=[],
                                import_id=None,
                                revision=None,
                            )
                        )
                    else:
                        data.append(
                            dict(
                                date=day.isoformat(),
                                scope=scope,
                                metric=metric,
                                aggregation="avg",
                                resolved_from=source,
                                sources=[f"bron:module:{scope[4:]}"],
                                **found,
                            )
                        )
                day += timedelta(days=1)
        offset = int(params.get("cursor") or 0)
        limit = min(int(params.get("limit", 500)), self.page_size)
        page = data[offset : offset + limit]
        nxt = str(offset + limit) if offset + limit < len(data) else None
        return httpx.Response(200, json={"data": page, "next_cursor": nxt, "freshness": self.freshness})


@pytest.fixture
def omgeving(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    nep = NepBridge()
    slaap = []
    monkeypatch.setattr(bridge, "_transport", httpx.MockTransport(nep.handler))
    monkeypatch.setattr(bridge, "_sleep", slaap.append)
    monkeypatch.setattr(bridge, "KEY_DIR", tmp_path / "secrets")
    monkeypatch.delenv("BRIDGE_TOKEN_KEY", raising=False)
    monkeypatch.setattr(importer, "SessionLocal", sessions)
    app = FastAPI()
    for router in (auth_router, core_router, settings_router, importer.router, bridge.router):
        app.include_router(router)

    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 200
        yield client, sessions, nep, slaap
    engine.dispose()


def aanzetten(client, **extra):
    body = {"enabled": True, "url": URL, **extra}
    response = client.put("/api/bridge/config", json=body)
    assert response.status_code == 200, response.text
    assert client.put("/api/bridge/token", json={"token": TOKEN}).status_code == 200
    return response.json()


def vul_week(nep, kap=1):
    for i in range(5):
        day = DAG + timedelta(days=i)
        nep.zet(f"kap:{kap}", day, "lichtsom_lampen_kap", 10.0 + i, quality="verdacht" if i == 1 else "ok")
        nep.zet("afdeling:1", day, "temp_etmaal_avg", 19.5)
        nep.zet("afdeling:1", day, "rv_avg", 81.0)
        nep.zet(f"kap:{kap}", day, "lamp_brandseconden", 39600)
    # dag 3 onvolledig, dag 5 (index 4) ontbreekt voor de lichtsom
    nep.zet(f"kap:{kap}", DAG + timedelta(days=2), "lichtsom_lampen_kap", 4.0, quality="onvolledig", coverage=0.4)
    del nep.rows[(f"kap:{kap}", (DAG + timedelta(days=4)).isoformat(), "lichtsom_lampen_kap")]


# --- Schakelaar uit: niets verandert -----------------------------------------------------------


def test_standaard_uit_en_dan_verandert_er_niets(omgeving):
    client, sessions, nep, _ = omgeving
    assert AppSettings().bridge_enabled is False
    status = client.get("/api/bridge/status").json()
    assert status["enabled"] is False and status["token_set"] is False
    assert client.get("/api/bridge/summary").json() == {"enabled": False}
    before = client.get("/api/climate", params={"start": "2026-11-01", "end": "2026-11-05", "bay_number": 1}).json()
    assert "bridge" not in before and all("lamp_source" not in d for d in before["derived"]["daily"])
    # Zelfs met URL, token en gevulde cache: zolang de schakelaar uit staat geen Bridge-gebruik.
    aanzetten(client)
    vul_week(nep)
    with sessions() as db:
        bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
    assert client.put("/api/bridge/config", json={"enabled": False, "url": URL}).status_code == 200
    calls = len(nep.calls)
    assert bridge.maybe_daily_sync(sessions) is None
    assert client.post("/api/bridge/sync", json={}).status_code == 409
    after = client.get("/api/climate", params={"start": "2026-11-01", "end": "2026-11-05", "bay_number": 1}).json()
    for key in ("series", "derived", "quality", "conflicts"):
        assert after[key] == before[key]
    assert "bridge" not in after
    assert len(nep.calls) == calls


# --- Instellingen en geheimen ------------------------------------------------------------------


def test_alleen_intern_netwerk(omgeving, monkeypatch):
    client, sessions, _, _ = omgeving
    for good in (
        "http://10.0.0.5:8000",
        "https://192.168.1.20/",
        "http://priva-bridge:8000",
        "https://bridge.lan",
        "http://[fd00::5]:8000",
        "http://10.0.0.5:8000/api/v1",
    ):
        assert bridge.validate_url(good)
    assert bridge.validate_url("http://10.0.0.5:8000/api/v1/") == "http://10.0.0.5:8000"
    for bad in (
        "http://8.8.8.8",
        "https://bridge.example.com",
        "ftp://10.0.0.5",
        "http://user:pw@10.0.0.5",
        "http://10.0.0.5/?x=1",
        "http://0.0.0.0",
    ):
        with pytest.raises(ValueError):
            bridge.validate_url(bad)
    response = client.put("/api/bridge/config", json={"enabled": True, "url": "https://bridge.example.com"})
    assert response.status_code == 422 and "intern" in response.text
    assert client.put("/api/bridge/config", json={"enabled": True, "url": ""}).status_code == 422
    # Interne naam die (bijv. door DNS-wijziging) naar een publiek adres wijst, wordt geweigerd.
    monkeypatch.setattr(bridge, "_resolve", lambda host: ["93.184.216.34"])
    with pytest.raises(bridge.BridgeFout) as error:
        bridge.check_resolves_internal("http://priva-bridge:8000")
    assert error.value.code == "geen_intern_adres"
    monkeypatch.setattr(bridge, "_resolve", lambda host: ["172.20.0.4"])
    bridge.check_resolves_internal("http://priva-bridge:8000")
    bad_ca = client.put("/api/bridge/config", json={"enabled": False, "url": URL, "ca_pem": "geen certificaat"})
    assert bad_ca.status_code == 422


def test_token_versleuteld_en_nooit_terug_naar_de_ui(omgeving):
    client, sessions, _, _ = omgeving
    aanzetten(client)
    status = client.get("/api/bridge/status")
    assert status.json()["token_set"] is True and status.json()["token_readable"] is True
    assert TOKEN not in status.text and TOKEN not in client.get("/api/settings").text
    with sessions() as db:
        row = db.get(bridge.BridgeCredential, 1)
        assert TOKEN not in row.ciphertext and row.ciphertext.startswith("v1:")
        assert bridge.load_token(db) == TOKEN
        assert all(TOKEN not in json.dumps(a.details) for a in db.scalars(select(AuditLog)))
        assert all(TOKEN not in json.dumps(v.settings) for v in db.scalars(select(SettingsVersion)))
        tampered = row.ciphertext[:-4] + ("AAAA" if not row.ciphertext.endswith("AAAA") else "BBBB")
        assert bridge.decrypt_token(tampered) is None
    assert client.delete("/api/bridge/token").json() == {"token_set": False}
    assert client.get("/api/bridge/status").json()["token_set"] is False


# --- Contract: status, 401/403, 429, timeouts --------------------------------------------------


def test_verbinding_testen(omgeving):
    client, _, nep, _ = omgeving
    aanzetten(client)
    result = client.post("/api/bridge/test").json()
    assert result["ok"] is True and result["last_complete_date"] == "2026-11-15"
    assert result["source_mode"] == "manual" and result["warnings"][0]["code"] == "unknown_column"
    assert nep.calls[0].url.path == "/api/v1/status"
    assert nep.calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    nep.token = "ander-token"
    result = client.post("/api/bridge/test").json()
    assert result["ok"] is False and result["code"] == "token_ongeldig" and "401" in result["message"]
    nep.token, nep.scopes_token = TOKEN, set()
    result = client.post("/api/bridge/test").json()
    assert result["code"] == "geen_toegang" and "read:data" in result["message"]


def test_429_retry_after_wordt_gerespecteerd(omgeving):
    client, sessions, nep, slaap = omgeving
    aanzetten(client)
    vul_week(nep)
    nep.rate_limit, nep.retry_after = 2, "2"
    with sessions() as db:
        result = bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
    assert result["status"] == "ok" and slaap == [2.0, 2.0]
    # Lange Retry-After: niet blijven wachten, maar fout vastleggen en de dagtaak houdt zich eraan.
    nep.rate_limit, nep.retry_after = 99, "3600"
    with sessions() as db, pytest.raises(bridge.BridgeFout) as error:
        bridge.sync(db, start=DAG, end=DAG)
    assert error.value.code == "te_veel_verzoeken"
    with sessions() as db:
        last = bridge.last_sync(db)
        assert last.status == "fout" and last.retry_after > utcnow() + timedelta(minutes=50)
    calls = len(nep.calls)
    assert bridge.maybe_daily_sync(sessions) is None
    assert len(nep.calls) == calls
    assert bridge.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_timeout_laat_cache_intact(omgeving):
    client, sessions, nep, _ = omgeving
    aanzetten(client)
    vul_week(nep)
    with sessions() as db:
        bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
        count = db.query(bridge.BridgeDailyValue).count()
    nep.timeout = True
    with sessions() as db, pytest.raises(bridge.BridgeFout) as error:
        bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
    assert error.value.code == "timeout"
    with sessions() as db:
        assert db.query(bridge.BridgeDailyValue).count() == count
        assert bridge.last_sync(db).error_code == "timeout"
    assert client.post("/api/bridge/test").json()["code"] == "timeout"


# --- Ophalen: cursor, ontbreekt, revisies ------------------------------------------------------


def test_cursor_ontbreekt_en_revisies(omgeving):
    client, sessions, nep, _ = omgeving
    aanzetten(client)
    vul_week(nep)
    with sessions() as db:
        first = bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
    assert first["pages"] > 5  # 44 scopes × 5 dagen × meetwaarden, pagina’s van 7
    daily_calls = [c for c in nep.calls if c.url.path == "/api/v1/daily"]
    assert all(c.url.params["inherit"] == "true" for c in daily_calls)
    assert any("cursor" in c.url.params for c in daily_calls)
    assert first["details"]["freshness"]["last_complete_date"] == "2026-11-15"
    with sessions() as db:
        rows, _ = bridge.cached_rows(db, ["kap:1"], DAG, DAG + timedelta(days=4))
        missing = rows[("kap:1", (DAG + timedelta(days=4)).isoformat(), "lichtsom_lampen_kap")]
        assert missing.quality == "ontbreekt" and missing.value is None
        temp = rows[("kap:1", DAG.isoformat(), "temp_etmaal_avg")]
        assert temp.value == 19.5 and temp.resolved_from == "afdeling:1"
        assert bridge.usable_model_value(missing, "bridge_ok_verdacht") is None
        incomplete = rows[("kap:1", (DAG + timedelta(days=2)).isoformat(), "lichtsom_lampen_kap")]
        assert incomplete.coverage == 0.4 and bridge.usable_model_value(incomplete, "bridge_ok_verdacht") is None
        suspect = rows[("kap:1", (DAG + timedelta(days=1)).isoformat(), "lichtsom_lampen_kap")]
        assert bridge.usable_model_value(suspect, "bridge_ok_verdacht") == 11.0
        assert bridge.usable_model_value(suspect, "bridge_alleen_ok") is None
    # Opnieuw ophalen zonder wijziging: geen nieuwe regels.
    with sessions() as db:
        assert bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))["rows_stored"] == 0
        between = utcnow()
    # Revisie: alleen de gewijzigde regel komt erbij; de oude blijft voor reproduceerbaarheid.
    nep.zet("kap:1", DAG, "lichtsom_lampen_kap", 12.5, revision=2, import_id="imp_0002")
    with sessions() as db:
        assert bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))["rows_stored"] == 1
        now, _ = bridge.cached_rows(db, ["kap:1"], DAG, DAG, metrics=["lichtsom_lampen_kap"])
        old, _ = bridge.cached_rows(db, ["kap:1"], DAG, DAG, metrics=["lichtsom_lampen_kap"], cutoff=between)
        key = ("kap:1", DAG.isoformat(), "lichtsom_lampen_kap")
        assert (now[key].value, now[key].revision, now[key].import_id) == (12.5, 2, "imp_0002")
        assert (old[key].value, old[key].revision) == (10.0, 1)


def test_ongeldige_regels_worden_nooit_nul():
    assert (
        bridge.normalize_row(
            {"date": "2026-11-10", "scope": "kap:1", "metric": "x", "value": 0, "quality": "ontbreekt"}
        )["value"]
        is None
    )
    assert (
        bridge.normalize_row({"date": "2026-11-10", "scope": "kap:1", "metric": "x", "value": "NaN", "quality": "ok"})[
            "quality"
        ]
        == "onbekend"
    )
    assert bridge.normalize_row({"date": "gisteren", "scope": "kap:1", "metric": "x"}) is None
    row = bridge.normalize_row(
        {"date": "2026-11-10", "scope": "kap:1", "metric": "x", "value": 0.0, "quality": "ok", "coverage": 7}
    )
    assert row["value"] == 0.0 and row["coverage"] is None  # een echte gemeten 0 blijft 0


def test_handmatig_ophalen_en_dagelijkse_taak(omgeving, monkeypatch):
    client, sessions, nep, _ = omgeving
    aanzetten(client, sync_hour=6, sync_days=3)
    vul_week(nep)
    response = client.post(
        "/api/bridge/sync", json={"start": DAG.isoformat(), "end": (DAG + timedelta(days=4)).isoformat()}
    )
    assert response.status_code == 202, response.text
    bridge.wait_for_background()
    status = client.get("/api/bridge/status").json()
    assert status["last_sync"]["status"] == "ok" and status["last_sync"]["trigger"] == "handmatig"
    assert status["freshness"]["last_complete_date"] == "2026-11-15" and status["cached_rows"] > 0
    assert client.post("/api/bridge/sync", json={"start": DAG.isoformat()}).status_code == 422
    monkeypatch.setattr(bridge, "_now_local", lambda: datetime(2026, 11, 16, 5, 0, tzinfo=bridge.NL))
    assert bridge.maybe_daily_sync(sessions) is None  # vóór het ingestelde uur
    monkeypatch.setattr(bridge, "_now_local", lambda: datetime(2026, 11, 16, 6, 30, tzinfo=bridge.NL))
    result = bridge.maybe_daily_sync(sessions)
    assert result["trigger"] == "auto" and (result["period_from"], result["period_to"]) == ("2026-11-13", "2026-11-15")
    assert bridge.maybe_daily_sync(sessions) is None  # één keer per dag
    summary = client.get("/api/bridge/summary").json()
    assert summary["enabled"] is True and summary["freshness"]["last_complete_date"] == "2026-11-15"


# --- Klimaatpagina: bron per dag en voorrang ---------------------------------------------------


def test_klimaat_toont_bron_per_dag(omgeving):
    client, sessions, nep, _ = omgeving
    aanzetten(client)
    vul_week(nep)
    with sessions() as db:
        bridge.sync(db, start=DAG, end=DAG + timedelta(days=4))
    params = {"start": DAG.isoformat(), "end": (DAG + timedelta(days=4)).isoformat(), "bay_number": 1}
    c = client.get("/api/climate", params=params).json()
    sources = [d["lamp_source"] for d in c["derived"]["daily"]]
    # ok, verdacht → Bridge; onvolledig en ontbreekt zonder eigen import → ontbreekt (nooit 0)
    assert sources == ["bridge", "bridge", "ontbreekt", "bridge", "ontbreekt"]
    assert [d["lamp_dli"] for d in c["derived"]["daily"]] == [10.0, 11.0, None, 13.0, None]
    assert c["derived"]["lamp_dli"] is None  # onvolledige periode geeft geen som
    days = c["bridge"]["days"]
    assert days[0]["sources"]["temperature"] == "bridge" and days[0]["metrics"]["temp_etmaal_avg"]["value"] == 19.5
    assert c["bridge"]["freshness"]["last_complete_date"] == "2026-11-15"
    assert c["bridge"]["voorrang"] == "bridge_ok_verdacht"
    aanzetten(client, voorrang="bridge_alleen_ok")
    c = client.get("/api/climate", params=params).json()
    assert [d["lamp_source"] for d in c["derived"]["daily"]][:2] == ["bridge", "ontbreekt"]
    assert bridge.choose(True, 5.0, "eigen_import_eerst") == "eigen_import"
    assert bridge.choose(False, 5.0, "eigen_import_eerst") == "bridge"
    assert bridge.choose(True, None, "bridge_ok_verdacht") == "eigen_import"
    assert bridge.choose(False, None, "bridge_ok_verdacht") is None
