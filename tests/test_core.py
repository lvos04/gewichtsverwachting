import uuid
from datetime import date
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db import Base, get_db
from app.settings import SettingsVersion, AppSettings
from app.auth import router as auth_router
from app.core import router, seed_data
from app.models import AuditLog, Measurement, Stem

@pytest.fixture
def api(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with sessions() as db: seed_data(db)
    app=FastAPI(); app.include_router(auth_router); app.include_router(router)
    def db_override():
        with sessions() as db: yield db
    app.dependency_overrides[get_db]=db_override
    with TestClient(app) as client:
        yield client,sessions
    engine.dispose()

def test_setup_roles_origin_and_durable_rate_limit(api):
    c, sessions=api
    assert c.post('/api/auth/setup',json={'username':'owner','password':'very long password'},headers={'Origin':'https://evil.example'}).status_code==403
    assert c.post('/api/auth/setup',json={'username':'owner','password':'very long password'}).status_code==200
    assert c.post('/api/auth/setup',json={'username':'other','password':'very long password'}).status_code==409
    assert len(c.get('/api/bays').json())==40
    assert c.post('/api/users',json={'username':'reader','password':'another long password','role':'lezer'}).status_code==200
    assert c.post('/api/auth/logout').status_code==200
    assert c.post('/api/auth/login',json={'username':'reader','password':'another long password'}).status_code==200
    assert c.post('/api/varieties',json={'name':'x'}).status_code==403
    assert c.get('/api/users').status_code==403
    assert c.post('/api/auth/logout').status_code==200
    for _ in range(5): assert c.post('/api/auth/login',json={'username':'owner','password':'wrong'}).status_code==401
    assert c.post('/api/auth/login',json={'username':'owner','password':'very long password'}).status_code==429
    with sessions() as db: assert db.scalar(select(AuditLog).where(AuditLog.action=='login_failed')) is not None

def test_measurement_idempotence_planning_and_beds(api):
    c,sessions=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    v=c.get('/api/varieties').json()[0]['id']
    data={'round_name':'R1','bay_number':1,'variety_id':v,'beds':4,'status':'active'}
    cultivation=c.post('/api/cultivations',json=data); assert cultivation.status_code==200, cultivation.text
    cid=cultivation.json()['id']
    assert c.post('/api/cultivations',json={**data,'beds':3}).status_code==409
    assert c.post(f'/api/cultivations/{cid}/planning',json={'plant_date':'2026-11-01','light_out_date':'2026-11-03'}).status_code==200
    measurement={'client_id':str(uuid.uuid4()),'cultivation_id':cid,'kind':'KD10','measured_at':'2026-11-13T09:00:00','stems':[{'position':str(i),'weight_g':10+i,'length_cm':20+i} for i in range(5)]}
    first=c.post('/api/measurements',json=measurement); assert first.status_code==200, first.text
    assert first.json()['actual_kd_day']==10
    assert c.post('/api/measurements',json=measurement).json()['id']==first.json()['id']
    measurement['stems'][0]['weight_g']=99
    assert c.post('/api/measurements',json=measurement).status_code==409
    assert c.post(f'/api/cultivations/{cid}/planning',json={'light_out_date':'2026-11-04'}).status_code==422
    assert c.post(f'/api/cultivations/{cid}/planning',json={'light_out_date':'2026-11-04','reason':'correctie'}).status_code==200
    detail=c.get(f'/api/cultivations/{cid}').json()
    assert detail['measurements'][0]['actual_kd_day']==9
    assert detail['planning'][-1]['plant_date']=='2026-11-01'
    with sessions() as db:
        assert len(db.scalars(select(Measurement)).all())==1
        assert len(db.scalars(select(Stem)).all())==5

def test_mapping_versions_and_harvest_ambiguity(api):
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    old=next(m for m in c.get('/api/mappings').json() if m['bay_number']==1 and m['object_type']=='valve')
    new={'bay_number':1,'object_type':'valve','object_number':41,'valid_from':'2027-01-01','reason':'nieuwe installatie'}
    assert c.post('/api/mappings',json=new).status_code==409
    response=c.post('/api/mappings',json={**new,'replaces_id':old['id']})
    assert response.status_code==200, response.text
    previous=next(m for m in c.get('/api/mappings').json() if m['id']==old['id'])
    assert previous['valid_until']=='2026-12-31'
    v=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R2','bay_number':2,'variety_id':v,'beds':3}).json()['id']
    assert c.post(f'/api/cultivations/{cid}/harvest',json={'weight_g':80,'quality_classes':[{'weight_g':95},{'weight_g':65}]}).status_code==422
    assert c.post(f'/api/cultivations/{cid}/harvest',json={'quality_classes':[{'weight_g':95},{'weight_g':65}]}).status_code==200

def test_concurrent_offline_retries_and_reauthentication(api):
    from concurrent.futures import ThreadPoolExecutor
    c, sessions=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    v=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R1','bay_number':1,'variety_id':v,'beds':6}).json()['id']
    payload={'client_id':str(uuid.uuid4()),'cultivation_id':cid,'kind':'pilot','measured_at':'2026-11-13T09:00:00','stems':[{'position':str(i),'weight_g':10+i,'length_cm':20+i} for i in range(5)]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: c.post('/api/measurements',json=payload), range(2)))
    assert [r.status_code for r in results]==[200,200]
    assert results[0].json()['id']==results[1].json()['id']
    assert c.post('/api/auth/logout').status_code==200
    assert c.post('/api/measurements',json=payload).status_code==401
    assert c.post('/api/auth/login',json={'username':'owner','password':'very long password'}).status_code==200
    assert c.post('/api/measurements',json=payload).json()['id']==results[0].json()['id']
    with sessions() as db:
        assert len(db.scalars(select(Measurement)).all())==1
        assert len(db.scalars(select(Stem)).all())==5

