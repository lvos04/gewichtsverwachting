from datetime import datetime, date
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db import Base, get_db
from app.auth import router as auth_router
from app.core import router as core_router, seed_data
from app.crop_profiles import router as profile_router, in_season, interpolate
from app.operations import router as operations_router
from app.models import CropProfileVersion, MappingRevision, PlanningVersion, Measurement

@pytest.fixture
def api(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with sessions() as db: seed_data(db)
    app=FastAPI(); app.include_router(auth_router); app.include_router(core_router); app.include_router(profile_router); app.include_router(operations_router)
    def override():
        with sessions() as db: yield db
    app.dependency_overrides[get_db]=override
    with TestClient(app) as c:
        c.post('/api/auth/setup',json={'username':'owner','password':'very long password'})
        yield c,sessions
    engine.dispose()

def test_season_wrap_leap_and_interpolation():
    assert in_season(date(2024,2,29),'10-01','03-01')
    assert in_season(date(2025,1,1),'10-01','03-01')
    assert not in_season(date(2025,7,1),'10-01','03-01')
    curve=[{'kd_day':10,'length_cm':20},{'kd_day':30,'length_cm':60}]
    assert interpolate(curve,20)==40
    assert interpolate(curve,9) is None and interpolate(curve,31) is None

def test_profile_versions_asof_no_future_leak_and_cards(api):
    c,sessions=api
    vid=c.get('/api/varieties').json()[0]['id']
    cid=c.post('/api/cultivations',json={'round_name':'R','bay_number':1,'variety_id':vid,'beds':6}).json()['id']
    plan=c.post(f'/api/cultivations/{cid}/planning',json={'plant_date':'2020-12-01','light_out_date':'2020-12-10','interlight_description':'<licht>','planned_stems':1200,'planned_lamp_hours':12}).json()
    profile={'variety_id':vid,'season_start':'10-01','season_end':'03-01','valid_from':'2020-01-01','source':'eigen curve','length_curve':[{'kd_day':10,'length_cm':20},{'kd_day':30,'length_cm':60}]}
    old=c.post('/api/crop-profiles',json=profile); assert old.status_code==200, old.text
    assert c.post('/api/crop-profiles',json=profile).status_code==409
    new=c.post('/api/crop-profiles',json={**profile,'length_curve':[{'kd_day':10,'length_cm':30},{'kd_day':30,'length_cm':70}],'supersedes_id':old.json()['id']}); assert new.status_code==200, new.text
    body={'client_id':str(uuid.uuid4()),'cultivation_id':cid,'kind':'KD10','measured_at':'2020-12-20T09:00:00','stems':[{'position':str(i),'weight_g':10,'length_cm':25} for i in range(5)]}
    measurement_id=c.post('/api/measurements',json=body).json()['id']
    late=c.post('/api/measurements',json={**body,'client_id':str(uuid.uuid4()),'measured_at':'2021-01-02T09:00:00'}).json()['id']
    with sessions() as db:
        db.get(Measurement,measurement_id).created_at=datetime(2020,12,20,10)
        db.get(Measurement,late).created_at=datetime(2020,12,20,10)
        db.get(PlanningVersion,plan['id']).created_at=datetime(2020,1,1)
        db.get(CropProfileVersion,old.json()['id']).created_at=datetime(2020,1,1)
        db.get(CropProfileVersion,new.json()['id']).created_at=datetime(2022,1,1)
        db.commit()
    first=c.get(f'/api/cultivations/{cid}/growth-target',params={'as_of':'2021-01-01T12:00:00Z'}).json()
    assert first['profile']['id']==old.json()['id'] and first['target_kd10_cm']==20 and first['target_kd30_cm']==60
    assert len(first['comparisons'])==1 and first['comparisons'][0]['difference_cm']==5
    latest=c.get(f'/api/cultivations/{cid}/growth-target',params={'as_of':'2023-01-01T12:00:00Z'}).json()
    assert latest['profile']['id']==new.json()['id'] and latest['target_kd10_cm']==30
    card=c.get('/api/cards',params={'ids':str(cid)}); assert card.status_code==200
    assert 'Streeflengte KD10: 30' in card.text and '&lt;licht&gt;' in card.text and '1200' in card.text
    assert 'planning_version' not in card.text  # The version is encoded in the QR image.
    revised=c.post(f'/api/cultivations/{cid}/planning',json={'reason':'correctie','density':5}).json()
    assert revised['interlight_description']=='<licht>' and revised['planned_stems']==1200

def test_role_validation_and_mapping_history(api):
    c,sessions=api
    vid=c.get('/api/varieties').json()[0]['id']
    p={'variety_id':vid,'season_start':'02-30','season_end':'03-01','valid_from':'2020-01-01','source':'proef','length_curve':[{'kd_day':10,'length_cm':20}]}
    assert c.post('/api/crop-profiles',json=p).status_code==422
    old=next(m for m in c.get('/api/mappings').json() if m['bay_number']==1 and m['object_type']=='valve')
    with sessions() as db:
        initial=db.scalars(select(MappingRevision).where(MappingRevision.mapping_id==old['id'])).all()
        assert len(initial)==1 and initial[0].valid_until is None
    change={'bay_number':1,'object_type':'valve','object_number':41,'valid_from':'2027-01-01','reason':'wijziging','replaces_id':old['id']}
    assert c.post('/api/mappings',json=change).status_code==200
    with sessions() as db:
        versions=db.scalars(select(MappingRevision).where(MappingRevision.mapping_id==old['id']).order_by(MappingRevision.id)).all()
        assert [v.valid_until for v in versions]==[None,date(2026,12,31)]
    c.post('/api/users',json={'username':'reader','password':'another long password','role':'lezer'}); c.post('/api/auth/logout'); c.post('/api/auth/login',json={'username':'reader','password':'another long password'})
    assert c.post('/api/crop-profiles',json={**p,'season_start':'02-28'}).status_code==403

def test_profile_without_invented_curve(api):
    c,_=api
    vid=c.get('/api/varieties').json()[0]['id']
    base={'variety_id':vid,'season_start':'01-01','season_end':'12-31','valid_from':'2020-01-01','source':'getest doel'}
    assert c.post('/api/crop-profiles',json=base).status_code==422
    target=c.post('/api/crop-profiles',json={**base,'target_weight_g':110,'length_curve':[]})
    assert target.status_code==200 and target.json()['length_curve']==[]
    assert interpolate(target.json()['length_curve'],10) is None
