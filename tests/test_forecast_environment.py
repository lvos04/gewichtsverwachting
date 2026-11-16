"""Integration gates between real source extraction, Ridge profiles and replay."""
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from test_forecast import db, add_cultivation
from app import forecast as fc
from app.importer import Calibration, ImportJob, PrivaSource, PrivaValue, normalize_row
from app.models import Harvest, MappingRevision, MappingVersion, Measurement, PlanningVersion, VarietyTargetVersion
from app.settings import AppSettings, SettingsVersion


def _source_environment(db, crop, at):
    plant = at(0).date()
    for kind, number in [('department', 1), ('valve', 1), ('light', 1), ('light', 101)]:
        mapping = MappingVersion(bay_number=1, object_type=kind, object_number=number,
            valid_from=plant, created_at=at(0))
        db.add(mapping); db.flush()
        db.add(MappingRevision(mapping_id=mapping.id, bay_number=1, object_type=kind,
            object_number=number, valid_from=plant, created_at=at(0)))
    for label, unit in [('Ave_24h_CompTemp', '°C'), ('Ave_24h_CompRV', '%'),
                        ('Sum_24h_Valve/m2', 'L/m2'), ('Sum_24h_ModGrowlightSysHours', 's')]:
        db.add(Calibration(kind='unit', valid_from=plant, label=label, value_json=json.dumps({'unit': unit}),
            confirmed=True, user_id=1, created_at=at(0), reason='synthetic verified unit'))
    for module, ppfd in [(1, 80), (101, 120)]:
        db.add(Calibration(kind='lamp', valid_from=plant, object_type='light', object_number=module,
            channel='1', value_json=json.dumps({'ppfd': ppfd, 'dim_factor': 1, 'independent_contribution': True,
                'hours_label': 'Sum_24h_ModGrowlightSysHours', 'time_unit': 's'}),
            confirmed=True, user_id=1, created_at=at(0), reason='synthetic verified contribution'))
    job = ImportJob(user_id=1, status='completed', created_at=at(0)); db.add(job); db.flush()
    source = PrivaSource(job_id=job.id, name='synthetic.csv', path='imports/synthetic.csv',
        checksum='synthetic-source', profile='klimaat', logical_key='synthetic-all-days',
        version=1, created_at=at(0), activated_at=at(9))
    db.add(source); db.flush()
    number = 0
    for day in range(35):
        stamp = at(day).strftime('%d-%m-%Y')
        observations = [('Ave_24h_CompTemp','Afdeling',1,'','20' if day < 10 else '40'),
                        ('Ave_24h_CompRV','Afdeling',1,'','80'),
                        ('Sum_24h_Valve/m2','Kraan',1,'','0')]
        observations += [('Sum_24h_ModGrowlightSysHours','modgroeilichtsturing',m,'1','21600') for m in (1,101)]
        for label, kind, object_number, channel, value in observations:
            number += 1
            raw = {'label':label,'pcu':'SYNTHETIC','type_1':kind,'idx_1':str(object_number),
                   'type_2':'kanaal' if channel else '', 'idx_2':channel,
                   'startdate':stamp,'enddate':stamp,'value':value}
            db.add(PrivaValue(source_id=source.id, **normalize_row(raw, number)))
    db.commit()
    return source


def _spec(profile):
    return {'profile': profile, 'minimum_coverage': 1.0, 'include_daylight_estimate': False,
            'include_co2': False, 'anchor': 'measurement'}


def test_real_environment_is_stage_anchored_and_unknown_is_never_zero(db):
    crop, at = add_cultivation(db, 1)
    source = _source_environment(db, crop, at)
    snap = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('climate'))
    assert snap is not None
    assert snap['features']['env_temperature_c'] == 20  # later completed 40°C days excluded
    assert snap['features']['env_lamp_light_mol_m2'] == pytest.approx(43.2)
    assert snap['features']['env_water_l_m2'] == 0  # explicitly measured zero
    assert snap['environment']['window']['knowledge_cutoff'].startswith(at(20).isoformat())
    assert snap['environment']['window']['observed_until'].startswith(at(10).isoformat())
    assert snap['sources']['environment']['priva_source_ids'] == [source.id]
    unit = db.scalar(select(Calibration).where(Calibration.label == 'Ave_24h_CompTemp'))
    unit.created_at = at(12); db.commit()
    diagnostics = {}
    assert fc.feature_snapshot(db, crop, 'KD10', at(11), spec=_spec('climate'), diagnostics=diagnostics) is None
    assert 'env_temperature_c' in diagnostics['missing']
    assert fc.feature_snapshot(db, crop, 'KD10', at(11), spec=_spec('light')) is not None


