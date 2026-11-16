from datetime import datetime, timedelta, timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import settings
from app.auth import router as auth_router
from app.core import router as core_router, seed_data
from app.db import Base, get_db
from app.models import AuditLog

@pytest.fixture
def api(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/test.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    app = FastAPI()
    for router in (auth_router, core_router, settings.router):
        app.include_router(router)
    def override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        client.post('/api/auth/setup', json={'username': 'beheer', 'password': 'lang-wachtwoord-test'})
        yield client, sessions
    engine.dispose()

def test_all_settings_persist_audited_without_lowering_release_gates(api):
    client, sessions = api
    config = client.get('/api/settings').json()['settings']
    config.update(latitude_deg=51.8, session_hours=8, measurement_tolerance_days=2,
                  steering_until_kd=33, backup_keep_days=90, min_training_cultivations=40)
    saved = client.post('/api/settings', json={'settings': config, 'reason': 'Bedrijfssituatie ingesteld'})
    assert saved.status_code == 200, saved.text
    actual = client.get('/api/settings').json()
    assert actual['settings'] == config
    assert len(actual['history']) == 1
    assert client.post('/api/settings', json={'settings': {**config, 'min_training_cultivations': 1}, 'reason': 'test'}).status_code == 422
    assert client.post('/api/settings', json={'settings': {**config, 'required_unseen_rounds': 1}, 'reason': 'test'}).status_code == 422
    with sessions() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'settings_changed'))
    client.post('/api/users', json={'username': 'lezer', 'password': 'lang-wachtwoord-test', 'role': 'lezer'})
    client.post('/api/auth/logout')
    client.post('/api/auth/login', json={'username': 'lezer', 'password': 'lang-wachtwoord-test'})
    assert client.get('/api/settings').json()['history'] == []
    assert client.post('/api/settings', json={'settings': config, 'reason': 'test'}).status_code == 403

def test_future_settings_do_not_rewrite_historical_calculations(api):
    client, sessions = api
    config = client.get('/api/settings').json()['settings']
    now = datetime.now(timezone.utc)
    effective = now + timedelta(days=2)
    assert client.post('/api/settings', json={'settings': {**config, 'latitude_deg': 50}, 'effective_from': effective.isoformat(), 'reason': 'Nieuwe locatie vanaf overmorgen'}).status_code == 200
    with sessions() as db:
        assert settings.effective_settings(db, now).latitude_deg == 52
        assert settings.effective_settings(db, effective + timedelta(seconds=1)).latitude_deg == 50
    assert client.post('/api/settings', json={'settings': config, 'effective_from': (now-timedelta(days=5)).isoformat(), 'reason': 'Geen terugwerkende kracht'}).status_code == 422
