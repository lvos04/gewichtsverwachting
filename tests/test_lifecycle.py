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


def test_upgrade_from_0001_preserves_data_and_begins_honest_history(tmp_path):
    original = tmp_path / 'legacy'
    run_python('''
from alembic import command
from alembic.config import Config
from app.db import engine
from sqlalchemy import text
# Build the actual frozen 0.1 schema; do not use current metadata as a stand-in.
command.upgrade(Config('alembic.ini'),'0001')
with engine.begin() as connection:
    connection.execute(text("INSERT INTO users(id,username,password_hash,role,active,created_at) VALUES(1,'legacy','not-a-real-password','admin',1,'2020-01-01 00:00:00')"))
    connection.execute(text("INSERT INTO varieties(id,name,target_weight_g) VALUES(1,'antonov',107)"))
    connection.execute(text("INSERT INTO bays(number,department,area_m2,beds) VALUES(1,1,100,6)"))
    connection.execute(text("INSERT INTO cultivations(id,round_name,bay_number,variety_id,beds,category,status,label_quality) VALUES(1,'legacyround',1,1,6,'standard','active','verified')"))
    connection.execute(text("INSERT INTO planning_versions(cultivation_id,plant_date,light_out_date,density,created_at,author_id) VALUES(1,'2020-01-01','2020-01-10',50,'2020-01-01 00:00:00',1)"))
    connection.execute(text("INSERT INTO cultivation_revisions(cultivation_id,category,label_quality,status,created_at) VALUES(1,'standard','verified','active','2020-01-01 00:00:00')"))
    connection.execute(text("INSERT INTO mapping_versions(id,bay_number,object_type,object_number,valid_from,created_at,reason) VALUES(1,1,'valve',1,'2020-01-01','2020-01-01 00:00:00','known current mapping')"))
''', original)
    run_python('''
from datetime import datetime
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app.main import app
from app.db import DATA_DIR, SessionLocal, engine
from app.backups import restore_backup
from app.models import Cultivation, MappingRevision, MappingVersion, PlanningVersion, VarietyTargetVersion
from app.core import target_at
from app.environment import _mappings_as_of
with TestClient(app) as client:
    with SessionLocal() as db:
        assert db.get(Cultivation,1).round_name=='legacyround'
        plan=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==1))
        assert plan.density==50 and plan.planned_stems is None
        mapping=db.scalar(select(MappingRevision).where(MappingRevision.mapping_id==1))
        assert mapping.object_number==1
        assert mapping.created_at>datetime(2020,1,1)
        # The migration does not pretend this mutable state was known in 2020.
        assert _mappings_as_of(db,1,datetime(2021,1,1))[0]==[]
        assert target_at(db,1,datetime(2021,1,1)) is None
        assert target_at(db,1,datetime.utcnow()).target_weight_g==107
    from app.main import refresh_due_forecasts
    from app.forecast import ForecastSnapshot
    refresh_due_forecasts()
    with SessionLocal() as db:
        before=db.scalars(select(ForecastSnapshot).where(ForecastSnapshot.cultivation_id==1)).all()
        assert len(before)==1 and before[0].response['target_g']==107
        db.add(VarietyTargetVersion(variety_id=1,target_weight_g=108))
        db.commit()
    refresh_due_forecasts()
    with SessionLocal() as db:
        after=db.scalars(select(ForecastSnapshot).where(ForecastSnapshot.cultivation_id==1).order_by(ForecastSnapshot.id)).all()
        assert len(after)==2 and after[-1].response['target_g']==108

    backups=list((DATA_DIR/'backups').glob('*.zip'))
    assert backups, 'No automatic backup before changing the existing schema'
    restored=DATA_DIR.parent/'before-upgrade'
    restore_backup(backups[0],restored)
    import sqlite3
    with sqlite3.connect(restored/'app.db') as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0001'
        assert db.execute('SELECT round_name FROM cultivations WHERE id=1').fetchone()[0]=='legacyround'
        assert db.execute('SELECT target_weight_g FROM varieties WHERE id=1').fetchone()[0]==107
        assert 'active' not in [r[1] for r in db.execute('PRAGMA table_info(varieties)')]
''', original)


def test_upgrade_0002_preserves_pending_workbook_review(tmp_path):
    location=tmp_path/'pending-history'
    run_python('''
from alembic import command
from alembic.config import Config
from app.db import engine
from sqlalchemy import text
command.upgrade(Config('alembic.ini'),'0002')
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,password_hash,role,active,created_at) VALUES(1,'pending','unused','admin',1,'2026-01-01 00:00:00')"))
    c.execute(text("INSERT INTO history_imports(id,filename,sha256,raw_path,status,created_at,user_id) VALUES(1,'synthetic.xlsx',:sha,'history/synthetic.xlsx','preview','2026-01-01 00:00:00',1)"),{'sha':'a'*64})
    c.execute(text("INSERT INTO history_preview_rows(id,import_id,row_number,status,reasons,original,effective,cultivations,created_ids,payload_hash) VALUES(1,1,2,'ambiguous','[]','{}','{}','[]','[]',:sha)"),{'sha':'b'*64})
from app.main import migrate
migrate()
with engine.connect() as c:
    assert c.execute(text('SELECT profile,status FROM history_imports WHERE id=1')).one()==('generic','preview')
    assert c.execute(text('SELECT source,status FROM history_preview_rows WHERE id=1')).one()==(None,'ambiguous')
    assert c.execute(text('SELECT count(*) FROM legacy_lighting_jobs')).scalar()==0
from app.db import DATA_DIR
assert list((DATA_DIR/'backups').glob('*.zip'))
''', location)
