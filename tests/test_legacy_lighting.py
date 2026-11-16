"""Synthetic XLSX fixtures only; farm workbooks never enter the repository."""
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import legacy_lighting as light
from app.auth import require_user
from app.db import Base, get_db
from app.history_import import HistoryImport
from app.importer import Calibration, ImportJob, PrivaSource, PrivaValue, normalize_row
from app.models import User

REAL_SCHEDULE=light.schedule
DAY=date(2024,8,1)


def workbook(path,*,days=(DAY,),percentage=25,extra=(),end_markers=False,modules=(101,)):
    wb=Workbook(); ws=wb.active;ws.title='data'
    for column,module in enumerate(modules,2):
        ws.cell(1,column,'Gem. licht%');ws.cell(2,column,f'SYNTHETIC:MOD. LICHT {module}');ws.cell(3,column,'%')
    stamps=[]
    for day in days:
        begin=datetime.combine(day,datetime.min.time())
        stamps.extend(begin+timedelta(minutes=5*(i+int(end_markers))) for i in range(288))
    stamps.extend(extra)
    for row,stamp in enumerate(stamps,4):
        ws.cell(row,1,stamp)
        for col,module in enumerate(modules,2):ws.cell(row,col,percentage)
    wb.save(path)
    return path


@pytest.fixture
def api(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine); sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with sessions() as db:
        db.add(User(id=1,username='synthetic',password_hash='x',role='admin'));db.commit()
    monkeypatch.setattr(light,'DATA_DIR',tmp_path)
    monkeypatch.setattr(light,'SessionLocal',sessions)
    monkeypatch.setattr(light,'schedule',light.run_job)
    light._stop.clear()
    app=FastAPI();app.include_router(light.router)
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[require_user]=lambda:SimpleNamespace(id=1,role='admin')
    with TestClient(app) as client:
        yield SimpleNamespace(client=client,sessions=sessions,root=tmp_path)
    engine.dispose()


def history(api,**kwargs):
    folder=api.root/'history-imports';folder.mkdir(exist_ok=True)
    path=workbook(folder/f'synthetic-{len(list(folder.glob("*.xlsx")))}.xlsx',**kwargs)
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    with api.sessions() as db:
        source=HistoryImport(filename=path.name,sha256=checksum,raw_path=path.relative_to(api.root).as_posix(),status='uploaded',user_id=1)
        db.add(source);db.commit(); source_id=source.id
    response=api.client.post(f'/api/legacy-lighting/from-history/{source_id}')
    assert response.status_code==200,response.text
    job=api.client.get(f"/api/legacy-lighting/{response.json()['id']}").json()
    assert job['status']=='preview',job
    return job,path


def confirm(api,job,*,kind='linear_dim_percent',role='interval_start'):
    response=api.client.post(f"/api/legacy-lighting/{job['id']}/confirm",json={
         'confirmed':True,'reason':'Synthetic source interpretation validated',
         'interpretation':{'timezone':'Europe/Amsterdam','sample_seconds':300,'date_system':'1900',
                           'timestamp_role':role,'value_kind':kind,'unit':'%'}})
    assert response.status_code==200,response.text
    return api.client.get(f"/api/legacy-lighting/{job['id']}").json()


def calibration(api,*,dim=1,created=None,module=101):
    with api.sessions() as db:
        setting=Calibration(kind='lamp',valid_from=date(2024,1,1),object_type='light',object_number=module,
             channel='',label=light.LABEL,value_json=json.dumps({'hours_label':light.LABEL,'time_unit':'h',
             'independent_contribution':True,'ppfd':120,'dim_factor':dim}),reason='Synthetic actual full output check',
             confirmed=True,user_id=1,created_at=created or light.now())
        db.add(setting);db.commit();return setting.id


def snapshot(api,start=DAY,end=DAY,as_of=None,**kwargs):
    with api.sessions() as db:
        return light.supplemental_daily_light(db,{101},start,end,as_of or light.now()+timedelta(seconds=2),**kwargs)


