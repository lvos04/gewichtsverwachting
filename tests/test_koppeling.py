import hashlib
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import koppeling, importer  # noqa: F401  (registreert tabellen)
from app.auth import router as auth_router
from app.core import router as core_router, seed_data
from app.db import Base, get_db
from app.forecast import ForecastSnapshot
from app.koppeling import Koppelsleutel
from app.models import AuditLog

ADMIN = {'username': 'beheer', 'password': 'lang-wachtwoord-test'}
STATUSSEN = {'ok', 'let_op', 'actie', 'onbekend'}


@pytest.fixture
def api(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/test.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(core_router)
    koppeling.install(app)
    def override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = override
    koppeling._verzoeken.clear()
    with TestClient(app) as client:
        assert client.post('/api/auth/setup', json=ADMIN).status_code == 200
        assert client.put('/api/koppeling/status', json={'ingeschakeld': True}).json() == {'ingeschakeld': True}
        yield client, sessions
    engine.dispose()


def nieuwe_sleutel(client, naam='Bedrijfsdashboard'):
    response = client.post('/api/koppelsleutels', json={'naam': naam})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(sleutel):
    return {'Authorization': f'Bearer {sleutel}'}


def anoniem(client):
    """Server-naar-server: geen sessiecookie meesturen."""
    return TestClient(client.app)


def teelt(client, bay, status='active', ronde='R1', **planning):
    variety = client.get('/api/varieties').json()[0]['id']
    response = client.post('/api/cultivations', json={'round_name': ronde, 'bay_number': bay, 'variety_id': variety,
                                                      'beds': 6, 'status': status})
    assert response.status_code == 200, response.text
    cid = response.json()['id']
    if planning:
        assert client.post(f'/api/cultivations/{cid}/planning', json=planning).status_code == 200
    return cid


def test_sleutel_aanmaken_eenmalig_gehasht_en_geaudit(api):
    client, sessions = api
    data = nieuwe_sleutel(client)
    sleutel = data['sleutel']
    assert sleutel.startswith('bk_') and len(sleutel) >= 3 + 43
    assert data['voorvoegsel'] == sleutel[:8] and data['actief'] is True
    with sessions() as db:
        rij = db.scalar(select(Koppelsleutel))
        assert rij.sleutel_hash == hashlib.sha256(sleutel.encode()).hexdigest()
        assert sleutel not in str({c.name: getattr(rij, c.name) for c in rij.__table__.columns})
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'koppelsleutel.aanmaken')) is not None
    lijst = client.get('/api/koppelsleutels').json()
    assert len(lijst) == 1 and 'sleutel' not in lijst[0] and 'sleutel_hash' not in lijst[0]
    assert sleutel not in client.get('/api/koppelsleutels').text
    assert lijst[0]['aangemaakt_door'] == 'beheer' and lijst[0]['laatst_gebruikt_op'] is None
    remote = anoniem(client)
    assert remote.get('/api/koppeling/v1/info', headers=bearer(sleutel)).status_code == 200
    assert client.get('/api/koppelsleutels').json()[0]['laatst_gebruikt_op'] is not None


def test_401_zonder_ongeldige_of_ingetrokken_sleutel(api):
    client, sessions = api
    remote = anoniem(client)
    sleutel = nieuwe_sleutel(client)
    for headers in ({}, {'Authorization': 'Basic abc'}, bearer('bk_onbekend'), bearer(sleutel['sleutel'] + 'x'),
                    {'Authorization': sleutel['sleutel']}):
        response = remote.get('/api/koppeling/v1/samenvatting', headers=headers)
        assert response.status_code == 401
        assert response.json() == {'fout': 'ongeldige_koppelsleutel'}
        assert response.headers['www-authenticate'] == 'Bearer'
    # Een geldige sessiecookie is geen koppelsleutel.
    assert client.get('/api/koppeling/v1/info').status_code == 401
    assert remote.get('/api/koppeling/v1/info', headers=bearer(sleutel['sleutel'])).status_code == 200
    assert client.delete(f"/api/koppelsleutels/{sleutel['id']}").json()['actief'] is False
    response = remote.get('/api/koppeling/v1/info', headers=bearer(sleutel['sleutel']))
    assert response.status_code == 401 and response.json() == {'fout': 'ongeldige_koppelsleutel'}
    assert client.delete('/api/koppelsleutels/999').status_code == 404
    with sessions() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'koppelsleutel.intrekken')) is not None


