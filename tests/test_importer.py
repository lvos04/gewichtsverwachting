"""Synthetic Priva exports only. No farm originals are included in test artifacts."""
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app import importer as imp
from app.auth import router as auth_router
from app.core import seed_data
from app.settings import router as settings_router
from app.models import AuditLog
from app.db import Base, get_db

REAL_SCHEDULE=imp.schedule


def csv_bytes(rows):
    header=';'.join(imp.HEADERS)+'\n'
    return (header+'\n'.join(';'.join(str(x) for x in r) for r in rows)+'\n').encode()


def row(label='Ave_24h_CompTemp',number=1,value='20,5',day='16-11-26',kind='Afdeling',subtype='',channel=''):
    return (label,'SYNTHETIC',kind,number,subtype,channel,day,day,value)


@pytest.fixture
def api(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with sessions() as db:
        seed_data(db)
    monkeypatch.setattr(imp,'SessionLocal',sessions)
    monkeypatch.setattr(imp,'DATA_DIR',tmp_path)
    monkeypatch.setattr(imp,'ROOT',tmp_path/'originals')
    monkeypatch.setattr(imp,'schedule',imp.run_job)
    imp._stop.clear()
    app=FastAPI(); app.include_router(auth_router); app.include_router(imp.router); app.include_router(settings_router)
    def override_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db]=override_db
    with TestClient(app) as client:
        client.post('/api/auth/setup',json={'username':'owner','password':'long synthetic password'})
        yield client,sessions,tmp_path
    engine.dispose()


def upload(c,rows,name='synthetic.csv'):
    response=c.post('/api/imports',files=[('files',(name,csv_bytes(rows),'text/csv'))])
    assert response.status_code==200,response.text
    return c.get(f"/api/imports/{response.json()['id']}").json()


def confirmed(c,rows,name='synthetic.csv'):
    job=upload(c,rows,name)
    assert job['status']=='preview',job
    response=c.post(f"/api/imports/{job['id']}/confirm")
    assert response.status_code==200,response.text
    job=c.get(f"/api/imports/{job['id']}").json()
    assert job['status']=='completed',job
    return job


def test_profiles_contents_dates_and_unknown_units(tmp_path):
    cases=[('kranen',[row('Sum_24h_ValveUse',41,'5',kind='Kraan')]),
           ('belichting',[row('Ave_24h_ModGrowlightPAR',101,'0','10-11-25','modgroeilichtsturing','kanaal','2')]),
           ('algemeen_afdeling',[row('Sum_24h_VgUse',1,'1',kind='Kraangroep')]),
           ('algemeen_bedrijf',[row(number=1),row(number=2)]),
           ('klimaat',[row()])]
    for expected,rows in cases:
        path=tmp_path/'misleading-name-week-46-2026.csv'; path.write_bytes(csv_bytes(rows))
        report=imp.inspect_file(path,path.name)
        assert report['profile']==expected
        assert report['years']==([2025] if expected=='belichting' else [2026])
        assert report['quality']['unknown_unit']==len(rows)
    with pytest.raises(ValueError): imp.parse_priva_date('1-1-99')
    with pytest.raises(ValueError): imp.parse_priva_date('31-2-26')
    with pytest.raises(ValueError): imp.parse_priva_date('25-10-26 02:30')
    assert imp.parse_priva_date('1-7-26').utcoffset().total_seconds()==7200
    assert imp.parse_priva_date('1-12-26').utcoffset().total_seconds()==3600


def test_quality_preserves_raw_and_actual_years(api):
    c,sessions,_=api
    rows=[row('Ave_24h_CompCO2',1,'0'),row('Ave_24h_CompCO2',3,'0'),row('Ave_24h_CompCO2',2,'850'),
          row('Sum_24h_MeasuredPAR',1,'0'),row(value=''),row(value='bad'),row(day='31-2-26')]
    job=confirmed(c,rows)
    assert job['preview']['quality']['untrusted_co2_zero']==2
    assert job['preview']['quality']['invalid_date']==1
    with sessions() as db:
        values=db.scalars(select(imp.PrivaValue)).all()
        assert len(values)==len(rows)
        assert values[0].value is None and values[0].original_value=='0'
        assert json.loads(values[0].raw_json)['value']=='0'
        assert values[2].value==850
        source=db.get(imp.PrivaSource,values[0].source_id)
        assert imp.original_path(source.path).read_bytes()==csv_bytes(rows)
    climate=c.get('/api/climate?start=2026-11-16&end=2026-11-16').json()
    assert climate['derived']['total_dli'] is None
    assert climate['quality']['untrusted_co2_zero']==2
    lighting=confirmed(c,[row('Ave_24h_ModGrowlightPAR',101,'80','10-11-25','modgroeilichtsturing','kanaal','1')])
    assert lighting['preview']['files'][0]['years']==[2025]
    climate=c.get('/api/climate?start=2026-11-16&end=2026-11-16').json()
    assert all(s['object_type']!='light' for s in climate['series'])