def test_actual_header_schema_preview_and_unknown_archive(api):
    job,path=history(api,modules=(101,102))
    assert job['preview']['row_count']==288
    assert job['preview']['date_system']=='1900'
    assert [m['module'] for m in job['preview']['modules']]==[101,102]
    assert job['preview']['modules'][0]['unit']=='%'
    with api.sessions() as db:assert db.scalar(select(func.count(light.LegacyLightingDay.id)))==0
    archived=confirm(api,job,kind='unknown',role='unknown')
    assert archived['status']=='completed',archived
    day=snapshot(api)['days'][DAY.isoformat()][101]
    assert day['quality']=='unverified' and day['mol_m2_day'] is None and day['full_power_hours'] is None
    assert day['raw_percent_hours']==600
    assert day['channel'] is None
    assert day['cell_refs']==['data!B4:B291']
    assert path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==job['checksum']
    with api.sessions() as db:
        stored=db.get(light.LegacyLightingJob,job['id'])
        assert not Path(stored.raw_path).is_absolute()
        assert db.scalar(select(func.count(light.LegacyLightingDay.id)))==2


def test_verified_dimming_integral_requires_actual_capacity_and_no_double_dim(api):
    job,_=history(api);assert confirm(api,job)['status']=='completed'
    unknown=snapshot(api)['days'][DAY.isoformat()][101]
    assert unknown['full_power_hours']==6 and unknown['mol_m2_day'] is None
    calibration(api,dim=.5)
    double=snapshot(api)['days'][DAY.isoformat()][101]
    assert double['mol_m2_day'] is None  # 25% already weights equivalent hours
    valid_id=calibration(api,dim=1)
    value=snapshot(api)['days'][DAY.isoformat()][101]
    assert value['quality']=='verified_supplemental'
    assert value['mol_m2_day']==pytest.approx(2.592)
    assert value['coverage']==1 and value['calibration_ids']==[valid_id]
    assert value['source_ids']['checksum']==job['checksum']