def test_training_profile_reports_data_exclusions_and_higher_minimum(db):
    for i in range(30):
        add_cultivation(db, i)
    settings = AppSettings(model_feature_profile='climate', environment_min_training_cultivations=60)
    db.add(SettingsVersion(settings=settings.model_dump(), created_at=datetime(2010,1,1),
        effective_from=datetime(2010,1,1), reason='synthetic profile', user_id=1)); db.commit()
    result = fc.train_kind(db, 'KD10')
    assert result['status'] == 'insufficient_data'
    assert result['feature_profile'] == 'climate' and result['required'] == 60
    assert result['complete_standard_cultivations'] == 0
    assert result['environment_excluded_count'] == 30


def test_environment_model_must_improve_same_cohort_basic_reference(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, 'DATA_DIR', tmp_path)
    for i in range(32):
        add_cultivation(db, i, m1_weight=20, m2_weight=40, final=60 + (i*7 % 17)*3)
    original = fc._labeled_rows(db, 'KD10', spec=_spec('basic'))
    for i, row in enumerate(original):
        row['features']['env_lamp_light_mol_m2'] = float(i*7 % 17)
    config = AppSettings(model_feature_profile='light', environment_min_training_cultivations=30)
    db.add(SettingsVersion(settings=config.model_dump(), created_at=datetime(2010,1,1),
        effective_from=datetime(2010,1,1), reason='synthetic training comparison', user_id=1)); db.commit()
    monkeypatch.setattr(fc, '_labeled_rows', lambda *args, **kwargs: original)
    model = fc.train_kind(db, 'KD10'); db.commit()
    assert model['features']['spec']['profile'] == 'light'
    assert model['metrics']['basic_reference']['n'] == model['metrics']['model']['n'] == 2
    assert model['metrics']['same_evaluation_ids'] == model['evaluation_ids']
    assert model['operational_eligible']
    assert all(row['mae_g'] < row['basic_reference_mae_g']
               for row in model['metrics']['release_holdout']['rounds'])
    # A spurious/constant environmental column must not release merely for
    # beating the weaker historical mean when the basic Ridge is equally good.
    for row in original:
        row['features']['env_lamp_light_mol_m2'] = 1.0
    rejected = fc.train_kind(db, 'KD10'); db.commit()
    assert not rejected['operational_eligible']


def test_missing_active_climate_inputs_fall_back_without_zero_imputation(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, 'DATA_DIR', tmp_path)
    for i in range(30):
        add_cultivation(db, i, m1_weight=18 + i%8, final=86+2*(i%8))
    base = fc.train_kind(db, 'KD10'); db.commit()
    basic_model = db.get(fc.ModelArtifact, base['id'])
    climate = fc.ModelArtifact(kind='KD10', feature_version=fc.FEATURE_VERSION,
        created_at=fc.utcnow(), active=True, activated_at=fc.utcnow(), operational_eligible=True,
        features={'spec':_spec('climate'),'numeric':fc.numeric_features('KD10',_spec('climate'))},
        metrics={}, training_ids=basic_model.training_ids, evaluation_ids=[], data_hash='synthetic-other',
        artifact_hash=basic_model.artifact_hash, artifact_path=basic_model.artifact_path, interval_radius_g=2)
    db.add(climate); db.flush()
    db.add(fc.ModelActivation(model_id=climate.id, kind='KD10', created_at=fc.utcnow())); db.commit()
    crop, _ = add_cultivation(db, 100, plant=(fc.utcnow()-timedelta(days=31)).date())
    result = fc.forecast(db, crop.id, fc.utcnow(), persist=False)
    assert result['model_id'] == basic_model.id
    assert result['feature_profile'] == 'basic'
    assert result['point_g'] is not None
    assert result['unavailable_models'][0]['model_id'] == climate.id
    assert not any(key.startswith('env_') for key in result['features_used'])
    assert result['status'] == 'experimental'