def test_preview_idempotence_correction_revoke_and_audit(api):
    c,sessions,_=api
    job=upload(c,[row()])
    with sessions() as db: assert db.scalar(select(func.count(imp.PrivaValue.id)))==0
    c.post(f"/api/imports/{job['id']}/confirm")
    same=confirmed(c,[row()])
    assert same['sources'][0]['duplicate'] is True
    corrected=confirmed(c,[row(value='21,5')])
    assert corrected['sources'][0]['version']==2
    assert corrected['preview']['files'][0]['corrects_source_id']
    def temperature():
        data=c.get('/api/climate?start=2026-11-16&end=2026-11-16').json()
        return data['series'][0]['dates'][0]['value']
    assert temperature()==21.5
    c.post(f"/api/imports/{same['id']}/revoke")
    assert temperature()==21.5
    c.post(f"/api/imports/{corrected['id']}/revoke")
    assert temperature()==20.5
    with sessions() as db:
        assert db.scalar(select(func.count(imp.PrivaValue.id)))==2
        assert db.scalar(select(func.count(imp.PrivaSource.id)))==2
        assert all(imp.original_path(s.path).exists() for s in db.scalars(select(imp.PrivaSource)).all())


def test_zip_nested_paths_and_traversal(api):
    c,sessions,tmp=api
    def send_zip(name):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(name,csv_bytes([row()]))
        r=c.post('/api/imports',files=[('files',('test.zip',data.getvalue(),'application/zip'))])
        assert r.status_code==200
        return c.get(f"/api/imports/{r.json()['id']}").json()
    good=send_zip('2026/november/data.csv')
    assert good['status']=='preview'
    assert len(good['sources'])==2  # untouched zip + normalized CSV original
    bad=send_zip('../../escape.csv')
    assert bad['status']=='failed' and 'Onveilig' in bad['error']
    assert not (tmp/'escape.csv').exists()


def test_durable_batch_resume_and_recovery(api,monkeypatch):
    c,sessions,_=api
    rows=[row(value=str(i)) for i in range(510)]
    job=upload(c,rows)
    write=imp._write_batch
    failed=False
    def interrupted(db,job,source,batch):
        nonlocal failed
        write(db,job,source,batch)
        if not failed:
            failed=True
            raise RuntimeError('Simulated process interruption')
    monkeypatch.setattr(imp,'_write_batch',interrupted)
    c.post(f"/api/imports/{job['id']}/confirm")
    failed_job=c.get(f"/api/imports/{job['id']}").json()
    assert failed_job['status']=='failed'
    with sessions() as db:
        assert db.scalar(select(func.count(imp.PrivaValue.id)))==500
        assert not imp.active_sources(db)
    monkeypatch.setattr(imp,'_write_batch',write)
    with sessions() as db:
        j=db.get(imp.ImportJob,job['id']); j.status='importing'; db.commit()
    imp.recover_jobs()
    assert c.get(f"/api/imports/{job['id']}").json()['status']=='completed'
    with sessions() as db:
        assert db.scalar(select(func.count(imp.PrivaValue.id)))==510
        assert len(imp.active_sources(db))==1


def test_explicit_priority_and_date_valid_mapping(api):
    c,_,_=api
    confirmed(c,[row(value='10',day='15-11-26'),row(value='10')])
    confirmed(c,[row(value='22',day='15-11-26'),row(value='22'),row(number=2,value='23')])
    data=c.get('/api/climate?start=2026-11-15&end=2026-11-16').json()
    temps=[s for s in data['series'] if s['object_number']==1]
    assert [x['value'] for x in temps[0]['dates']]==[22,22]
    assert data['quality']['source_conflicts']==2
    historical=c.get('/api/climate?start=2026-11-15&end=2026-11-15&bay_number=1').json()
    assert historical['mapping_uncertain'] is True
    assert not historical['series']
    current=c.get('/api/climate?start=2026-11-16&end=2026-11-16&bay_number=1').json()
    assert current['mapping_uncertain'] is False
    assert current['series'][0]['dates'][0]['value']==22