def test_mixed_beds_history_and_restrictions(api):
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    vs=c.get('/api/varieties').json()
    base={'round_name':'mixed','bay_number':1,'beds':3,'status':'active'}
    one=c.post('/api/cultivations',json={**base,'variety_id':vs[0]['id']}).json()['id']
    assert c.post('/api/cultivations',json={**base,'variety_id':vs[1]['id']}).status_code==200
    assert c.post('/api/cultivations',json={**base,'variety_id':vs[2]['id'],'beds':1}).status_code==409
    assert c.post(f'/api/cultivations/{one}/planning',json={'plant_date':'2026-11-01','light_out_date':'2026-11-03'}).status_code==200
    assert c.post(f'/api/cultivations/{one}/planning',json={'plant_date':'2026-11-01','light_out_date':'2026-11-05','reason':'correctie'}).status_code==200
    plans=c.get(f'/api/cultivations/{one}').json()['planning']
    assert [p['light_out_date'] for p in plans]==['2026-11-03','2026-11-05']
    assert c.post(f'/api/cultivations/{one}/events',json={'kind':'alar','occurred_at':'2026-12-01T09:00:00','amount':10}).status_code==422
    assert c.post('/api/varieties',json={'name':'baltazar'},headers={'Origin':'https://evil.example'}).status_code==403
    mapping={'bay_number':1,'object_type':'valve','object_number':1,'valid_from':'2026-11-17','reason':'overlap'}
    assert c.post('/api/mappings',json=mapping).status_code==409
    c.post('/api/users',json={'username':'reader','password':'another long password','role':'lezer'})
    c.post('/api/auth/logout')
    c.post('/api/auth/login',json={'username':'reader','password':'another long password'})
    assert c.get(f'/api/cultivations/{one}').status_code==200
    assert c.post(f'/api/cultivations/{one}/events',json={'kind':'alar','occurred_at':'2026-12-01T09:00:00'}).status_code==403
    assert c.patch('/api/bays/1',json={'area_m2':100}).status_code==403

