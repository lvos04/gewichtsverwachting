"""Synthetic adversarial temporal tests; no farm data are checked in."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import environment as env
from app.db import Base, get_db
from app.auth import require_user
from app.importer import Calibration, ImportJob, PrivaSource, PrivaValue, normalize_row
from app.models import Bay, Cultivation, MappingRevision, MappingVersion, PlanningVersion, User, Variety
from app.settings import AppSettings, SettingsVersion

NL=ZoneInfo('Europe/Amsterdam')


def at(day,hour=0,month=11,year=2026):
    return datetime(year,month,day,hour,tzinfo=NL).astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def context():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as db:
        db.add_all([User(id=1,username='synthetic',password_hash='x',role='admin'),Bay(number=1,department=1,beds=6),Variety(id=1,name='synthetic',target_weight_g=105)])
        db.flush()
        crop=Cultivation(round_name='R-synthetic',bay_number=1,variety_id=1,beds=6,status='active')
        db.add(crop); db.flush()
        plan=PlanningVersion(cultivation_id=crop.id,plant_date=date(2026,11,16),light_out_date=date(2026,11,17),created_at=at(15),author_id=1)
        db.add(plan); db.flush()
        mappings=[]
        for kind,number in [('department',1),('valve',1),('light',1),('light',101)]:
            m=MappingVersion(bay_number=1,object_type=kind,object_number=number,valid_from=date(2026,11,16),created_at=at(15),reason='Synthetic confirmed mapping')
            db.add(m); db.flush()
            r=MappingRevision(mapping_id=m.id,bay_number=1,object_type=kind,object_number=number,valid_from=m.valid_from,created_at=at(15),reason=m.reason)
            db.add(r); mappings.append(r)
        db.commit()
        yield SimpleNamespace(db=db,crop=crop,plan=plan,mappings=mappings)
    engine.dispose()


def calibration(ctx,kind,value,created=None,**scope):
    record=Calibration(kind=kind,value_json=json.dumps(value),valid_from=date(2026,11,16),
         created_at=created or at(15),reason='Synthetic confirmed calibration',confirmed=True,user_id=1,**scope)
    ctx.db.add(record); ctx.db.flush()
    return record


def calibrate_all(ctx,created=None,co2=True):
    records=[]
    for label,unit in [('Ave_24h_CompTemp','°C'),('Ave_24h_CompRV','%'),('Sum_24h_Valve/m2','L/m2'),('Sum_24h_ModGrowlightSysHours','s')]+([('Ave_24h_CompCO2','ppm')] if co2 else []):
        records.append(calibration(ctx,'unit',{'unit':unit},created,label=label))
    for number,ppfd in [(1,80),(101,120)]:
        records.append(calibration(ctx,'lamp',{'ppfd':ppfd,'dim_factor':1,'hours_label':'Sum_24h_ModGrowlightSysHours','time_unit':'s','independent_contribution':True},created,object_type='light',object_number=number,channel='1'))
    ctx.db.commit()
    return records


def source(ctx,rows,profile='klimaat',activated=None,key=None,version=1,revoked=None):
    db=ctx.db
    job=ImportJob(user_id=1,status='completed',created_at=at(15),confirmed_at=at(15)); db.add(job); db.flush()
    value=PrivaSource(job_id=job.id,name='synthetic.csv',path='imports/synthetic.csv',checksum=f'synthetic-{job.id}',
          profile=profile,logical_key=key or f'key-{job.id}',version=version,created_at=at(15),activated_at=activated or at(20),revoked_at=revoked)
    db.add(value); db.flush()
    for n,row in enumerate(rows,1):
        raw={'label':row[0],'pcu':'SYNTHETIC','type_1':row[1],'idx_1':str(row[2]),'type_2':row[3],
             'idx_2':row[4],'startdate':row[5],'enddate':row[5],'value':row[6]}
        db.add(PrivaValue(source_id=value.id,**normalize_row(raw,n)))
    db.commit()
    return value


def daily_rows(days=(16,17,18,19),temp='20',co2='0',water='0'):
    rows=[]
    for d in days:
        day=f'{d}-11-26'
        rows.extend([(label,'Afdeling',1,'','',day,value) for label,value in [('Ave_24h_CompTemp',temp),('Ave_24h_CompRV','80'),('Ave_24h_CompCO2',co2)]])
        rows.append(('Sum_24h_Valve/m2','Kraan',1,'','',day,water))
        for module in (1,101):
            rows.append(('Sum_24h_ModGrowlightSysHours','modgroeilichtsturing',module,'kanaal','1',day,'21600'))
    return rows


def snapshot(ctx,**kwargs):
    return env.environment_snapshot(ctx.db,ctx.crop,ctx.plan,kwargs.pop('as_of',at(20,9)),**kwargs)


def test_complete_daily_features_intraday_cutoff_zero_water_and_phases(context):
    calibrate_all(context)
    observed=source(context,daily_rows(days=(16,17,18,19,20)))
    m1=SimpleNamespace(cultivation_id=context.crop.id,measured_at=at(18,9),created_at=at(18,10))
    m2=SimpleNamespace(cultivation_id=context.crop.id,measured_at=at(19,9),created_at=at(19,10))
    result=snapshot(context,profile='climate',m1=m1,m2=m2)
    assert result['eligible'],result['missing']
    assert result['features']==pytest.approx({'env_lamp_light_mol_m2':17.28,'env_temperature_c':20,'env_rh_pct':80,'env_water_l_m2':0})
    assert result['coverage']['lamp_light']['expected_days']==4
    assert result['coverage']['water']['valid_days']==4  # measured zero is real, missing is not zero
    assert result['coverage']['co2']['valid_days']==0
    assert result['quality']['untrusted_co2_zero']==4
    assert result['quality']['unfinished_daily_rows_excluded']==6
    assert result['window']['partial_day_excluded'] is True
    assert result['phases']['LD']['features']['env_lamp_light_mol_m2']==pytest.approx(4.32)
    assert result['phases']['KD_toM1']['features']['env_lamp_light_mol_m2']==pytest.approx(4.32)
    assert result['phases']['M1_toM2']['coverage']['lamp_light']['expected_days']==0
    assert '2026-11-18' in result['phases']['M1_toM2']['boundary_days_excluded']
    assert result['source_ids']['priva_source_ids']==[observed.id]
    assert len(result['source_ids']['mapping_revision_ids'])==4
    assert len(result['source_ids']['calibration_ids'])==7
    assert result['source_ids']['priva_value_count']==24


def test_late_source_correction_revocation_and_observation_stage(context):
    calibrate_all(context)
    original=source(context,daily_rows(temp='20'),key='same-week',revoked=at(22))
    corrected=source(context,daily_rows(temp='30'),key='same-week',version=2,activated=at(21))
    old=snapshot(context,profile='climate')
    assert old['features']['env_temperature_c']==20
    assert corrected.id not in old['source_ids']['priva_source_ids']
    newer=snapshot(context,profile='climate',as_of=at(21,9),observed_until=at(20,9))
    assert newer['features']['env_temperature_c']==30
    assert newer['source_ids']['priva_source_ids']==[corrected.id]
    historical=snapshot(context,profile='climate',as_of=at(20,9))
    assert historical['features']==old['features']
    # Stage anchoring always stops sums at the stage, even in a much later replay.
    anchored=snapshot(context,profile='light',as_of=at(23,9),observed_until=at(18,9))
    assert anchored['features']['env_lamp_light_mol_m2']==pytest.approx(8.64)
    assert anchored['window']['complete_days']==2
    assert anchored['window']['knowledge_cutoff'].startswith('2026-11-23')
    assert anchored['window']['observed_until'].startswith('2026-11-18')
    assert original.revoked_at==at(22)


def test_late_calibration_and_mapping_revisions_never_enter_old_snapshot(context):
    source(context,daily_rows())
    calibrations=calibrate_all(context,created=at(21))
    old=snapshot(context,profile='climate')
    assert old['features']=={} and not old['eligible']
    assert old['source_ids']['calibration_ids']==[]
    for c in calibrations:
        c.created_at=at(15)
    context.db.commit()
    baseline=snapshot(context,profile='climate')
    assert baseline['eligible']
    light=context.mappings[-1]
    # Mutable present-day MappingVersion was closed after the historical cutoff.
    current=context.db.get(MappingVersion,light.mapping_id)
    current.valid_until=date(2026,11,15)
    revision=MappingRevision(mapping_id=light.mapping_id,bay_number=1,object_type='light',object_number=101,
        valid_from=date(2026,11,16),valid_until=date(2026,11,15),reason='Later correction',created_at=at(21))
    context.db.add(revision); context.db.commit()
    replay=snapshot(context,profile='climate')
    assert replay['features']==baseline['features']
    assert light.id in replay['source_ids']['mapping_revision_ids']
    assert revision.id not in replay['source_ids']['mapping_revision_ids']


def test_mapping_created_late_and_moved_bay_not_guessed(context):
    calibrate_all(context); source(context,daily_rows())
    for revision in context.mappings:
        revision.created_at=at(21)
    context.db.commit()
    result=snapshot(context,profile='light')
    assert not result['eligible'] and result['features']=={}
    # Latest revision is selected BEFORE filtering bay; an old bay1 revision
    # must not return when a newer revision moves that mapping elsewhere.
    for revision in context.mappings:
        revision.created_at=at(15)
    light=context.mappings[-1]
    context.db.add(MappingRevision(mapping_id=light.mapping_id,bay_number=2,object_type='light',object_number=101,
        valid_from=date(2026,11,16),reason='Explicit move',created_at=at(19)))
    context.db.commit()
    selected,_=env._mappings_as_of(context.db,1,at(20,9))
    assert all(m.mapping_id!=light.mapping_id for m in selected)


def test_missing_days_required_coverage_and_no_zero_imputation(context):
    calibrate_all(context)
    source(context,daily_rows(days=(16,17,18)))
    result=snapshot(context,profile='climate')
    assert not result['eligible'] and result['features']=={}
    assert result['coverage']['lamp_light']['fraction']==.75
    assert result['coverage']['water']['observed_value']==0
    assert result['coverage']['water']['valid_days']==3
    assert result['coverage']['water']['missing_dates']==['2026-11-19']
    partial=snapshot(context,profile='climate',min_coverage=.75)
    assert partial['eligible']
    assert partial['features']['env_lamp_light_mol_m2']==pytest.approx(12.96)
    assert 'partial_coverage_lamp_light' in partial['warnings']
    basic=snapshot(context,profile='basic')
    assert basic['eligible'] and basic['features']=={}


def test_explicit_units_and_optional_co2_gates(context):
    calibrate_all(context,co2=False)
    source(context,daily_rows(co2='850',water='1,5'))
    basic=snapshot(context,profile='climate')
    assert basic['eligible']
    assert 'env_co2_ppm' not in basic['features']
    required=snapshot(context,profile='climate',include_co2=True)
    assert not required['eligible'] and 'env_co2_ppm' in required['missing']
    calibration(context,'unit',{'unit':'ppm'},label='Ave_24h_CompCO2')
    # Department1 positive values remain reliable; only its zero placeholders are rejected.
    context.db.commit()
    required=snapshot(context,profile='climate',include_co2=True)
    assert required['eligible'] and required['features']['env_co2_ppm']==850
    assert required['features']['env_water_l_m2']==6
    calibration(context,'unit',{'unit':'L'},created=at(19),label='Sum_24h_Valve/m2')
    context.db.commit()
    wrong=snapshot(context,profile='climate')
    assert not wrong['eligible'] and 'env_water_l_m2' in wrong['missing']


def test_priority_quality_and_pcu_conflict_are_explicit(context):
    calibrate_all(context)
    source(context,daily_rows(temp='20'))
    source(context,[(label,kind,num,sub,ch,day,'30' if label=='Ave_24h_CompTemp' else value) for label,kind,num,sub,ch,day,value in daily_rows()],profile='algemeen_bedrijf')
    high=snapshot(context,profile='climate')
    assert high['features']['env_temperature_c']==30
    assert high['quality']['source_conflicts']==4
    settings=AppSettings(priority_klimaat=90,priority_algemeen_bedrijf=5)
    row=SettingsVersion(settings=settings.model_dump(),effective_from=at(19),created_at=at(19),reason='Explicit priority',user_id=1)
    context.db.add(row); context.db.commit()
    low=snapshot(context,profile='climate')
    assert low['features']['env_temperature_c']==20
    assert low['source_ids']['settings_id']==row.id
    # A settings row recorded after cutoff must not affect replay.
    row.created_at=at(21); context.db.commit()
    assert snapshot(context,profile='climate')['features']['env_temperature_c']==30
    # A separate PCU has no established physical identity; do not average it.
    duplicate=context.db.scalar(select(PrivaValue).where(PrivaValue.label=='Ave_24h_CompTemp',PrivaValue.local_date=='2026-11-16'))
    alternate=PrivaValue(**{column.name:getattr(duplicate,column.name) for column in PrivaValue.__table__.columns if column.name!='id'})
    alternate.row_number=999; alternate.pcu='OTHER'; context.db.add(alternate); context.db.commit()
    ambiguous=snapshot(context,profile='climate')
    assert not ambiguous['eligible']
    assert ambiguous['quality']['ambiguous_measurement_series']==1


def test_unmapped_extra_valve_and_daylight_is_only_explicit_estimate(context):
    calibrate_all(context)
    rows=daily_rows()
    rows.extend([('Sum_24h_Valve/m2','Kraan',41,'','',f'{day}-11-26','1000') for day in (16,17,18,19)])
    rows.extend([('Sum_24h_Radiation','Straling',1,'','',f'{day}-11-26','1000') for day in (16,17,18,19)])
    source(context,rows)
    plain=snapshot(context,profile='climate')
    assert plain['features']['env_water_l_m2']==0
    assert 'env_daylight_estimate_mol_m2' not in plain['features']
    assert not snapshot(context,profile='climate',include_daylight_estimate=True)['eligible']
    calibration(context,'unit',{'unit':'J/cm2'},label='Sum_24h_Radiation')
    transmission=calibration(context,'transmission',{'factor':.7},object_type='bay',object_number=1)
    context.db.commit()
    assert not snapshot(context,profile='climate',include_daylight_estimate=True)['eligible']
    transmission.value_json=json.dumps({'factor':.7,'no_screen_effect':True}); context.db.commit()
    estimated=snapshot(context,profile='climate',include_daylight_estimate=True)
    assert estimated['eligible']
    assert estimated['features']['env_daylight_estimate_mol_m2']==pytest.approx(60.2)
    assert estimated['coverage']['daylight_estimate']['quality']=='estimate'
    assert 'daylight_is_estimated_not_verified' in estimated['warnings']


def test_bounded_reads_are_never_eligible(context,monkeypatch):
    calibrate_all(context); source(context,daily_rows())
    monkeypatch.setattr(env,'MAX_ROWS',2)
    result=snapshot(context,profile='basic')
    assert result['truncated'] and not result['eligible']
    assert 'bounded_read_truncated' in result['missing']


def test_environment_route_requires_auth_and_uses_plan_cutoff(context):
    app=FastAPI(); app.include_router(env.router)
    def database():
        yield context.db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        assert client.get(f'/api/cultivations/{context.crop.id}/environment').status_code==401
        app.dependency_overrides[require_user]=lambda:SimpleNamespace(id=1,role='admin')
        # A future date is rejected even if validly formatted.
        assert client.get(f'/api/cultivations/{context.crop.id}/environment?as_of=2099-01-01T00:00:00Z').status_code==422
        assert client.get('/api/cultivations/999/environment').status_code==404
        response=client.get(f'/api/cultivations/{context.crop.id}/environment?as_of=2026-11-15T09:00:00Z&profile=light')
        assert response.status_code==200,response.text
        assert not response.json()['eligible']


def test_daily_means_use_actual_dst_duration_without_prorating_sums(context):
    context.plan.plant_date=date(2024,3,30); context.plan.light_out_date=date(2024,3,30)
    context.plan.created_at=at(29,month=3,year=2024)
    for revision in context.mappings:
        revision.valid_from=date(2024,3,30); revision.created_at=at(29,month=3,year=2024)
    records=calibrate_all(context,created=at(29,month=3,year=2024))
    for record in records:
        record.valid_from=date(2024,3,30)
    rows=[]
    for day,temperature in [(30,'10'),(31,'20')]:
        rows.extend([(label,kind,number,sub,ch,f'{day}-3-24',temperature if label=='Ave_24h_CompTemp' else value)
            for label,kind,number,sub,ch,old_day,value in daily_rows(days=(16,))])
    observed=source(context,rows,activated=at(1,month=4,year=2024))
    observed.created_at=at(29,month=3,year=2024); context.db.commit()
    result=snapshot(context,profile='climate',as_of=at(1,month=4,year=2024))
    assert result['eligible'],result['missing']
    assert result['features']['env_temperature_c']==pytest.approx((10*24+20*23)/47)
    assert result['features']['env_lamp_light_mol_m2']==pytest.approx(8.64)
    assert result['coverage']['temperature']['valid_days']==2
    assert not result['window']['partial_day_excluded']


def test_late_measurement_boundary_and_source_activation_are_excluded(context):
    calibrate_all(context); source(context,daily_rows())
    not_yet_imported=snapshot(context,profile='light',as_of=at(19,9))
    assert not not_yet_imported['eligible']
    assert not_yet_imported['source_ids']['priva_source_ids']==[]
    late=SimpleNamespace(cultivation_id=context.crop.id,measured_at=at(18,9),created_at=at(21))
    result=snapshot(context,profile='light',m1=late)
    assert result['eligible']
    assert result['phases']['KD_toM1']['coverage']['lamp_light']['expected_days']==3
    assert result['phases']['M1_toM2']['window']['start'] is None
    # No hidden dependence on the mutable plan passed by a caller.
    context.plan.created_at=at(21)
    rejected=snapshot(context,profile='light')
    assert not rejected['eligible'] and rejected['features']=={}
    assert rejected['missing']==['planning_not_known_at_cutoff']


def test_late_revocation_only_changes_later_knowledge_snapshot(context):
    calibrate_all(context)
    observed=source(context,daily_rows(),revoked=at(21))
    old=snapshot(context,profile='light')
    assert old['eligible'] and old['source_ids']['priva_source_ids']==[observed.id]
    later=snapshot(context,profile='light',as_of=at(22),observed_until=at(20,9))
    assert not later['eligible'] and later['source_ids']['priva_source_ids']==[]
    assert snapshot(context,profile='light')['features']==old['features']


def test_usable_value_outranks_high_priority_missing_value(context):
    calibrate_all(context)
    source(context,daily_rows(temp='20'))
    rows=daily_rows(temp='30')
    rows=[(label,kind,n,sub,ch,day,'' if label=='Ave_24h_CompTemp' and day=='16-11-26' else value)
          for label,kind,n,sub,ch,day,value in rows]
    source(context,rows,profile='algemeen_bedrijf')
    result=snapshot(context,profile='climate')
    assert result['eligible']
    assert result['features']['env_temperature_c']==pytest.approx(27.5)
    assert result['coverage']['temperature']['valid_days']==4


def test_finite_source_values_that_overflow_period_sum_remain_unknown(context):
    calibrate_all(context)
    source(context, daily_rows(water='1e308'))
    result=snapshot(context,profile='climate')
    assert not result['eligible']
    assert 'env_water_l_m2' in result['missing']
    assert result['coverage']['water']['observed_value'] is None
    assert result['coverage']['water']['reason']=='non_finite_aggregate'
    # No NaN/Infinity may escape through API JSON, including phase summaries.
    json.dumps(result,allow_nan=False)


def supplemental_source(ctx, module=101, day=19, activated=None):
    from app.history_import import HistoryImport
    from app.legacy_lighting import LegacyLightingJob, LegacyLightingDay, LABEL
    history=HistoryImport(filename='synthetic.xlsx',sha256='a'*64,raw_path='history/synthetic.xlsx',status='uploaded',user_id=1,created_at=at(18))
    ctx.db.add(history); ctx.db.flush()
    job=LegacyLightingJob(history_import_id=history.id,user_id=1,filename=history.filename,
        raw_path=history.raw_path,checksum=history.sha256,source_key='b'*64,status='completed',
        phase='import',created_at=at(18),confirmed_at=at(18),activated_at=activated or at(18),
        interpretation={'value_kind':'linear_dim_percent','timestamp_role':'interval_start'},preview={'sheet':'data'})
    ctx.db.add(job); ctx.db.flush()
    row=LegacyLightingDay(job_id=job.id,day=date(2026,11,day),module=module,column='B',
        row_count=288,valid_count=288,percent_sum=14400,seen_mask=hex((1<<288)-1)[2:],
        valid_mask=hex((1<<288)-1)[2:],row_ranges=[[4,291]],quality=[])
    ctx.db.add(row)
    calibration(ctx,'lamp',{'ppfd':120,'dim_factor':1,'hours_label':LABEL,'time_unit':'h',
        'independent_contribution':True},object_type='light',object_number=module,label=LABEL,channel='')
    ctx.db.commit()
    return job


def test_excel_only_supplements_missing_priva_modules_with_explicit_provenance(context):
    context.plan.plant_date=context.plan.light_out_date=date(2026,11,19)
    calibrate_all(context)
    observed=source(context,[r for r in daily_rows(days=(19,)) if r[2]!=101])
    job=supplemental_source(context)
    result=snapshot(context,profile='light')
    assert result['eligible'],result
    assert result['features']['env_lamp_light_mol_m2']==pytest.approx(1.728+5.184)
    assert result['source_ids']['priva_source_ids']==[observed.id]
    assert result['supplemental_lighting']['used_days'][0]['legacy_lighting_job_id']==job.id
    assert result['source_ids']['legacy_lighting']['job_ids']==[job.id]
    assert result['source_ids']['legacy_lighting']['source_checksums']==['a'*64]
    assert result['quality']['supplemental_excel_lamp_days']==1
    # A later revocation changes later knowledge, while replay preserves the source.
    job.revoked_at=at(21); context.db.commit()
    assert not snapshot(context,profile='light',as_of=at(22),observed_until=at(20))['eligible']
    assert snapshot(context,profile='light')['eligible']
    # No observations from after the measurement stage can enter a replay.
    assert 'env_lamp_light_mol_m2' not in snapshot(context,profile='light',observed_until=at(19,9))['features']


@pytest.mark.parametrize('primary_value,expected', [('21600',4.32),('0',1.728),('',None)])
def test_priva_present_always_beats_excel_including_zero_and_invalid(context,primary_value,expected):
    context.plan.plant_date=context.plan.light_out_date=date(2026,11,19)
    calibrate_all(context); supplemental_source(context)
    rows=[(label,kind,n,sub,ch,day,primary_value if n==101 else value)
          for label,kind,n,sub,ch,day,value in daily_rows(days=(19,))]
    source(context,rows)
    settings=AppSettings(priority_klimaat=0)
    context.db.add(SettingsVersion(settings=settings.model_dump(),created_at=at(19),effective_from=at(19),reason='Synthetic zero primary priority',user_id=1));context.db.commit()
    result=snapshot(context,profile='light')
    assert not result['supplemental_lighting']['used_days']
    assert result['eligible']==(expected is not None)
    if expected is not None:
        assert result['features']['env_lamp_light_mol_m2']==pytest.approx(expected)
    else:
        assert 'env_lamp_light_mol_m2' not in result['features']


def test_invalid_multiday_priva_record_still_blocks_supplement(context):
    context.plan.plant_date=context.plan.light_out_date=date(2026,11,19)
    calibrate_all(context); supplemental_source(context)
    source(context,daily_rows(days=(19,)))
    row=context.db.scalar(select(PrivaValue).where(PrivaValue.object_number==101))
    row.local_date='2026-11-18'
    raw=json.loads(row.raw_json);raw['startdate']='18-11-26';row.raw_json=json.dumps(raw)
    context.db.commit()
    result=snapshot(context,profile='light')
    assert not result['eligible']
    assert result['coverage']['lamp_light']['valid_days']==0
    assert not result['supplemental_lighting']['used_days']