def test_valves_41_45_remain_unmapped(api):
    c,_,_=api
    job=confirmed(c,[row('Sum_24h_ValveUse',41,'100',kind='Kraan'),row('Sum_24h_ValveUse',1,'10',kind='Kraan')])
    assert job['preview']['quality']['unmapped_valve']==1
    all_data=c.get('/api/climate?start=2026-11-16&end=2026-11-16').json()
    assert {s['object_number'] for s in all_data['series']}=={1,41}
    bay=c.get('/api/climate?start=2026-11-16&end=2026-11-16&bay_number=1').json()
    assert [s['object_number'] for s in bay['series']]==[1]


def test_date_valid_calibrations_enable_only_verified_dli(api):
    c,_,_=api
    confirmed(c,[row('Sum_24h_ModGrowlightSysHours',n,'21600',kind='modgroeilichtsturing',subtype='kanaal',channel='1') for n in (1,101)])
    confirmed(c,[row('Sum_24h_Radiation',1,'1500',kind='Straling'),row(number=1),row(number=2)])
    query='/api/climate?start=2026-11-16&end=2026-11-16&bay_number=1'
    assert c.get(query).json()['derived']['lamp_dli'] is None
    def calibration(kind,value,**scope):
        payload=dict(kind=kind,valid_from='2026-11-16',value=value,reason='Synthetic confirmed instrument check',confirmed=True,**scope)
        result=c.post('/api/calibrations',json=payload)
        assert result.status_code==200,result.text
    calibration('unit',{'unit':'s'},label='Sum_24h_ModGrowlightSysHours',object_type='light')
    for n,ppfd in ((1,80),(101,120)):
        calibration('lamp',{'ppfd':ppfd,'dim_factor':1,'hours_label':'Sum_24h_ModGrowlightSysHours','time_unit':'s','independent_contribution':True},object_type='light',object_number=n,channel='1')
    derived=c.get(query).json()['derived']
    assert derived['lamp_dli']==pytest.approx(4.32)
    assert derived['daylight_dli'] is None and derived['total_dli'] is None
    calibration('unit',{'unit':'J/cm2'},label='Sum_24h_Radiation',object_type='radiation')
    calibration('transmission',{'factor':0.7},object_type='bay',object_number=1)
    assert c.get(query).json()['derived']['daylight_dli'] is None  # no unknown daily screen multiplier
    calibration('transmission',{'factor':0.7,'no_screen_effect':True},object_type='bay',object_number=1)
    derived=c.get(query).json()['derived']
    assert derived['daylight_dli']==pytest.approx(22.575)
    assert derived['total_dli']==pytest.approx(26.895)
    assert derived['quality']=='estimate'
    assert len(c.get('/api/calibrations').json())==6
    # New future-only calibration cannot change historical results.
    payload=dict(kind='unit',valid_from='2026-11-17',value={'unit':'unknown'},label='Sum_24h_ModGrowlightSysHours',object_type='light',reason='Next day instrument change',confirmed=True)
    assert c.post('/api/calibrations',json=payload).status_code==200
    assert c.get(query).json()['derived']['lamp_dli']==pytest.approx(4.32)
    assert c.post('/api/calibrations',json={**payload,'confirmed':False}).status_code==422


def test_portable_relative_originals_and_checksum_integrity(api,monkeypatch):
    import shutil
    c,sessions,tmp=api
    job=upload(c,[row()])
    with sessions() as db:
        original=db.scalar(select(imp.ImportFile).where(imp.ImportFile.job_id==job['id']))
        assert not Path(original.path).is_absolute()
        stored=original.path
    restored=tmp/'restored-dataset'
    shutil.copytree(tmp/'originals',restored/'originals')
    shutil.rmtree(tmp/'originals')
    monkeypatch.setattr(imp,'DATA_DIR',restored)
    monkeypatch.setattr(imp,'ROOT',restored/'originals')
    assert imp.original_path(stored).read_bytes()==csv_bytes([row()])
    assert c.post(f"/api/imports/{job['id']}/confirm").status_code==200
    assert c.get(f"/api/imports/{job['id']}").json()['status']=='completed'
    changed=upload(c,[row(value='21')])
    with sessions() as db:
        f=db.scalar(select(imp.ImportFile).where(imp.ImportFile.job_id==changed['id']))
        imp.original_path(f.path).write_bytes(csv_bytes([row(value='22')]))
    c.post(f"/api/imports/{changed['id']}/confirm")
    error=c.get(f"/api/imports/{changed['id']}").json()
    assert error['status']=='failed' and 'Checksum' in error['error']