def test_priva_hours_always_block_even_missing_or_unvalidated(api):
    job,_=history(api);confirm(api,job);calibration(api)
    with api.sessions() as db:
        batch=ImportJob(user_id=1,status='completed');db.add(batch);db.flush()
        primary=PrivaSource(job_id=batch.id,name='synthetic.csv',path='imports/none.csv',checksum='primary-checksum',
            profile='belichting',logical_key='primary',version=1,activated_at=light.now())
        db.add(primary);db.flush()
        row={'label':'Sum_24h_ModGrowlightSysHours','pcu':'SYNTHETIC','type_1':'modgroeilichtsturing',
             'idx_1':'101','type_2':'kanaal','idx_2':'2','startdate':'1-8-24','enddate':'1-8-24','value':''}
        raw=PrivaValue(source_id=primary.id,**normalize_row(row,1));db.add(raw);db.commit();value_id=raw.id
    blocked=snapshot(api)
    assert blocked['days'][DAY.isoformat()][101]['quality']=='blocked_by_priva'
    assert blocked['days'][DAY.isoformat()][101]['mol_m2_day'] is None
    assert blocked['blocked_by_priva']==[{'date':'2024-08-01','module':101}]
    # A different primary metric does not block missing primary hours.
    with api.sessions() as db:
        value=db.get(PrivaValue,value_id);value.label='Ave_24h_ModGrowlightPAR';db.commit()
    assert snapshot(api)['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    assert snapshot(api,priva_presence={(DAY,101)})['days'][DAY.isoformat()][101]['quality']=='blocked_by_priva'


def test_out_of_order_dates_supported_but_duplicate_timestamps_block(api):
    earlier=DAY-timedelta(days=1)
    job,_=history(api,days=(DAY,earlier))
    assert job['preview']['quality']['out_of_order_segments']==1
    assert confirm(api,job)['status']=='completed';calibration(api)
    result=snapshot(api,start=earlier,end=DAY)
    assert result['days'][earlier.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    assert result['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    duplicate,_=history(api,extra=(datetime.combine(DAY,datetime.min.time()),))
    assert duplicate['preview']['quality']['duplicate_timestamp_rows']==1
    assert confirm(api,duplicate)['status']=='completed'
    result=snapshot(api)['days'][DAY.isoformat()][101]
    assert result['mol_m2_day'] is None and 'duplicate_timestamps' in result['warnings']


def test_interval_end_is_explicit_and_dst_wall_clock_remains_unresolved(api):
    job,_=history(api,end_markers=True)
    assert job['preview']['quality']['incomplete_timestamp_days']==2
    assert confirm(api,job,role='interval_end')['status']=='completed';calibration(api)
    value=snapshot(api)['days'][DAY.isoformat()][101]
    assert value['coverage']==1 and value['mol_m2_day']==pytest.approx(2.592)
    dst=date(2024,3,31)
    other,_=history(api,days=(dst,));confirm(api,other)
    unresolved=snapshot(api,start=dst,end=dst)['days'][dst.isoformat()][101]
    assert unresolved['mol_m2_day'] is None and 'dst_clock_unresolved' in unresolved['warnings']


def test_daily_checkpoint_restart_is_idempotent(api,monkeypatch):
    second=DAY+timedelta(days=1)
    job,_=history(api,days=(DAY,second))
    original=light._flush_day;interrupted=False
    def fail_once(*args,**kwargs):
        nonlocal interrupted
        original(*args,**kwargs)
        if not interrupted:
            interrupted=True;raise RuntimeError('Simulated crash after committed daily batch')
    monkeypatch.setattr(light,'_flush_day',fail_once)
    assert confirm(api,job)['status']=='failed'
    with api.sessions() as db:
        assert db.scalar(select(func.count(light.LegacyLightingDay.id)))==1
        assert db.get(light.LegacyLightingJob,job['id']).resume_row==291
    monkeypatch.setattr(light,'_flush_day',original)
    response=api.client.post(f"/api/legacy-lighting/{job['id']}/resume")
    assert response.status_code==200,response.text
    assert api.client.get(f"/api/legacy-lighting/{job['id']}").json()['status']=='completed'
    with api.sessions() as db:
        rows=list(db.scalars(select(light.LegacyLightingDay)))
        assert len(rows)==2 and all(r.valid_count==288 and r.duplicate_count==0 for r in rows)


def test_source_activation_calibration_creation_and_revocation_as_of(api):
    job,_=history(api);confirm(api,job)
    base=light.now()
    late=calibration(api,created=base+timedelta(days=1))
    unknown=snapshot(api,as_of=base+timedelta(seconds=1))['days'][DAY.isoformat()][101]
    assert unknown['mol_m2_day'] is None and unknown['calibration_ids']==[]
    with api.sessions() as db:
        setting=db.get(Calibration,late);setting.created_at=base-timedelta(seconds=1)
        record=db.get(light.LegacyLightingJob,job['id']);record.revoked_at=base+timedelta(days=2);db.commit()
    old=snapshot(api,as_of=base+timedelta(seconds=1))
    assert old['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    assert snapshot(api,as_of=base+timedelta(days=3))['days']=={}
    assert snapshot(api,as_of=base-timedelta(days=1))['days']=={}


def test_repeat_reference_new_interpretation_and_non_destructive_revoke(api):
    job,path=history(api);confirm(api,job,kind='unknown')
    again=api.client.post(f"/api/legacy-lighting/from-history/{job['history_import_id']}").json()
    assert again['id']==job['id']
    response=api.client.post(f"/api/legacy-lighting/from-history/{job['history_import_id']}",json={'new_version':True})
    assert response.status_code==200,response.text
    new=api.client.get(f"/api/legacy-lighting/{response.json()['id']}").json()
    assert new['version']==2 and new['supersedes_id']==job['id']
    assert confirm(api,new)['status']=='completed';calibration(api)
    assert snapshot(api)['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    assert api.client.post(f"/api/legacy-lighting/{new['id']}/revoke").status_code==200
    assert snapshot(api)['days'][DAY.isoformat()][101]['quality']=='unverified'
    assert path.exists()
    with api.sessions() as db:assert db.scalar(select(func.count(light.LegacyLightingDay.id)))==2


def test_raw_checksum_changes_fail_without_activation(api):
    job,path=history(api)
    path.write_bytes(path.read_bytes()+b'changed')
    error=confirm(api,job)
    assert error['status']=='failed' and 'checksum' in error['error']
    assert snapshot(api)['days']=={}


def test_daily_report_route_and_archive_validation(api):
    job,_=history(api);confirm(api,job)
    response=api.client.get('/api/legacy-lighting/daily?start=2024-08-01&end=2024-08-01&modules=101')
    assert response.status_code==200,response.text
    assert response.json()['days']['2024-08-01']['101']['quality']=='unverified'
    assert api.client.get('/api/legacy-lighting/daily?start=2024-08-01&end=2024-08-01&modules=bad').status_code==422
    data=io.BytesIO()
    with ZipFile(data,'w') as archive:archive.writestr('../escape.xml','bad')
    with ZipFile(data) as archive:
        with pytest.raises(ValueError):light._validate_archive(archive)


def test_real_serial_background_worker_completes(api,monkeypatch):
    import time
    monkeypatch.setattr(light,'schedule',REAL_SCHEDULE)
    folder=api.root/'history-imports';folder.mkdir()
    path=workbook(folder/'async.xlsx')
    with api.sessions() as db:
        row=HistoryImport(filename=path.name,sha256=light._checksum(path),raw_path=path.relative_to(api.root).as_posix(),status='uploaded',user_id=1)
        db.add(row);db.commit();history_id=row.id
    try:
        response=api.client.post(f'/api/legacy-lighting/from-history/{history_id}');assert response.status_code==200
        job_id=response.json()['id']
        def await_state(wanted):
            end=time.monotonic()+10
            while time.monotonic()<end:
                result=api.client.get(f'/api/legacy-lighting/{job_id}').json()
                assert result['status']!='failed',result
                if result['status']==wanted:return result
                time.sleep(.01)
            pytest.fail('Background job timed out')
        job=await_state('preview')
        payload={'confirmed':True,'reason':'Archive only','interpretation':{'date_system':'1900'}}
        assert api.client.post(f'/api/legacy-lighting/{job_id}/confirm',json=payload).status_code==200
        assert await_state('completed')['progress_rows']==288
    finally:light.shutdown_worker()


def test_changed_original_creates_new_version_and_revoke_restores_old(api):
    old,path=history(api,percentage=25);confirm(api,old);calibration(api)
    changed,new_path=history(api,percentage=50)
    assert changed['checksum']!=old['checksum']
    assert changed['version']==2 and changed['supersedes_id']==old['id']
    confirm(api,changed)
    assert snapshot(api)['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(5.184)
    assert api.client.post(f"/api/legacy-lighting/{changed['id']}/revoke").status_code==200
    assert snapshot(api)['days'][DAY.isoformat()][101]['mol_m2_day']==pytest.approx(2.592)
    assert path.exists() and new_path.exists()


def test_daily_storage_bound_stops_before_excess_rows(api,monkeypatch):
    job,_=history(api)
    monkeypatch.setattr(light,'MAX_DAILY',0)
    failed=confirm(api,job)
    assert failed['status']=='failed'
    with api.sessions() as db:
        assert db.scalar(select(func.count(light.LegacyLightingDay.id)))==0
    assert snapshot(api)['days']=={}


@pytest.mark.parametrize('end_value',[None,'invalid','2024-07-31'])
def test_invalid_primary_end_still_blocks_known_module_start_day(api,end_value):
    job,_=history(api);confirm(api,job);calibration(api)
    with api.sessions() as db:
        batch=ImportJob(user_id=1,status='completed');db.add(batch);db.flush()
        source=PrivaSource(job_id=batch.id,name='invalid.csv',path='imports/none.csv',checksum='invalid-end-source',
             profile='belichting',logical_key='invalid-end',version=1,activated_at=light.now())
        db.add(source);db.flush()
        row={'label':'Sum_24h_ModGrowlightSysHours','pcu':'SYNTHETIC','type_1':'modgroeilichtsturing',
            'idx_1':'101','type_2':'kanaal','idx_2':'1','startdate':'1-8-24','enddate':'not-a-date','value':''}
        item=PrivaValue(source_id=source.id,**normalize_row(row,1))
        item.local_date=DAY.isoformat();item.end_date=end_value;db.add(item);db.commit()
    assert snapshot(api)['days'][DAY.isoformat()][101]['quality']=='blocked_by_priva'


def test_invalid_primary_end_with_normalized_start_cleared_uses_raw_start(api):
    job,_=history(api);confirm(api,job);calibration(api)
    with api.sessions() as db:
        batch=ImportJob(user_id=1,status='completed');db.add(batch);db.flush()
        source=PrivaSource(job_id=batch.id,name='invalid.csv',path='imports/none.csv',checksum='invalid-raw-start',
             profile='belichting',logical_key='invalid-raw',version=1,activated_at=light.now())
        db.add(source);db.flush()
        row={'label':'Sum_24h_ModGrowlightSysHours','pcu':'SYNTHETIC','type_1':'modgroeilichtsturing',
            'idx_1':'101','type_2':'kanaal','idx_2':'1','startdate':'1-8-24','enddate':'bad','value':'100'}
        item=PrivaValue(source_id=source.id,**normalize_row(row,1));assert item.local_date is None
        db.add(item);db.commit()
    assert snapshot(api)['days'][DAY.isoformat()][101]['quality']=='blocked_by_priva'
