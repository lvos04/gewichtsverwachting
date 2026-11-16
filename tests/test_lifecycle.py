"""First-boot migration and restored application startup in isolated processes."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def run_python(code, data_dir):
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            env={**os.environ, 'DATA_DIR': str(data_dir), 'DATABASE_URL': f'sqlite:///{data_dir}/app.db'},
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr

def test_migration_backup_restore_and_relocated_source(tmp_path):
    original = tmp_path / 'original'
    restored = tmp_path / 'restored'
    run_python('''
import time
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from app.main import app
from app.db import engine, DATA_DIR, SessionLocal, Base
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from app.backups import restore_backup
from app.importer import PrivaSource, original_path
with TestClient(app) as client:
    assert client.get('/health').json()['status'] == 'ok'
    assert len(inspect(engine).get_table_names()) >= 26
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert client.post('/api/auth/setup',json={'username':'testbeheer','password':'een-lang-testwachtwoord'}).status_code==200
    assert len(client.get('/api/bays').json())==40
    csv = b'label;pcu;type_1;idx_1;type_2;idx_2;startdate;enddate;value\\nAve_24h_CompTemp;synthetic;Afdeling;1;;;16-11-26;16-11-26;21,2\\n'
    response=client.post('/api/imports', files={'files':('synthetic.csv',csv,'text/csv')})
    assert response.status_code == 200, response.text
    job_id=response.json()['id']
    for _ in range(200):
        report=client.get(f'/api/imports/{job_id}').json()
        if report['status']=='preview': break
        time.sleep(.025)
    assert report['status']=='preview', report
    assert client.post(f'/api/imports/{job_id}/confirm').status_code==200
    for _ in range(200):
        report=client.get(f'/api/imports/{job_id}').json()
        if report['status']=='completed': break
        time.sleep(.025)
    assert report['status']=='completed', report
    archive=client.post('/api/backups').json()['filename']
    restore_backup(DATA_DIR/'backups'/archive, DATA_DIR.parent/'restored')
''', original)
    run_python('''
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.importer import PrivaSource, original_path
with TestClient(app) as client:
    assert client.post('/api/auth/login',json={'username':'testbeheer','password':'een-lang-testwachtwoord'}).status_code==200
    assert client.get('/api/imports/1').json()['status']=='completed'
    climate=client.get('/api/climate?start=2026-11-16&end=2026-11-16').json()
    assert climate['series'][0]['dates'][0]['value']==21.2
    with SessionLocal() as db:
        source=db.scalar(select(PrivaSource))
        assert not source.path.startswith('/')
        assert original_path(source.path).is_file()
''', restored)
