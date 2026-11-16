"""Verbeterplan 0.3: aanmeldbeperking, oorsprong, MFA, oogstcorrectie, opschonen, back-ups en performance."""

import random
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import auth, forecast, importer, legacy_lighting, mfa, onderhoud, settings  # noqa: F401  (tabellen)
from app.auth import router as auth_router
from app.core import NL, dashboard_data, router as core_router, seed_data
from app.db import Base, get_db
from app.forecast import Harvest, _historical_baseline
from app.models import AuditLog, AuthSession, Cultivation, LoginAttempt, Measurement, PlanningVersion, User

ADMIN = {"username": "beheer", "password": "lang-wachtwoord-test"}


@pytest.fixture
def api(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    app = FastAPI()
    for router in (auth_router, mfa.router, onderhoud.router, core_router, settings.router):
        app.include_router(router)

    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 200
        yield client, sessions
    engine.dispose()


def set_settings(client, **changes):
    config = client.get("/api/settings").json()["settings"] | changes
    response = client.post("/api/settings", json={"settings": config, "reason": "test instelling"})
    assert response.status_code == 200, response.text


def request_from(peer, xff=None):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "client": (peer, 1234)})


# --- 1.1 Aanmeldbeperking per account en per echt IP ---------------------------------------
def test_client_ip_vertrouwt_alleen_geconfigureerde_proxy():
    proxies = auth.parse_proxies("10.0.0.0/8, 192.168.1.5")
    assert auth.client_ip(request_from("203.0.113.9", "1.2.3.4"), []) == "203.0.113.9"
    # Niet-vertrouwde peer: X-Forwarded-For wordt genegeerd (vervalsen onmogelijk).
    assert auth.client_ip(request_from("203.0.113.9", "1.2.3.4"), proxies) == "203.0.113.9"
    assert auth.client_ip(request_from("10.1.2.3", "198.51.100.7"), proxies) == "198.51.100.7"
    # Van rechts lezen: client-gestuurde waarde links wordt niet vertrouwd.
    assert auth.client_ip(request_from("10.1.2.3", "6.6.6.6, 198.51.100.7, 192.168.1.5"), proxies) == "198.51.100.7"
    assert auth.client_ip(request_from("10.1.2.3", "onzin"), proxies) == "10.1.2.3"
    assert auth.client_ip(request_from("10.1.2.3"), proxies) == "10.1.2.3"


def test_fout_wachtwoord_blokkeert_niet_iedereen_achter_zelfde_ip(api):
    client, sessions = api
    set_settings(client, login_delay_max_seconds=0)
    assert (
        client.post(
            "/api/users", json={"username": "collega", "password": "ander-lang-wachtwoord", "role": "medewerker"}
        ).status_code
        == 200
    )
    client.post("/api/auth/logout")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "beheer", "password": "fout"}).status_code == 401
    assert client.post("/api/auth/login", json=ADMIN).status_code == 429
    # Ander account vanaf hetzelfde (proxy-)adres kan nog steeds aanmelden.
    assert (
        client.post("/api/auth/login", json={"username": "collega", "password": "ander-lang-wachtwoord"}).status_code
        == 200
    )
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == "ip:testclient")) == 0


def test_ip_limiet_blijft_gelden(api):
    client, _ = api
    set_settings(client, login_delay_max_seconds=0, login_ip_max_attempts=4)
    client.post("/api/auth/logout")
    for name in ("a", "b", "c", "d"):
        assert client.post("/api/auth/login", json={"username": name, "password": "fout"}).status_code == 401
    assert client.post("/api/auth/login", json=ADMIN).status_code == 429


def test_ongeldige_proxy_instelling_geweigerd(api):
    client, _ = api
    config = client.get("/api/settings").json()["settings"] | {"trusted_proxies": "10.0.0.1, geen-ip"}
    assert client.post("/api/settings", json={"settings": config, "reason": "test"}).status_code == 422
    set_settings(client, trusted_proxies="172.16.0.0/12, 127.0.0.1")