def test_explicit_reimport_after_revocation_preserves_old_source(api):
    c,sessions,_=api
    original=confirmed(c,[row()])
    assert c.post(f"/api/imports/{original['id']}/revoke").status_code==200
    assert c.get('/api/climate?start=2026-11-16&end=2026-11-16').json()['series']==[]
    restored=confirmed(c,[row()])
    assert restored['sources'][0]['version']==2
    with sessions() as db:
        sources=db.scalars(select(imp.PrivaSource).order_by(imp.PrivaSource.id)).all()
        assert len(sources)==2 and sources[0].revoked_at is not None
        assert sources[0].checksum==sources[1].checksum
        assert len(imp.active_sources(db))==1


def test_upload_limit_counts_content_length_and_chunked_transfer():
    app=FastAPI(); app.include_router(imp.router)
    app.add_middleware(imp.ImportUploadLimitMiddleware,max_bytes=1024)
    with TestClient(app) as client:
        assert client.post('/api/imports',files=[('files',('large.csv',b'x'*2000,'text/csv'))]).status_code==413
        chunks=iter([b'--bound\r\nContent-Disposition: form-data; name="files"; filename="large.csv"\r\n\r\n',b'x'*2000,b'\r\n--bound--\r\n'])
        response=client.post('/api/imports',content=chunks,headers={'Content-Type':'multipart/form-data; boundary=bound'})
        assert response.status_code==413,response.text


def test_real_background_worker_preview_and_confirmation(api,monkeypatch):
    import time
    c,_,_=api
    monkeypatch.setattr(imp,'schedule',REAL_SCHEDULE)
    try:
        response=c.post('/api/imports',files=[('files',('async.csv',csv_bytes([row()]),'text/csv'))])
        assert response.status_code==200
        job_id=response.json()['id']
        def await_status(wanted):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                job=c.get(f'/api/imports/{job_id}').json()
                assert job['status']!='failed',job
                if job['status']==wanted:
                    return job
                time.sleep(0.01)
            pytest.fail(f'Worker did not reach {wanted}')
        await_status('preview')
        assert c.post(f'/api/imports/{job_id}/confirm').status_code==200
        assert await_status('completed')['progress_rows']==1
    finally:
        imp.shutdown_worker()


def test_configured_priority_changes_resolution_and_preserves_preview_audit(api):
    c,sessions,_=api
    confirmed(c,[row(value='20'),row(value='21',day='17-11-26')])
    confirmed(c,[row(value='30'),row(number=2,value='31'),row(value='',day='17-11-26'),row(number=2,value='31',day='17-11-26')])
    # This preview captures the original policy before settings change.
    pending=upload(c,[row('Ave_24h_CompHD',value='7')])
    assert pending['preview']['priorities']['algemeen_bedrijf']==50
    old_version=pending['preview']['priority_settings_version_id']
    query='/api/climate?start=2026-11-16&end=2026-11-17'
    def department_one_temperatures(data):
        series=next(s for s in data['series'] if s['label']=='Ave_24h_CompTemp' and s['object_number']==1)
        return [point['value'] for point in series['dates']]
    assert department_one_temperatures(c.get(query).json())==[30,21]
    with sessions() as db:
        before=[(r.id,r.value,r.raw_json) for r in db.scalars(select(imp.PrivaValue).order_by(imp.PrivaValue.id))]
    changed=c.post('/api/settings',json={'settings':{'priority_klimaat':90,'priority_algemeen_bedrijf':5},'reason':'Verified UI source selection preference'})
    assert changed.status_code==200,changed.text
    data=c.get(query).json()
    assert department_one_temperatures(data)==[20,21]
    assert data['priorities']['klimaat']==90
    assert data['priority_settings_version_id']==changed.json()['id']
    with sessions() as db:
        assert before==[(r.id,r.value,r.raw_json) for r in db.scalars(select(imp.PrivaValue).order_by(imp.PrivaValue.id))]
    assert c.post(f"/api/imports/{pending['id']}/confirm").status_code==200
    with sessions() as db:
        log=db.scalar(select(AuditLog).where(AuditLog.action=='import_confirm',AuditLog.entity==str(pending['id'])))
        assert log.details['priorities']==pending['preview']['priorities']
        assert log.details['priority']==pending['preview']['source_priority']
        assert log.details['settings_version_id']==old_version
    # New previews use the changed policy, while earlier previews remain intact.
    new_preview=upload(c,[row('Ave_24h_CompRV',value='75')])
    assert new_preview['preview']['priorities']['klimaat']==90
    assert new_preview['preview']['priority_settings_version_id']==changed.json()['id']
    assert c.get(f"/api/imports/{pending['id']}").json()['preview']['priorities']['algemeen_bedrijf']==50