def test_alleen_beheerders_beheren_sleutels(api):
    client, _ = api
    sleutel = nieuwe_sleutel(client)
    assert client.post('/api/koppelsleutels', json={'naam': 'x'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.delete(f"/api/koppelsleutels/{sleutel['id']}", headers={'Origin': 'https://evil.example'}).status_code == 403
    for rol in ('medewerker', 'lezer'):
        assert client.post('/api/users', json={'username': rol, 'password': 'nog-een-lang-wachtwoord', 'role': rol}).status_code == 200
    client.post('/api/auth/logout')
    assert client.get('/api/koppelsleutels').status_code == 401
    for rol in ('medewerker', 'lezer'):
        assert client.post('/api/auth/login', json={'username': rol, 'password': 'nog-een-lang-wachtwoord'}).status_code == 200
        assert client.get('/api/koppelsleutels').status_code == 403
        assert client.post('/api/koppelsleutels', json={'naam': 'x'}).status_code == 403
        assert client.delete(f"/api/koppelsleutels/{sleutel['id']}").status_code == 403
        client.post('/api/auth/logout')
    # Een koppelsleutel geeft evenmin toegang tot beheer.
    assert anoniem(client).get('/api/koppelsleutels', headers=bearer(sleutel['sleutel'])).status_code == 401


def test_info_en_samenvatting_vorm_zonder_verzonnen_gewicht(api):
    client, sessions = api
    sleutel = nieuwe_sleutel(client)['sleutel']
    today = datetime.now(timezone.utc).date()
    teelt(client, 1, plant_date=str(today - timedelta(days=60)), light_out_date=str(today - timedelta(days=50)),
          expected_harvest_date=str(today + timedelta(days=5)))
    teelt(client, 2, status='planned', expected_harvest_date=str(today + timedelta(days=40)))
    remote = anoniem(client)
    info = remote.get('/api/koppeling/v1/info', headers=bearer(sleutel)).json()
    assert info['app'] == 'gewichtsverwachting' and info['api_versie'] == '1'
    assert info['datasets'][0] == 'samenvatting' and {'teelten', 'oogstplanning', 'prognoses', 'oogsten'} <= set(info['datasets'])
    assert info['tijd'].endswith('Z') and info['bedrijf'] is None and info['versie']
    s = remote.get('/api/koppeling/v1/samenvatting', headers=bearer(sleutel)).json()
    assert s['app'] == 'gewichtsverwachting' and s['gegenereerd_op'].endswith('Z')
    assert 4 <= len(s['kpis']) <= 8
    for kpi in s['kpis']:
        assert set(kpi) == {'sleutel', 'label', 'waarde', 'eenheid', 'status', 'toelichting'}
        assert kpi['status'] in STATUSSEN
        if kpi['waarde'] is None:
            assert kpi['status'] == 'onbekend' and kpi['toelichting']
    for melding in s['meldingen']:
        assert melding['niveau'] in {'info', 'waarschuwing', 'kritiek'} and melding['tekst']
    kpis = {k['sleutel']: k for k in s['kpis']}
    assert kpis['actieve_teelten']['waarde'] == 1
    assert kpis['oogsten_komende_14_dagen']['waarde'] == 1
    assert kpis['gevalideerde_modellen']['waarde'] == 0
    # Doelgewicht 105 g is geen voorspelling.
    assert kpis['prognose_komende_oogsten']['waarde'] is None
    assert kpis['prognose_komende_oogsten']['status'] == 'onbekend'
    assert kpis['laatst_gemeten_gewicht']['waarde'] is None
    assert 105 not in [k['waarde'] for k in s['kpis']]
    # Achterstallige KD10-meting (licht uit 50 dagen geleden, geen meting).
    assert kpis['metingen_achterstallig']['waarde'] >= 1 and kpis['metingen_achterstallig']['status'] != 'ok'


def test_dataset_teelten_limiet_cursor_sinds_en_fouten(api):
    client, sessions = api
    sleutel = nieuwe_sleutel(client)['sleutel']
    ids = [teelt(client, bay, plant_date='2026-10-01') for bay in range(1, 6)]
    remote = anoniem(client)
    url = '/api/koppeling/v1/datasets/teelten'
    first = remote.get(url, params={'limiet': 2}, headers=bearer(sleutel)).json()
    assert first['dataset'] == 'teelten' and first['app'] == 'gewichtsverwachting'
    assert [i['id'] for i in first['items']] == ids[:2] and first['volgende_cursor']
    item = first['items'][0]
    assert item['kap'] == 1 and item['plantdatum'] == '2026-10-01' and item['bijgewerkt_op'].endswith('Z')
    assert not {'notes', 'reason', 'password_hash', 'user_id', 'author_id'} & set(item)
    seen = [i['id'] for i in first['items']]
    cursor = first['volgende_cursor']
    while cursor:
        page = remote.get(url, params={'limiet': 2, 'cursor': cursor}, headers=bearer(sleutel)).json()
        seen += [i['id'] for i in page['items']]
        cursor = page['volgende_cursor']
    assert seen == ids
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert remote.get(url, params={'sinds': future}, headers=bearer(sleutel)).json()['items'] == []
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    assert len(remote.get(url, params={'sinds': past}, headers=bearer(sleutel)).json()['items']) == 5
    for params in ({'limiet': 0}, {'limiet': 501}, {'limiet': 'x'}, {'sinds': 'gisteren'}, {'cursor': '!!'}, {'cursor': 'eDox'}):
        assert remote.get(url, params=params, headers=bearer(sleutel)).status_code == 422, params
    response = remote.get('/api/koppeling/v1/datasets/geheimen', headers=bearer(sleutel))
    assert response.status_code == 404 and response.json() == {'fout': 'onbekende_dataset'}
    assert remote.post('/api/koppeling/v1/datasets/teelten', headers=bearer(sleutel)).status_code == 405


def test_metingen_oogsten_en_prognoses_tonen_alleen_gevalideerd_gewicht(api):
    client, sessions = api
    sleutel = nieuwe_sleutel(client)['sleutel']
    a = teelt(client, 1, plant_date='2026-10-01', light_out_date='2026-10-10', expected_harvest_date='2026-12-01')
    b = teelt(client, 2, plant_date='2026-10-01', light_out_date='2026-10-10', expected_harvest_date='2026-12-02')
    stems = [{'position': str(i), 'weight_g': 40 + i, 'length_cm': 50} for i in range(5)]
    stems[4] |= {'excluded': True, 'exclusion_reason': 'beschadigd'}
    assert client.post('/api/measurements', json={'client_id': str(uuid.uuid4()), 'cultivation_id': a, 'kind': 'KD10',
                       'measured_at': '2026-10-20T09:00:00', 'notes': 'privé notitie', 'stems': stems}).status_code == 200
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions() as db:
        db.add(ForecastSnapshot(cultivation_id=a, as_of=now, status='experimental', point_g=110.0, low_g=100.0, high_g=120.0,
                                source_ids={}, response={'model_kind': 'KD10', 'target_g': 105}))
        db.add(ForecastSnapshot(cultivation_id=b, as_of=now, status='operational', point_g=98.04, low_g=90.0, high_g=106.1,
                                source_ids={}, response={'model_kind': 'KD30'}))
        db.commit()
    remote = anoniem(client)
    metingen = remote.get('/api/koppeling/v1/datasets/metingen', headers=bearer(sleutel)).json()['items']
    assert len(metingen) == 1 and metingen[0]['aantal_takken'] == 4 and metingen[0]['gemiddeld_gewicht_g'] == 41.5
    assert 'notes' not in metingen[0] and 'privé' not in str(metingen)
    prognoses = {p['teelt_id']: p for p in remote.get('/api/koppeling/v1/datasets/prognoses', headers=bearer(sleutel)).json()['items']}
    assert prognoses[a]['gewicht_g'] is None and prognoses[a]['status'] == 'onbekend' and prognoses[a]['toelichting']
    assert prognoses[a]['ondergrens_g'] is None and 'target_g' not in prognoses[a]
    assert prognoses[b]['status'] == 'gevalideerd' and prognoses[b]['gewicht_g'] == 98.0 and prognoses[b]['modeltype'] == 'KD30'
    assert client.post(f'/api/cultivations/{a}/harvest', json={'end_date': '2026-11-01', 'weight_g': 101.5,
                       'stem_count': 900, 'notes': 'vertrouwelijk'}).status_code == 200
    oogsten = remote.get('/api/koppeling/v1/datasets/oogsten', params={'limiet': 1}, headers=bearer(sleutel)).json()
    assert oogsten['items'][0]['gemiddeld_takgewicht_g'] == 101.5 and oogsten['volgende_cursor'] is None
    assert 'vertrouwelijk' not in str(oogsten)
    planning = remote.get('/api/koppeling/v1/datasets/oogstplanning', headers=bearer(sleutel)).json()['items']
    assert [p['teelt_id'] for p in planning] == [b]  # geoogste teelt valt eruit
    s = remote.get('/api/koppeling/v1/samenvatting', headers=bearer(sleutel)).json()
    kpis = {k['sleutel']: k for k in s['kpis']}
    assert kpis['laatst_gemeten_gewicht']['waarde'] == 41.5


def test_rate_limit_per_sleutel(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(koppeling, 'LIMIET_PER_MINUUT', 3)
    sleutel = nieuwe_sleutel(client)['sleutel']
    remote = anoniem(client)
    assert [remote.get('/api/koppeling/v1/info', headers=bearer(sleutel)).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert remote.get('/api/koppeling/v1/info', headers=bearer(sleutel)).json() == {'fout': 'te_veel_verzoeken'}
    andere = nieuwe_sleutel(client, 'Andere app')['sleutel']
    assert remote.get('/api/koppeling/v1/info', headers=bearer(andere)).status_code == 200


def test_koppeling_standaard_uit_en_schakelbaar(api):
    client, sessions = api
    sleutel = nieuwe_sleutel(client)['sleutel']
    extern = anoniem(client)
    assert extern.get('/api/koppeling/v1/info', headers=bearer(sleutel)).status_code == 200
    assert client.put('/api/koppeling/status', json={'ingeschakeld': False}).json() == {'ingeschakeld': False}
    assert client.get('/api/koppeling/status').json() == {'ingeschakeld': False}
    for pad in ('/info', '/samenvatting', '/datasets/teelten'):
        response = extern.get('/api/koppeling/v1' + pad, headers=bearer(sleutel))
        assert response.status_code == 403
        assert response.json() == {'fout': 'koppeling_uitgeschakeld'}
    # Ook zonder of met een ongeldige sleutel: uitgeschakeld.
    assert extern.get('/api/koppeling/v1/info').json() == {'fout': 'koppeling_uitgeschakeld'}
    # Sleutelbeheer blijft beschikbaar.
    assert client.get('/api/koppelsleutels').status_code == 200
    with sessions() as db:
        acties = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.action == 'settings_changed'))]
        assert len(acties) == 2
    assert client.put('/api/koppeling/status', json={'ingeschakeld': True}).status_code == 200
    assert extern.get('/api/koppeling/v1/info', headers=bearer(sleutel)).status_code == 200


def test_koppeling_uit_op_verse_installatie(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/vers.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    koppeling.install(app)
    def override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        response = client.get('/api/koppeling/v1/info', headers=bearer('bk_' + 'x' * 43))
        assert response.status_code == 403 and response.json() == {'fout': 'koppeling_uitgeschakeld'}
    engine.dispose()