def test_activation_history_and_target_history_cannot_rewrite_past(db):
    crop, at = add_cultivation(db, 1)
    def model(name):
        item = fc.ModelArtifact(kind='KD10', created_at=at(0), active=False,
            feature_version='legacy', features={}, metrics={}, training_ids=[], evaluation_ids=[],
            data_hash=name, artifact_hash=name, artifact_path=name, interval_radius_g=1,
            operational_eligible=True)
        db.add(item); db.flush(); return item
    first, second = model('first'), model('second')
    db.add_all([fc.ModelActivation(model_id=first.id,kind='KD10',created_at=at(10)),
                fc.ModelActivation(model_id=second.id,kind='KD10',created_at=at(20)),
                fc.ModelActivation(model_id=first.id,kind='KD10',created_at=at(30))])
    db.add(VarietyTargetVersion(variety_id=1,target_weight_g=115,created_at=at(20),author_id=1))
    db.commit()
    assert fc.active_model_at(db,'KD10',at(15)) == first.id
    assert fc.active_model_at(db,'KD10',at(25)) == second.id
    assert fc.active_model_at(db,'KD10',at(35)) == first.id
    # Keep this test focused on target replay without loading placeholder files.
    db.query(fc.ModelActivation).delete(); db.query(fc.ModelArtifact).delete(); db.commit()
    assert fc.forecast(db,crop.id,at(15),persist=False)['target_g'] == 105
    assert fc.forecast(db,crop.id,at(25),persist=False)['target_g'] == 115


def test_future_harvest_and_overlapping_holdouts_never_train_early(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, 'DATA_DIR', tmp_path)
    crop, at = add_cultivation(db, 1)
    harvest = db.scalar(select(Harvest).where(Harvest.cultivation_id==crop.id))
    harvest.end_date = date(2035,1,1); db.commit()
    assert crop.id not in [row['id'] for row in fc._labeled_rows(db,'KD10')]
    # Inject a bounded cohort with overlapping planting/stage dates: the final
    # second round's forecast predates the first. A frozen model may use neither
    # a late-recorded nor a not-yet-completed harvest at either holdout time.
    rows=[]
    origin=datetime(2018,1,1)
    for i in range(32):
        rows.append({'id':i+1,'round':f'R{i:02}','variety_id':1,'bay_number':1,
            'plant_date':date(2018,1,1)+timedelta(days=i),
            'available_at': origin+timedelta(days=10+i) if i<30 else origin+timedelta(days=70 if i==30 else 60),
            'harvest_recorded_at':origin+timedelta(days=i+41 if i<30 else 90+i-30),
            'harvest_available_at':origin+timedelta(days=i+41 if i<30 else 90+i-30),
            'y':float(100+i),'features':{'variety_id':'1','m1_weight_g':float(20+i)},
            'sources':{},'harvest_id':i+1})
    monkeypatch.setattr(fc,'_labeled_rows',lambda *args,**kwargs:rows)
    result=fc.train_kind(db,'KD10'); db.commit()
    # Only 20 labels were genuinely available at the earliest holdout time.
    assert result['metrics']['release_holdout'] is None
    assert not result['operational_eligible']


def test_late_label_certification_cannot_supply_prior_training_evidence(db, tmp_path, monkeypatch):
    from app.models import CultivationRevision
    monkeypatch.setattr(fc, 'DATA_DIR', tmp_path)
    first = None
    for i in range(32):
        crop, _ = add_cultivation(db, i, quality='ambiguous' if i == 0 else 'verified',
            m1_weight=18+(i*7%15), final=50+2*(18+(i*7%15)))
        if i == 0:
            first = crop
    db.add(CultivationRevision(cultivation_id=first.id, category='standard', label_quality='verified',
        status='harvested', created_at=datetime(2025,1,1)))
    db.commit()
    rows = fc._labeled_rows(db,'KD10',spec=_spec('basic'))
    certified = next(row for row in rows if row['id'] == first.id)
    assert certified['harvest_available_at'] == datetime(2025,1,1)
    result = fc.train_kind(db,'KD10'); db.commit()
    assert result['metrics']['release_holdout'] is None
    assert not result['operational_eligible']


def test_pre_02_artifact_keeps_legacy_profile_and_remains_loadable(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc,'DATA_DIR',tmp_path)
    for i in range(30):
        add_cultivation(db,i,m1_weight=20+i%5,final=90+2*(i%5))
    trained=fc.train_kind(db,'KD10');db.commit()
    legacy=db.get(fc.ModelArtifact,trained['id'])
    legacy.features={key:value for key,value in legacy.features.items() if key!='spec'}
    legacy.feature_version='kd-point-in-time-v2-settings'
    db.commit()
    crop,_=add_cultivation(db,100,plant=(fc.utcnow()-timedelta(days=31)).date())
    result=fc.forecast(db,crop.id,fc.utcnow(),persist=False)
    assert result['model_id']==legacy.id and result['point_g'] is not None
    assert result['feature_version']=='kd-point-in-time-v2-settings'
    assert result['feature_profile']=='basic'
    assert result['features_used']['stage_daylength_hours']==pytest.approx(result['season']['daylength_hours'])
    assert fc.model_spec(legacy)['anchor']=='legacy_asof'