# --- 1.6 Strikte oorsprongcontrole ----------------------------------------------------------
def test_strikte_oorsprongcontrole_met_referer_terugval(api):
    client, _ = api
    body = {"name": "nieuw", "target_weight_g": 100}
    assert client.post("/api/varieties", json=body, headers={"origin": ""}).status_code == 403
    assert (
        client.post(
            "/api/varieties", json=body, headers={"origin": "", "referer": "https://evil.example/x"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/varieties", json=body, headers={"origin": "", "referer": "http://testserver/#manage"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/varieties", json={**body, "name": "twee"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/api/varieties", headers={"origin": ""}).status_code == 200
    set_settings(client, strict_origin_check=False)
    assert client.post("/api/varieties", json={**body, "name": "drie"}, headers={"origin": ""}).status_code == 200


# --- 1.2 Optionele TOTP-MFA ------------------------------------------------------------------
def code_for(secret, offset=0):
    import time

    return mfa.totp(secret, int(time.time() // mfa.STEP) + offset)


def test_totp_rfc6238_testvector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert mfa.totp(secret, 59 // 30) == "287082"
    assert mfa.totp(secret, 1111111109 // 30) == "081804"


def test_mfa_aanzetten_aanmelden_en_hergebruik(api):
    client, sessions = api
    assert client.get("/api/auth/mfa").json()["mfa_enabled"] is False
    setup = client.post("/api/auth/mfa/setup").json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and setup["qr"].startswith("data:image/svg+xml")
    assert client.post("/api/auth/mfa/enable", json={"code": "000000"}).status_code == 422
    assert client.post("/api/auth/mfa/enable", json={"code": code_for(setup["secret"], -1)}).status_code == 200
    client.post("/api/auth/logout")
    first = client.post("/api/auth/login", json=ADMIN).json()
    assert first["mfa_required"] is True and "user" not in first
    assert client.get("/api/users").status_code == 401
    assert (
        client.post("/api/auth/login/mfa", json={"challenge": first["challenge"], "code": "123456"}).status_code == 401
    )
    # Zelfde tijdstap als bij inschakelen: hergebruik geweigerd.
    assert (
        client.post(
            "/api/auth/login/mfa", json={"challenge": first["challenge"], "code": code_for(setup["secret"], -1)}
        ).status_code
        == 401
    )
    ok = client.post("/api/auth/login/mfa", json={"challenge": first["challenge"], "code": code_for(setup["secret"])})
    assert ok.status_code == 200 and ok.json()["user"]["mfa_enabled"] is True
    assert client.get("/api/users").status_code == 200
    assert (
        client.post(
            "/api/auth/login/mfa", json={"challenge": first["challenge"], "code": code_for(setup["secret"])}
        ).status_code
        == 401
    )
    with sessions() as db:
        actions = {a.action for a in db.scalars(select(AuditLog))}
        assert {"mfa_enabled", "login_mfa_failed", "login"} <= actions
        assert db.scalar(select(User)).mfa_secret == setup["secret"]


def test_mfa_verplicht_voor_beheerders_standaard_uit(api):
    client, sessions = api
    assert client.get("/api/settings").json()["settings"]["mfa_required_admins"] is False
    # Kan niet verplicht worden zolang de eigen beheerder geen MFA heeft (voorkomt buitensluiten).
    config = client.get("/api/settings").json()["settings"] | {"mfa_required_admins": True}
    assert client.post("/api/settings", json={"settings": config, "reason": "verplicht"}).status_code == 422
    assert (
        client.post(
            "/api/users", json={"username": "tweede", "password": "tweede-lang-wachtwoord", "role": "admin"}
        ).status_code
        == 200
    )
    setup = client.post("/api/auth/mfa/setup").json()
    assert client.post("/api/auth/mfa/enable", json={"code": code_for(setup["secret"])}).status_code == 200
    set_settings(client, mfa_required_admins=True)
    client.post("/api/auth/logout")
    other = TestClient(client.app)
    login = other.post("/api/auth/login", json={"username": "tweede", "password": "tweede-lang-wachtwoord"}).json()
    assert login["user"]["mfa_setup_required"] is True
    assert other.get("/api/users").status_code == 403
    assert other.get("/api/varieties").status_code == 200  # gewone functies blijven werken
    s2 = other.post("/api/auth/mfa/setup").json()
    assert other.post("/api/auth/mfa/enable", json={"code": code_for(s2["secret"])}).status_code == 200
    assert other.get("/api/users").status_code == 200
    # Uitzetten is geblokkeerd zolang het verplicht is; reset door beheerder kan wel.
    assert (
        other.post(
            "/api/auth/mfa/disable", json={"password": "tweede-lang-wachtwoord", "code": code_for(s2["secret"], 1)}
        ).status_code
        == 409
    )
    users = other.get("/api/users").json()
    target = next(u for u in users if u["username"] == "beheer")
    assert other.patch(f"/api/users/{target['id']}", json={"mfa_reset": True}).status_code == 200
    with sessions() as db:
        assert db.get(User, target["id"]).mfa_enabled is False
        assert db.scalar(select(AuditLog).where(AuditLog.action == "mfa_reset")) is not None


def test_mfa_uitzetten_met_wachtwoord_en_code(api):
    client, _ = api
    setup = client.post("/api/auth/mfa/setup").json()
    client.post("/api/auth/mfa/enable", json={"code": code_for(setup["secret"], -1)})
    assert (
        client.post("/api/auth/mfa/disable", json={"password": "fout", "code": code_for(setup["secret"])}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/auth/mfa/disable", json={"password": ADMIN["password"], "code": code_for(setup["secret"], 1)}
        ).status_code
        == 200
    )
    client.post("/api/auth/logout")
    assert "user" in client.post("/api/auth/login", json=ADMIN).json()


# --- 2.5 Oogstcorrectie ----------------------------------------------------------------------
def make_cultivation(client, bay, ronde="R1", light_out="2026-09-01"):
    variety = client.get("/api/varieties").json()[0]["id"]
    cid = client.post(
        "/api/cultivations",
        json={"round_name": ronde, "bay_number": bay, "variety_id": variety, "beds": 6, "status": "active"},
    ).json()["id"]
    client.post(f"/api/cultivations/{cid}/planning", json={"light_out_date": light_out})
    return cid


def test_oogstcorrectie_met_reden_audit_en_baseline(api):
    client, sessions = api
    old = make_cultivation(client, 1, "R1")
    harvest = client.post(
        f"/api/cultivations/{old}/harvest",
        json={"start_date": "2026-10-01", "end_date": "2026-10-03", "weight_g": 80, "stem_count": 100},
    ).json()
    url = f"/api/cultivations/{old}/harvest/{harvest['id']}/correction"
    assert client.post(url, json={"weight_g": 90}).status_code == 422  # reden verplicht
    assert client.post(url, json={"weight_g": 80, "reason": "geen wijziging"}).status_code == 422
    assert client.post(url, json={"weight_g": 3000, "reason": "te zwaar"}).status_code == 422
    corrected = client.post(url, json={"weight_g": 95.5, "reason": "Weegschaal was verkeerd geijkt"})
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["supersedes_id"] == harvest["id"] and body["weight_g"] == 95.5 and body["stem_count"] == 100
    assert body["end_date"] == "2026-10-03" and body["reason"] == "Weegschaal was verkeerd geijkt"
    # Alleen de meest recente registratie is te corrigeren.
    assert client.post(url, json={"weight_g": 99, "reason": "oude rij"}).status_code == 409
    detail = client.get(f"/api/cultivations/{old}").json()
    assert [h["weight_g"] for h in detail["harvests"]] == [80, 95.5]
    with sessions() as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "correct", AuditLog.entity == "harvest"))
        assert entry.details["changes"] == {"weight_g": {"voor": 80, "na": 95.5}}
        current = make_cultivation(client, 2, "R2")
        c = db.get(Cultivation, current)
        baseline, ids = _historical_baseline(db, c, datetime.now(timezone.utc).replace(tzinfo=None))
        assert baseline == 95.5 and ids == [body["id"]]
        # Op een moment vóór de correctie gold de oorspronkelijke waarde (geen terugwerkende kracht).
        original_created = db.get(Harvest, harvest["id"]).created_at
        db.get(Harvest, body["id"]).created_at = original_created + timedelta(days=10)
        db.commit()
        baseline_before, _ = _historical_baseline(db, c, original_created + timedelta(days=5))
        assert baseline_before == 80


# --- 2.6 / 2.7 Back-upstatus en opschoning ---------------------------------------------------
def test_backupstatus_toont_schijfruimte_en_advies(api, tmp_path, monkeypatch):
    client, _ = api
    monkeypatch.setattr(onderhoud, "DATA_DIR", tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "chrysant-a.zip").write_bytes(b"x" * 10)
    status = client.get("/api/backups/status").json()
    assert status["aantal"] == 1 and status["totaal_bytes"] == 10 and status["bewaardagen"] == 0
    assert status["schijf"]["vrij_bytes"] > 0 and "onbeperkt" in status["advies"][0]
    assert "dezelfde dataset" in status["offsite_waarschuwing"]
    set_settings(client, backup_keep_days=30)
    assert (
        client.get("/api/backups/status").json()["advies"] == []
        or "onbeperkt" not in client.get("/api/backups/status").json()["advies"][0]
    )


def test_prune_backups_houdt_nieuwste(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(onderhoud, "DATA_DIR", tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    now = datetime.now(timezone.utc).timestamp()
    for name, age in (("chrysant-oud.zip", 40), ("chrysant-middel.zip", 35), ("chrysant-nieuw.zip", 1)):
        path = folder / name
        path.write_bytes(b"x")
        os.utime(path, (now - age * 86400, now - age * 86400))
    assert onderhoud.prune_backups(0) == []
    assert sorted(onderhoud.prune_backups(30, now)) == ["chrysant-middel.zip", "chrysant-oud.zip"]
    assert [p.name for p in folder.glob("*.zip")] == ["chrysant-nieuw.zip"]
    # Ook als alles oud is, blijft de nieuwste bewaard.
    os.utime(folder / "chrysant-nieuw.zip", (now - 100 * 86400, now - 100 * 86400))
    assert onderhoud.prune_backups(30, now) == []


def test_opschonen_sessies_pogingen_en_optioneel_audit(api):
    client, sessions = api
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions() as db:
        db.add(AuthSession(token_hash="a" * 64, user_id=1, expires_at=now - timedelta(minutes=1)))
        db.add(LoginAttempt(key="user:x", attempted_at=now - timedelta(days=3)))
        db.add(LoginAttempt(key="user:y", attempted_at=now - timedelta(minutes=1)))
        db.add(AuditLog(action="oud", entity="x", created_at=now - timedelta(days=400)))
        db.commit()
        result = onderhoud.cleanup(db, now)
        assert result == {"sessies": 1, "mfa_stappen": 0, "aanmeldpogingen": 1, "auditregels": 0}
        assert db.scalar(select(AuditLog).where(AuditLog.action == "oud")) is not None
        assert db.scalar(select(func.count()).select_from(AuthSession)) == 1  # eigen geldige sessie blijft
    set_settings(client, audit_keep_days=365)
    with sessions() as db:
        assert onderhoud.cleanup(db, datetime.now(timezone.utc).replace(tzinfo=None))["auditregels"] == 1
        assert db.scalar(select(AuditLog).where(AuditLog.action == "oud")) is None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "audit_ingekort")) is not None


def test_audit_paginering(api):
    client, _ = api
    for name in ("p1", "p2", "p3", "p4"):
        client.post("/api/varieties", json={"name": name})
    first = client.get("/api/audit?limit=2").json()
    assert len(first) == 2
    rest = client.get(f"/api/audit?before_id={first[-1]['id']}").json()
    assert rest and all(r["id"] < first[-1]["id"] for r in rest)
    assert len(client.get("/api/audit").json()) == len(first) + len(rest)


# --- 3.1 / 3.2 / 3.3 Performance zonder gedragsverandering -----------------------------------
def reference_dashboard(db):
    """De oorspronkelijke (in-geheugen) implementatie van dashboard_data, als referentie."""
    from app.models import Variety

    steering = settings.effective_settings(db).steering_until_kd
    cs = db.scalars(select(Cultivation)).all()
    active = [c for c in cs if c.status in ("active", "planned")]
    latest = {}
    for p in db.scalars(select(PlanningVersion).order_by(PlanningVersion.created_at.desc(), PlanningVersion.id.desc())):
        latest.setdefault(p.cultivation_id, p)
    measurements = db.scalars(select(Measurement).order_by(Measurement.measured_at.desc())).all()
    by = {}
    for m in measurements:
        by.setdefault(m.cultivation_id, []).append(m)
    names = {v.id: v.name for v in db.scalars(select(Variety))}
    today = datetime.now(NL).date()
    week_start = today - timedelta(days=today.weekday())
    species = {
        c.variety_id
        for c in active
        if any(
            m.kind == "KD10" and week_start <= m.measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date() <= today
            for m in by.get(c.id, [])
        )
    }
    out = []
    for c in active:
        plan = latest.get(c.id)
        if not plan or not plan.light_out_date:
            continue
        kd = (today - plan.light_out_date).days
        items = by.get(c.id, [])
        kinds = {m.kind for m in items}

        def task(kind, due):
            return {
                "cultivation_id": c.id,
                "bay_number": c.bay_number,
                "round_name": c.round_name,
                "variety_id": c.variety_id,
                "variety": names.get(c.variety_id),
                "kind": kind,
                "due_date": due.isoformat(),
                "actual_kd_day": kd,
                "days_until": (due - today).days,
                "status": "overdue" if due < today else "due" if due == today else "upcoming",
                "steering_until_kd": steering,
                "steering_window_open": 0 <= kd <= steering,
            }

        if "KD10" not in kinds and c.variety_id not in species and kd >= 3:
            out.append(task("KD10", plan.light_out_date + timedelta(days=10)))
        if "KD10" in kinds and "KD30" not in kinds and kd >= 23:
            out.append(task("KD30", plan.light_out_date + timedelta(days=30)))
        pilots = [m for m in items if m.kind == "pilot"]
        if pilots and kd >= 7:
            due = pilots[0].measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date() + timedelta(days=7)
            if due <= today + timedelta(days=7):
                out.append(task("pilot", due))
    out.sort(key=lambda x: (x["days_until"], x["bay_number"]))
    selected, seen = [], set()
    for item in out:
        if item["kind"] == "KD10":
            if item["variety_id"] in seen:
                continue
            seen.add(item["variety_id"])
        selected.append(item)
    return {
        "active_cultivations": len(active),
        "rounds": sorted({c.round_name for c in active}),
        "bays": 40,
        "measurements": len(measurements),
        "steering_until_kd": steering,
        "tasks": selected,
    }


def test_dashboard_in_sql_gelijk_aan_oorspronkelijke_berekening(api):
    client, sessions = api
    rng = random.Random(7)
    today = datetime.now(NL).date()
    varieties = [v["id"] for v in client.get("/api/varieties").json()]
    with sessions() as db:
        for bay in range(1, 41):
            status = rng.choice(["active", "planned", "harvested", "historical"])
            c = Cultivation(
                round_name=f"R{bay % 3}", bay_number=bay, variety_id=rng.choice(varieties), beds=6, status=status
            )
            db.add(c)
            db.flush()
            for _ in range(rng.randint(0, 2)):
                light_out = today - timedelta(days=rng.randint(-5, 45))
                db.add(
                    PlanningVersion(
                        cultivation_id=c.id,
                        light_out_date=light_out if rng.random() > 0.1 else None,
                        author_id=1,
                        created_at=datetime(2026, 1, 1) + timedelta(minutes=rng.randint(0, 5000)),
                    )
                )
            for _ in range(rng.randint(0, 4)):
                when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                    days=rng.randint(0, 20), hours=rng.randint(0, 23)
                )
                db.add(
                    Measurement(
                        client_id=str(uuid.uuid4()),
                        cultivation_id=c.id,
                        kind=rng.choice(["KD10", "KD30", "pilot"]),
                        measured_at=when,
                        user_id=1,
                        payload_hash="x",
                    )
                )
        db.commit()
        assert dashboard_data(db) == reference_dashboard(db)


def test_cultivation_detail_queries_vast_aantal(api):
    from sqlalchemy import event

    client, sessions = api
    cid = make_cultivation(client, 3)
    for i in range(6):
        stems = [{"position": f"p{j}", "weight_g": 50 + j + i, "length_cm": 60} for j in range(5)]
        assert (
            client.post(
                "/api/measurements",
                json={
                    "client_id": str(uuid.uuid4()),
                    "cultivation_id": cid,
                    "kind": "pilot",
                    "measured_at": f"2026-10-0{i + 1}T09:00:00",
                    "stems": stems,
                },
            ).status_code
            == 200
        )
    with sessions() as db:
        engine = db.get_bind()
    statements = []
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        detail = client.get(f"/api/cultivations/{cid}").json()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(detail["measurements"]) == 6 and detail["measurements"][0]["mean_weight_g"] == 52
    assert sum("FROM stems" in s for s in statements) == 1


def test_prognoses_alleen_herberekend_na_wijziging(api, monkeypatch):
    client, sessions = api
    monkeypatch.setattr(onderhoud, "SessionLocal", sessions)
    calls = []
    import app.forecast as fc

    monkeypatch.setattr(fc, "forecast", lambda db, cid, now: calls.append(cid))
    make_cultivation(client, 4)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert onderhoud.refresh_due_forecasts(now) is True
    assert calls  # geen snapshot → berekend
    calls.clear()
    # Niets veranderd, zelfde dag: geen volledige controle.
    assert onderhoud.refresh_due_forecasts(now + timedelta(minutes=1)) is False
    # Een relevante wijziging zet de vlag.
    cid = client.get("/api/cultivations").json()[0]["id"]
    client.post(f"/api/cultivations/{cid}/events", json={"occurred_at": "2026-10-01T10:00:00", "kind": "snoei"})
    assert onderhoud.refresh_due_forecasts(now + timedelta(minutes=2)) is True
    # Nieuwe dag: opnieuw controleren.
    assert onderhoud.refresh_due_forecasts(now + timedelta(minutes=3)) is False
    assert onderhoud.refresh_due_forecasts(now + timedelta(days=1)) is True
    # Instelling met toekomstige ingangsdatum: controle zodra die ingaat.
    config = client.get("/api/settings").json()["settings"]
    effective = datetime.now(timezone.utc) + timedelta(days=1, hours=2)
    client.post(
        "/api/settings",
        json={"settings": config | {"latitude_deg": 51}, "effective_from": effective.isoformat(), "reason": "later"},
    )
    assert onderhoud.refresh_due_forecasts(now + timedelta(days=1, minutes=1)) is True  # vlag door opslaan
    assert onderhoud.refresh_due_forecasts(now + timedelta(days=1, minutes=2)) is False
    assert onderhoud.refresh_due_forecasts(effective.replace(tzinfo=None) + timedelta(minutes=1)) is True