def test_dashboard_tasks_use_real_plans_and_follow_same_bay(api):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    today=datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    v=c.get('/api/varieties').json()[0]['id']
    ids=[]
    for bay in (1,2):
        cid=c.post('/api/cultivations',json={'round_name':'R1','bay_number':bay,'variety_id':v,'beds':6,'status':'active'}).json()['id']
        ids.append(cid)
        c.post(f'/api/cultivations/{cid}/planning',json={'light_out_date':(today-timedelta(days=10)).isoformat()})
    tasks=c.get('/api/dashboard').json()['tasks']
    assert len([t for t in tasks if t['kind']=='KD10'])==1
    assert tasks[0]['cultivation_id']==ids[0]
    payload={'client_id':str(uuid.uuid4()),'cultivation_id':ids[0],'kind':'KD10','measured_at':f'{today}T09:00:00','stems':[{'position':str(i),'weight_g':10+i,'length_cm':20+i} for i in range(5)]}
    assert c.post('/api/measurements',json=payload).status_code==200
    c.post(f'/api/cultivations/{ids[0]}/planning',json={'light_out_date':(today-timedelta(days=30)).isoformat(),'reason':'correctie'})
    tasks=c.get('/api/dashboard').json()['tasks']
    assert not any(t['kind']=='KD10' for t in tasks)
    assert any(t['kind']=='KD30' and t['cultivation_id']==ids[0] for t in tasks)

def test_nonfinite_and_cultivation_status(api):
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    result=c.post('/api/varieties',content='{"name":"x","target_weight_g":1e999}',headers={'Content-Type':'application/json'})
    assert result.status_code==422
    v=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':v,'beds':6}).json()['id']
    assert c.post(f'/api/cultivations/{cid}/planning',content='{"density":1e999}',headers={'Content-Type':'application/json'}).status_code==422
    assert c.post(f'/api/cultivations/{cid}/harvest',content='{"quality_classes":[{"weight_g":1e999}]}',headers={'Content-Type':'application/json'}).status_code==422
    assert c.patch(f'/api/cultivations/{cid}',json={'status':'active'}).json()['status']=='active'
    assert c.patch(f'/api/cultivations/{cid}',json={'status':'harvested'}).status_code==422
    assert c.patch(f'/api/cultivations/{cid}',json={'status':'historical'}).status_code==200
    c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':v,'beds':6})
    assert c.patch(f'/api/cultivations/{cid}',json={'status':'active'}).status_code==409
    assert c.post('/api/mappings',json={'bay_number':1,'object_type':'department','object_number':5,'valid_from':'2028-01-01','reason':'x'}).status_code==422

def test_cultivation_revisions_preserve_retroactive_changes(api):
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    v=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':v,'beds':6,'category':'standard','label_quality':'verified'}).json()['id']
    assert c.patch(f'/api/cultivations/{cid}',json={'category':'adjusted','label_quality':'ambiguous','status':'active'}).status_code==200
    assert c.post(f'/api/cultivations/{cid}/harvest',json={'weight_g':100}).status_code==200
    revisions=c.get(f'/api/cultivations/{cid}').json()['revisions']
    assert [(r['category'],r['label_quality'],r['status']) for r in revisions]==[('standard','verified','planned'),('adjusted','ambiguous','active'),('adjusted','ambiguous','harvested')]
    assert revisions[0]['created_at']<=revisions[1]['created_at']<=revisions[2]['created_at']

