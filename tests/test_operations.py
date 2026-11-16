import io
from datetime import datetime
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import operations, backups
from app.auth import router as auth_router
from app.core import router as core_router, seed_data
from app.db import Base, get_db
from app.models import Event

@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/app.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    monkeypatch.setattr(operations, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(backups, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(backups, 'engine', engine)
    monkeypatch.setattr(operations.shutil, 'which', lambda _: None)
    app = FastAPI()
    for router in (auth_router, core_router, operations.router):
        app.include_router(router)
    def override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        assert client.post('/api/auth/setup', json={'username': 'beheer', 'password': 'een-lang-wachtwoord'}).status_code == 200
        variety = client.get('/api/varieties').json()[0]['id']
        cultivation = client.post('/api/cultivations', json={'round_name': 'R1', 'bay_number': 1, 'variety_id': variety, 'beds': 6}).json()['id']
        yield client, sessions, cultivation, tmp_path
    engine.dispose()

def test_cards_scans_review_and_retry_do_not_duplicate_events(api):
    client, sessions, cid, directory = api
    client.post(f'/api/cultivations/{cid}/planning', json={'plant_date': '2026-11-01', 'light_out_date': '2026-11-12'})
    cards = client.get(f'/api/cards?ids={cid}')
    assert cards.status_code == 200
    assert 'data:image/png;base64,' in cards.text and 'KD30: 2026-12-12' in cards.text
    image = io.BytesIO()
    Image.new('RGB', (50, 60), color='white').save(image, 'PNG')
    response = client.post('/api/scans', data={'cultivation_id': cid}, files={'file': ('kaart.png', image.getvalue(), 'image/png')})
    assert response.status_code == 200, response.text
    scan = response.json()
    assert scan['status'] == 'review'
    with sessions() as db:
        assert not list(db.scalars(select(Event)))
    event = {'occurred_at': '2026-11-13T09:00:00+01:00', 'kind': 'alar', 'amount': 30, 'unit': 'g/1000m2', 'scope': 'volvelds', 'reason': 'Overgenomen van gecontroleerde kaart'}
    assert client.patch(f"/api/scans/{scan['id']}", json={'notes': 'nagekeken', 'events': [event]}).status_code == 200
    assert client.post(f"/api/scans/{scan['id']}/confirm", json={'confirmed': False}).status_code == 422
    first = client.post(f"/api/scans/{scan['id']}/confirm", json={'confirmed': True})
    assert first.status_code == 200, first.text
    retry = client.post(f"/api/scans/{scan['id']}/confirm", json={'confirmed': True})
    assert retry.json()['event_ids'] == first.json()['event_ids']
    with sessions() as db:
        events = list(db.scalars(select(Event)))
        assert len(events) == 1
        assert events[0].occurred_at == datetime(2026, 11, 13, 8)
    assert client.patch(f"/api/scans/{scan['id']}", json={'notes': 'wijziging'}).status_code == 409

def test_backup_restore_checksum_and_secret_free_export(api):
    client, sessions, cid, directory = api
    (directory / 'originals').mkdir()
    (directory / 'originals' / 'bron.csv').write_text('originele inhoud')
    response = client.post('/api/backups')
    assert response.status_code == 200, response.text
    archive_path = directory / 'backups' / response.json()['filename']
    destination = directory.parent / (directory.name + '-restored')
    manifest = backups.restore_backup(archive_path, destination)
    assert manifest['database'] == 'app.db'
    assert (destination / 'originals' / 'bron.csv').read_text() == 'originele inhoud'
    import sqlite3
    with sqlite3.connect(destination / 'app.db') as db:
        assert db.execute('SELECT COUNT(*) FROM cultivations').fetchone()[0] == 1
    with pytest.raises(ValueError, match='lege map'):
        backups.restore_backup(archive_path, destination)
    exported = client.get('/api/export')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert 'cultivations.csv' in archive.namelist()
        assert 'users.csv' not in archive.namelist()
        assert 'auth_sessions.csv' not in archive.namelist()
    tampered = directory / 'corrupt.zip'
    with zipfile.ZipFile(archive_path) as src, zipfile.ZipFile(tampered, 'w') as dest:
        for name in src.namelist():
            dest.writestr(name, b'changed' if name == 'originals/bron.csv' else src.read(name))
    with pytest.raises(ValueError, match='Checksum'):
        backups.restore_backup(tampered, directory / 'corrupt-restore')