def _bridge_on(db, at, voorrang='bridge_ok_verdacht'):
    from app.bridge import BridgeDailyValue, BridgeSync
    db.add(SettingsVersion(settings=AppSettings(bridge_enabled=True, bridge_url='http://10.0.0.5:8000',
        bridge_voorrang=voorrang).model_dump(), effective_from=datetime(2000, 1, 1),
        created_at=datetime(2000, 1, 1), reason='Bridge aan (synthetisch)'))
    sync = BridgeSync(trigger='handmatig', status='ok', started_at=at(9)); db.add(sync); db.flush()
    for day in range(10):
        quality = 'onvolledig' if day == 3 else ('verdacht' if day == 4 else 'ok')
        db.add(BridgeDailyValue(sync_id=sync.id, scope='kap:1', local_date=at(day).date().isoformat(),
            metric='lichtsom_lampen_kap', value=5.0, unit='mol/m²/dag', coverage=1.0 if day != 3 else 0.5,
            quality=quality, resolved_from='kap:1', sources_json='[]', import_id='imp_1', revision=1,
            fetched_at=at(9)))
    db.commit()


def test_bridge_switch_off_leaves_environment_unchanged(db):
    from app.bridge import BridgeDailyValue, BridgeSync
    crop, at = add_cultivation(db, 1)
    _source_environment(db, crop, at)
    before = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    sync = BridgeSync(trigger='handmatig', status='ok', started_at=at(9)); db.add(sync); db.flush()
    db.add(BridgeDailyValue(sync_id=sync.id, scope='kap:1', local_date=at(1).date().isoformat(),
        metric='lichtsom_lampen_kap', value=99.0, unit='mol/m²/dag', coverage=1.0, quality='ok',
        sources_json='[]', fetched_at=at(9)))
    db.commit()
    after = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    assert after == before and 'bridge' not in after['environment']


def test_bridge_precedence_by_quality_and_fallback_to_own_import(db):
    crop, at = add_cultivation(db, 1)
    _source_environment(db, crop, at)
    _bridge_on(db, at)
    snap = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    # 9 Bridge-dagen à 5,0 (ok/verdacht) + dag 3 onvolledig → eigen import 4,32
    assert snap['features']['env_lamp_light_mol_m2'] == pytest.approx(9 * 5.0 + 4.32)
    info = snap['environment']['bridge']
    assert info['days_from_bridge']['lamp_light'] == 9 and info['days_from_own_import']['lamp_light'] == 1
    assert info['import_ids'] == ['imp_1']
    # Rows fetched after the cutoff are not known yet: point-in-time reproducible.
    from app.bridge import BridgeDailyValue
    for row in db.scalars(select(BridgeDailyValue)):
        row.fetched_at = at(25)
    db.commit()
    later = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    assert later['features']['env_lamp_light_mol_m2'] == pytest.approx(43.2)
    assert later['environment']['bridge']['days_from_bridge']['lamp_light'] == 0


def test_bridge_own_import_first_only_fills_gaps(db):
    crop, at = add_cultivation(db, 1)
    _source_environment(db, crop, at)
    _bridge_on(db, at, voorrang='eigen_import_eerst')
    snap = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    assert snap['features']['env_lamp_light_mol_m2'] == pytest.approx(43.2)
    assert snap['environment']['bridge']['days_from_bridge']['lamp_light'] == 0


def test_bridge_fills_days_without_own_import_but_missing_stays_missing(db):
    from app.bridge import BridgeDailyValue
    crop, at = add_cultivation(db, 1)
    _bridge_on(db, at)  # geen eigen import en geen koppelingen
    snap = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    assert snap is None  # dag 3 onvolledig en geen eigen import: geen volledige dekking, nooit als 0
    row = db.scalar(select(BridgeDailyValue).where(BridgeDailyValue.quality == 'onvolledig'))
    row.quality = 'ok'; db.commit()
    snap = fc.feature_snapshot(db, crop, 'KD10', at(20), spec=_spec('light'))
    assert snap['features']['env_lamp_light_mol_m2'] == pytest.approx(50.0)