def test_effective_settings_drive_login_and_measurement_tolerance(api):
    from datetime import datetime, timezone
    c,sessions=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    with sessions() as db:
        cfg=AppSettings(session_hours=2,measurement_tolerance_days=3,steering_until_kd=29,login_max_attempts=3,login_window_minutes=60)
        db.add(SettingsVersion(settings=cfg.model_dump(),effective_from=datetime.now(timezone.utc).replace(tzinfo=None),reason='test',user_id=1))
        db.commit()
    v=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':v,'beds':6}).json()['id']
    c.post(f'/api/cultivations/{cid}/planning',json={'light_out_date':'2026-11-01'})
    data={'client_id':str(uuid.uuid4()),'cultivation_id':cid,'kind':'KD10','measured_at':'2026-11-14T09:00:00','stems':[{'position':str(i),'weight_g':10+i,'length_cm':20+i} for i in range(5)]}
    assert c.post('/api/measurements',json=data).json()['timing_warning'] is False
    assert c.get('/api/dashboard').json()['steering_until_kd']==29
    c.post('/api/auth/logout')
    for _ in range(3): assert c.post('/api/auth/login',json={'username':'owner','password':'wrong'}).status_code==401
    assert c.post('/api/auth/login',json={'username':'owner','password':'very long password'}).status_code==429
    # Independent account and IP cannot bypass the source limit.
    assert c.post('/api/auth/login',json={'username':'other','password':'wrong'}).status_code==429
    with sessions() as db:
        from app.models import LoginAttempt
        db.query(LoginAttempt).delete(); db.commit()
    r=c.post('/api/auth/login',json={'username':'owner','password':'very long password'})
    assert r.status_code==200
    assert 'Max-Age=7200' in r.headers['set-cookie']

def test_inactive_varieties_remain_available_for_history(api):
    c,_=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    created=c.post('/api/varieties',json={'name':'baltazar','target_weight_g':110,'active':False})
    assert created.status_code==200 and created.json()['active'] is False
    vid=created.json()['id']
    base={'round_name':'R','bay_number':1,'variety_id':vid,'beds':6}
    assert c.post('/api/cultivations',json=base).status_code==422
    historical=c.post('/api/cultivations',json={**base,'status':'historical'})
    assert historical.status_code==200
    assert any(v['id']==vid and v['active'] is False for v in c.get('/api/varieties').json())
    assert c.patch(f'/api/varieties/{vid}',json={'active':True}).json()['target_weight_g']==110
    current=c.post('/api/cultivations',json={**base,'status':'active'})
    assert current.status_code==200
    assert c.patch(f'/api/varieties/{vid}',json={'active':False}).status_code==200
    assert c.get(f"/api/cultivations/{current.json()['id']}").status_code==200
    assert c.patch(f'/api/varieties/{vid}',json={'target_weight_g':111}).json()['active'] is False

def test_variety_target_versions_and_future_harvest_dates(api):
    from datetime import datetime, timezone, timedelta
    from app.core import target_at
    c,sessions=api
    c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
    vid=c.post('/api/varieties',json={'name':'nieuw','target_weight_g':90}).json()['id']
    with sessions() as db:
        from app.models import VarietyTargetVersion
        revision=db.scalar(select(VarietyTargetVersion).where(VarietyTargetVersion.variety_id==vid))
        revision.created_at=datetime(2020,1,1); db.commit()
        assert target_at(db,vid,datetime(2019,12,31)) is None
        assert target_at(db,vid,datetime(2020,1,2)).target_weight_g==90
    assert c.patch(f'/api/varieties/{vid}',json={'target_weight_g':110}).status_code==200
    assert c.patch(f'/api/varieties/{vid}',json={'active':False}).status_code==200
    versions=c.get(f'/api/varieties/{vid}/targets').json()
    assert [v['target_weight_g'] for v in versions]==[110,90]
    with sessions() as db:
        assert target_at(db,vid,datetime(2021,1,1)).target_weight_g==90
        assert target_at(db,vid,datetime.now(timezone.utc)).target_weight_g==110
    c.patch(f'/api/varieties/{vid}',json={'active':True})
    cid=c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':vid,'beds':6}).json()['id']
    from zoneinfo import ZoneInfo
    tomorrow=(datetime.now(ZoneInfo('Europe/Amsterdam')).date()+timedelta(days=1)).isoformat()
    assert c.post(f'/api/cultivations/{cid}/harvest',json={'start_date':tomorrow,'weight_g':100}).status_code==422
    assert c.post(f'/api/cultivations/{cid}/harvest',json={'end_date':tomorrow,'weight_g':100}).status_code==422
    assert c.post(f'/api/cultivations/{cid}/planning',json={'expected_harvest_date':tomorrow}).status_code==200
