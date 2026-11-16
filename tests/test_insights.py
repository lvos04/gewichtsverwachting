from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import require_user
from app.db import Base, get_db
from app.forecast import ForecastSnapshot, ModelArtifact
from app.insights import router
from app.models import Bay, Cultivation, CultivationRevision, Event, Harvest, Variety


@pytest.fixture
def setup_api(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/insights.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[require_user] = lambda: SimpleNamespace(id=1, role='lezer')
    with TestClient(app) as client:
        yield client, sessions, app
    engine.dispose()


def test_asof_groups_units_and_honest_comparison(setup_api):
    client, sessions, _ = setup_api
    cutoff = datetime(2026, 11, 15, 12)
    with sessions() as db:
        variety = Variety(name='test')
        db.add(variety)
        db.flush()
        # Six controls give five independent bays; a separate adjusted crop is
        # matched by exact round and variety.
        crops = []
        for i in range(6):
            bay = i + 1
            db.add(Bay(number=bay, department=1, beds=6))
            db.flush()
            crop = Cultivation(round_name='R1', bay_number=bay, variety_id=variety.id, beds=6)
            db.add(crop)
            db.flush()
            crops.append(crop)
            db.add(CultivationRevision(cultivation_id=crop.id, category='control',
                                       label_quality='verified', status='harvested',
                                       created_at=datetime(2026, 10, 1)))
            db.add(Harvest(cultivation_id=crop.id, start_date=date(2026, 11, 1),
                           end_date=date(2026, 11, 10), weight_g=100 + i,
                           created_at=datetime(2026, 11, 11)))
        bay = 7
        db.add(Bay(number=bay, department=1, beds=6))
        db.flush()
        adjusted = Cultivation(round_name='R1', bay_number=bay, variety_id=variety.id, beds=6)
        db.add(adjusted)
        db.flush()
        db.add(CultivationRevision(cultivation_id=adjusted.id, category='adjusted', label_quality='verified',
                                   status='harvested', created_at=datetime(2026, 10, 1)))
        db.add(Harvest(cultivation_id=adjusted.id, start_date=date(2026, 11, 1), end_date=date(2026, 11, 10),
                       weight_g=120, created_at=datetime(2026, 11, 11)))
        # Future recorded correction must not overwrite the known harvest.
        db.add(Harvest(cultivation_id=crops[0].id, start_date=date(2026, 11, 1), end_date=date(2026, 11, 10),
                       weight_g=999, created_at=datetime(2026, 11, 20)))
        # Latest recorded correction is authoritative even though its weight is null.
        db.add(Harvest(cultivation_id=crops[1].id, start_date=date(2026, 11, 1), end_date=date(2026, 11, 10),
                       weight_g=None, created_at=datetime(2026, 11, 12)))
        # An ambiguous crop is excluded; a later label correction is not yet known.
        amb = Cultivation(round_name='R1', bay_number=8, variety_id=variety.id, beds=6)
        db.add(Bay(number=8, department=1, beds=6))
        db.flush()
        db.add(amb)
        db.flush()
        db.add(CultivationRevision(cultivation_id=amb.id, category='control', label_quality='ambiguous',
                                   status='harvested', created_at=datetime(2026, 10, 1)))
        db.add(Harvest(cultivation_id=amb.id, end_date=date(2026, 11, 10), weight_g=300,
                       created_at=datetime(2026, 11, 11)))
        # Record time and event time both gate visibility; units remain separate.
        db.add(Event(cultivation_id=adjusted.id, occurred_at=datetime(2026, 11, 5), created_at=datetime(2026, 11, 6),
                     kind='alar volvelds', amount=2, unit='g/1000m2', scope='bay'))
        db.add(Event(cultivation_id=adjusted.id, occurred_at=datetime(2026, 11, 5), created_at=datetime(2026, 11, 6),
                     kind='selectief', amount=3, unit='g/10l', scope='plant'))
        db.add(Event(cultivation_id=adjusted.id, occurred_at=datetime(2026, 11, 17), created_at=datetime(2026, 11, 6),
                     kind='lamp', amount=4, unit='hours', scope='bay'))
        db.add(Event(cultivation_id=adjusted.id, occurred_at=datetime(2026, 11, 5), created_at=datetime(2026, 11, 17),
                     kind='temperature', amount=20, unit='C', scope='bay'))
        # Backdated as_of cannot make a forecast recorded after harvest began
        # into a preharvest prediction.
        model = ModelArtifact(kind='KD10', feature_version='test-v1', features={}, metrics={},
                              training_ids=[], evaluation_ids=[], data_hash='a' * 64,
                              artifact_hash='b' * 64, artifact_path='test.joblib', interval_radius_g=1,
                              created_at=datetime(2026, 10, 20))
        db.add(model)
        db.flush()
        db.add(ForecastSnapshot(cultivation_id=adjusted.id, as_of=datetime(2026, 10, 30),
                                created_at=datetime(2026, 11, 2), model_id=model.id,
                                status='experimental', point_g=90, low_g=80, high_g=100,
                                source_ids={}, features={}, response={}))
        db.commit()

    response = client.get('/api/interventions/compare', params={'round_name': 'R1', 'as_of': cutoff.isoformat()})
    assert response.status_code == 200
    result = response.json()
    comparison = result['comparisons'][0]
    assert comparison['groups']['control']['cultivations'] == 5
    assert comparison['groups']['control']['bays'] == 5
    assert comparison['groups']['control']['mean_final_weight_g'] == pytest.approx(102.8)
    assert comparison['adjusted_minus_control_observed_difference_g'] == pytest.approx(17.2)
    assert comparison['groups']['adjusted']['residual_n'] == 0
    assert comparison['difference_status'] == 'descriptive_noncausal'
    assert any(x['reason'] == 'ineligible_label_quality' for x in result['excluded_cultivations'])
    assert {(e['kind'], e['unit']) for e in result['events']} == {
        ('alar volvelds', 'g/1000m2'), ('selectief', 'g/10l')}
    assert all('niet causaal' in notice.lower() or 'geen bewijs' in notice.lower() or 'eenheid' in notice.lower()
               or 'gecorreleerd' in notice.lower() for notice in result['notices'])


def test_minimum_control_count_and_auth(setup_api):
    client, sessions, app = setup_api
    with sessions() as db:
        v = Variety(name='tiny')
        db.add(v)
        db.flush()
        model = ModelArtifact(kind='KD10', feature_version='test-v1', features={}, metrics={},
                              training_ids=[], evaluation_ids=[], data_hash='c' * 64,
                              artifact_hash='d' * 64, artifact_path='test.joblib', interval_radius_g=1,
                              created_at=datetime(2026, 10, 20))
        db.add(model)
        db.flush()
        for i in range(4):
            db.add(Bay(number=i + 1, department=1, beds=6))
            db.flush()
            c = Cultivation(round_name='R2', bay_number=i + 1, variety_id=v.id, beds=6)
            db.add(c)
            db.flush()
            category = 'adjusted' if i == 3 else 'control'
            db.add(CultivationRevision(cultivation_id=c.id, category=category, label_quality='verified', status='harvested',
                                       created_at=datetime(2026, 10, 1)))
            db.add(Harvest(cultivation_id=c.id, end_date=date(2026, 11, 10), weight_g=100,
                           created_at=datetime(2026, 11, 11)))
            db.add(ForecastSnapshot(cultivation_id=c.id, as_of=datetime(2026, 10, 30),
                                    created_at=datetime(2026, 10, 30), model_id=model.id,
                                    status='operational', point_g=90, low_g=80, high_g=100,
                                    source_ids={}, features={}, response={}))
        db.commit()
    result = client.get('/api/interventions/compare', params={'round_name': 'R2', 'as_of': '2026-11-15T12:00:00'}).json()
    comp = result['comparisons'][0]
    assert comp['adjusted_minus_control_observed_difference_g'] is None
    assert comp['average_prediction_residual_difference_g'] is None
    assert comp['difference_status'] == 'insufficient_controls'
    app.dependency_overrides.pop(require_user)
    assert client.get('/api/interventions/compare', params={'round_name': 'R2'}).status_code == 401


def test_future_asof_rejected(setup_api):
    client, _, _ = setup_api
    response = client.get('/api/interventions/compare', params={'round_name': 'empty', 'as_of': '2999-01-01T00:00:00'})
    assert response.status_code == 422


def test_control_adjusted_bay_overlap_is_reported_not_treated_as_independent(setup_api):
    client, sessions, _ = setup_api
    with sessions() as db:
        v = Variety(name='overlap')
        db.add(v)
        db.flush()
        for i in range(4):
            db.add(Bay(number=i + 1, department=1, beds=6))
            db.flush()
            control = Cultivation(round_name='R3', bay_number=i + 1, variety_id=v.id, beds=6)
            db.add(control)
            db.flush()
            db.add(CultivationRevision(cultivation_id=control.id, category='control', label_quality='verified',
                                       status='harvested', created_at=datetime(2026, 10, 1)))
            db.add(Harvest(cultivation_id=control.id, end_date=date(2026, 11, 10), weight_g=100,
                           created_at=datetime(2026, 11, 11)))
        adjusted = Cultivation(round_name='R3', bay_number=1, variety_id=v.id, beds=6)
        db.add(adjusted)
        db.flush()
        db.add(CultivationRevision(cultivation_id=adjusted.id, category='adjusted', label_quality='verified',
                                   status='harvested', created_at=datetime(2026, 10, 1)))
        db.add(Harvest(cultivation_id=adjusted.id, end_date=date(2026, 11, 10), weight_g=120,
                       created_at=datetime(2026, 11, 11)))
        db.commit()
    response = client.get('/api/interventions/compare', params={
        'round_name': 'R3', 'as_of': '2026-11-15T12:00:00'}).json()
    comparison = response['comparisons'][0]
    assert comparison['overlapping_bays'] == [1]
    assert comparison['independent_control_bays'] == 3
    assert comparison['adjusted_minus_control_observed_difference_g'] is None
    assert comparison['difference_status'] == 'overlapping_bays'
