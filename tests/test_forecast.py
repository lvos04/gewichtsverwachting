from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Bay, Cultivation, CultivationRevision, Harvest, Measurement, PlanningVersion, Stem, User, Variety, VarietyTargetVersion
from app.calculations import (alar_summary, astronomical_daylength, growth_g_per_kd_day,
                              kd_day, lamp_dli, natural_dli, phase_for_day, season_context)
from app import forecast as fc
from app.settings import AppSettings, SettingsVersion


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([User(id=1, username="admin", password_hash="x", role="admin"),
                         Bay(number=1, department=1, beds=6),
                         Variety(id=1, name="antonov", target_weight_g=105)])
        session.flush()
        session.add(VarietyTargetVersion(variety_id=1, target_weight_g=105, created_at=datetime(2010, 1, 1), author_id=1))
        session.commit()
        yield session


def add_cultivation(db, number, *, plant=None, m1_weight=20, m2_weight=40, final=100,
                    quality="verified", category="standard", late_plan=False):
    plant = plant or date(2018, 1, 1) + timedelta(days=number * 50)
    t = lambda day: datetime.combine(plant + timedelta(days=day), datetime.min.time())
    c = Cultivation(round_name=f"R{number:03d}", bay_number=1, variety_id=1, beds=6,
                    category=category, status="harvested", label_quality=quality)
    db.add(c); db.flush()
    db.add(CultivationRevision(cultivation_id=c.id, category=category,
        label_quality=quality, status="planned", created_at=t(0)))
    db.add(PlanningVersion(cultivation_id=c.id, plant_date=plant, light_out_date=plant,
                           expected_harvest_date=plant + timedelta(days=40), density=50,
                           created_at=t(0), author_id=1))
    for kind, day, weight in (("KD10", 10, m1_weight), ("KD30", 30, m2_weight)):
        m = Measurement(client_id=f"{number}-{kind}", cultivation_id=c.id, kind=kind,
                        measured_at=t(day), created_at=t(day + 1), user_id=1,
                        payload_hash="x", actual_kd_day=day)
        db.add(m); db.flush()
        for k in range(5):
            db.add(Stem(measurement_id=m.id, position=str(k), weight_g=weight,
                        length_cm=30 + day / 2, excluded=False))
    db.add(Harvest(cultivation_id=c.id, start_date=plant + timedelta(days=39),
                   end_date=plant + timedelta(days=40), weight_g=final,
                   created_at=t(41)))
    db.commit()
    return c, t


def test_point_in_time_blocks_future_plan_measurement_and_harvest(db):
    c, t = add_cultivation(db, 1)
    db.add(PlanningVersion(cultivation_id=c.id, plant_date=t(0).date(),
        light_out_date=t(0).date() + timedelta(days=5), density=999,
        created_at=t(20), author_id=1))
    db.commit()
    at_10 = fc.feature_snapshot(db, c, "KD10", t(11))
    assert at_10["features"]["density"] == 50
    assert "m2_weight_g" not in at_10["features"]
    assert at_10["features"]["m1_kd_day"] == 10
    assert fc.feature_snapshot(db, c, "KD30", t(11)) is None
    assert fc.feature_snapshot(db, c, "KD10", t(10)) is None  # entered the next day
    assert fc.forecast(db, c.id, t(11), persist=False)["point_g"] is None
    assert fc.forecast(db, c.id, t(11), persist=False)["target_g"] == 105


def test_backdated_measurement_and_unverified_label_excluded(db):
    c, t = add_cultivation(db, 1, quality="historical_assigned")
    assert fc._labeled_rows(db, "KD10") == []
    c.label_quality = "verified"
    db.add(CultivationRevision(cultivation_id=c.id, category="standard",
        label_quality="verified", status="harvested", created_at=t(42)))
    m1 = db.query(Measurement).filter_by(cultivation_id=c.id, kind="KD10").one()
    m1.created_at = t(42)  # actual harvest was already known when entered
    db.commit()
    assert fc._labeled_rows(db, "KD10") == []


def test_baseline_uses_latest_recorded_harvest_version_only(db):
    other, t = add_cultivation(db, 1, final=100)
    current, _ = add_cultivation(db, 2)
    db.add(Harvest(cultivation_id=other.id, start_date=t(39).date(),
                   end_date=t(40).date(), weight_g=120, created_at=t(45)))
    db.commit()
    assert fc._historical_baseline(db, current, t(44))[0] == 100
    assert fc._historical_baseline(db, current, t(46))[0] == 120
    db.add(Harvest(cultivation_id=other.id, start_date=t(39).date(),
                   end_date=t(40).date(), weight_g=None, created_at=t(47)))
    db.commit()
    assert fc._historical_baseline(db, current, t(48))[0] is None


def test_historical_eligibility_uses_revision_at_cutoff(db):
    other, t = add_cultivation(db, 1, final=100)
    current, _ = add_cultivation(db, 2, plant=t(30).date())
    other.category = "adjusted"
    db.add(CultivationRevision(cultivation_id=other.id, category="adjusted",
        label_quality="verified", status="harvested", created_at=t(46)))
    db.commit()
    assert fc._historical_baseline(db, current, t(45))[0] == 100
    assert fc._historical_baseline(db, current, t(47))[0] is None
    # The current row must not rewrite the answer that was knowable earlier.
    assert fc.forecast(db, current.id, t(45), persist=False)["point_g"] == 100
    assert fc.forecast(db, current.id, t(47), persist=False)["point_g"] is None


def test_unversioned_cultivation_is_not_assumed_to_exist_in_past(db):
    c = Cultivation(round_name="legacy", bay_number=1, variety_id=1, beds=6,
                    category="standard", label_quality="verified", status="harvested")
    db.add(c); db.commit()
    assert fc._revision(db, c.id, datetime(2020, 1, 1)) is None
    assert fc._historical_baseline(db, c, datetime(2020, 1, 1))[0] is None


def test_label_certified_later_does_not_leak_into_earlier_baseline(db):
    other, t = add_cultivation(db, 1, quality="ambiguous", final=100)
    current, _ = add_cultivation(db, 2, plant=t(30).date())
    other.label_quality = "verified"
    db.add(CultivationRevision(cultivation_id=other.id, category="standard",
        label_quality="verified", status="harvested", created_at=t(46)))
    db.commit()
    assert fc.forecast(db, current.id, t(45), persist=False)["point_g"] is None
    assert fc.forecast(db, current.id, t(47), persist=False)["point_g"] == 100


def test_effective_settings_control_season_and_steering_at_cutoff(db):
    c, t = add_cultivation(db, 1)
    db.add(SettingsVersion(settings=AppSettings(latitude_deg=53, steering_until_kd=12).model_dump(),
        effective_from=t(0), created_at=t(0), reason="test", user_id=1))
    db.add(SettingsVersion(settings=AppSettings(latitude_deg=55, steering_until_kd=35).model_dump(),
        effective_from=t(20), created_at=t(20), reason="test", user_id=1))
    db.commit()
    before = fc.forecast(db, c.id, t(11), persist=False)
    assert before["season"]["latitude_deg"] == 53
    assert before["actual_kd_day"] == 11
    assert before["can_steer"] is True
    assert before["steering_until_kd"] == 12
    assert fc.forecast(db, c.id, t(13), persist=False)["can_steer"] is False
    assert fc.forecast(db, c.id, t(21), persist=False)["season"]["latitude_deg"] == 55
    assert fc.feature_snapshot(db, c, "KD10", t(11))["sources"]["settings_id"] == before["settings_id"]


def test_train_uses_configured_minimum_alpha_and_round_gate(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "DATA_DIR", tmp_path)
    settings = AppSettings(min_training_cultivations=31, required_unseen_rounds=3, ridge_alpha=2.5)
    db.add(SettingsVersion(settings=settings.model_dump(), effective_from=datetime(2015, 1, 1),
                           created_at=datetime(2015, 1, 1), reason="test", user_id=1))
    db.commit()
    for i in range(30):
        add_cultivation(db, i, m1_weight=18 + i % 8, final=86 + 2 * (i % 8))
    assert fc.train_kind(db, "KD10")["required"] == 31
    add_cultivation(db, 30, m1_weight=30, final=110)
    result = fc.train_kind(db, "KD10")
    db.commit()
    assert result["metrics"]["settings"]["ridge_alpha"] == 2.5
    assert result["metrics"]["settings"]["required_unseen_rounds"] == 3
    assert result["metrics"]["release_holdout"] is None
    assert fc._load_verified(db.get(fc.ModelArtifact, result["id"])).named_steps["ridge"].alpha == 2.5


def test_rolling_rounds_and_release_gate(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "DATA_DIR", tmp_path)
    for i in range(32):
        weight = 32 if i == 31 else 18 + (i * 7 % 15)
        add_cultivation(db, i, m1_weight=weight, m2_weight=weight * 1.6,
                        final=50 + 2 * weight)
    result = fc.train_kind(db, "KD10")
    db.commit()
    assert result["metrics"]["unseen_rounds"] == 2
    rounds = result["metrics"]["evaluation_rounds"]
    assert all(set(e["train_ids"]).isdisjoint(e["eval_ids"]) for e in rounds)
    assert all(e["mae_g"] < e["baseline_mae_g"] for e in rounds)
    assert result["operational_eligible"]
    assert result["metrics"]["model"]["n"] == result["metrics"]["baseline"]["n"]
    assert 0 <= result["metrics"]["model"]["interval_80_coverage"] <= 1
    assert len(result["metrics"]["model"]["interval_radii_g"]) == 2
    assert result["metrics"]["by_variety"]["1"]["n"] == 2
    assert result["metrics"]["by_department"]["unknown"]["n"] == 2
    assert result["metrics"]["same_evaluation_ids"] == result["evaluation_ids"]
    assert fc._load_verified(db.get(fc.ModelArtifact, result["id"])) is not None


def test_minimum_and_round_group_gate(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "DATA_DIR", tmp_path)
    for i in range(29):
        add_cultivation(db, i)
    assert fc.train_kind(db, "KD10")["status"] == "insufficient_data"
    add_cultivation(db, 29)
    one = fc.train_kind(db, "KD10")
    assert not one["operational_eligible"]
    assert one["metrics"]["unseen_rounds"] == 0


def test_same_round_never_split_and_single_later_round_cannot_release(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "DATA_DIR", tmp_path)
    for i in range(30):
        add_cultivation(db, i, m1_weight=18 + i % 8, final=86 + 2 * (i % 8))
    first, _ = add_cultivation(db, 30, m1_weight=25, final=100)
    second, _ = add_cultivation(db, 31, m1_weight=28, final=110)
    second.round_name = first.round_name
    db.commit()
    result = fc.train_kind(db, "KD10")
    rounds = result["metrics"]["evaluation_rounds"]
    assert len(rounds) == 1
    assert set(rounds[0]["eval_ids"]) == {first.id, second.id}
    assert set(rounds[0]["train_ids"]).isdisjoint(rounds[0]["eval_ids"])
    assert not result["operational_eligible"]


def test_calculations_require_verified_units_and_separate_alar_scopes():
    assert lamp_dli([{"id": "1", "source_validated": True, "ppfd_umol_m2_s": 80,
                      "hours": 10, "dim_fraction": 1}], configuration_validated=True)["mol_m2_day"] == pytest.approx(2.88)
    assert lamp_dli([{"id": "2", "source_validated": False, "ppfd_umol_m2_s": 120,
                      "hours": 10, "dim_fraction": 1}], configuration_validated=True)["mol_m2_day"] is None
    assert natural_dli(100, unit=None, transmission=.7)["mol_m2_day"] is None
    assert natural_dli(100, unit="J/cm2", transmission=.7, screen_fraction=.5)["mol_m2_day"] is None
    assert natural_dli(100, unit="J/cm2", transmission=.7)["mol_m2_day"] == pytest.approx(1.505)
    class E:
        def __init__(self, scope, unit, amount):
            self.kind, self.scope, self.unit, self.amount = "alar", scope, unit, amount
    assert alar_summary([E("volvelds", "g/1000m2", 5), E("selectief", "g/10l", 8)]) == {
        "volvelds_g_per_1000m2": 5, "selectief_treatments": 1, "selectief_concentrations_g_per_10l": [8]}
    assert growth_g_per_kd_day(10, 30, 10, 30) == 1
    assert phase_for_day(date(2026, 2, 1), date(2026, 1, 1), date(2026, 1, 20),
                         date(2026, 1, 30), date(2026, 2, 20), date(2026, 3, 1)) == "M1_tot_M2"


def test_seasonal_direction_solstice_and_leap_year():
    assert astronomical_daylength(date(2026, 6, 21)) > astronomical_daylength(date(2026, 12, 21))
    assert season_context(date(2026, 2, 1))["trend"] == "longer"
    assert season_context(date(2026, 9, 1))["trend"] == "shorter"
    assert season_context(date(2026, 6, 21))["trend"] == "turning"
    assert season_context(date(2024, 2, 29))["trend"] == "longer"
    remainder = season_context(date(2026, 12, 1), date(2027, 2, 1))["expected_remainder"]
    assert remainder["days"] == 62
    assert all(x["forecast_weight_g"] is None for x in remainder["scenarios"])


def test_scenario_recording_time_prevents_backdated_issue_leak(db):
    c, _ = add_cultivation(db, 1)
    scenario = fc.WeatherScenario(cultivation_id=c.id, issued_at=datetime(2018, 1, 1),
        valid_at=datetime(2018, 1, 2), created_at=datetime(2020, 1, 1), name="dark")
    db.add(scenario); db.commit()
    assert fc.list_scenarios(cultivation_id=c.id, as_of=datetime(2019, 1, 1, tzinfo=fc.timezone.utc),
                             db=db, user=None)["scenarios"] == []
    assert len(fc.list_scenarios(cultivation_id=c.id, as_of=datetime(2021, 1, 1, tzinfo=fc.timezone.utc),
                                 db=db, user=None)["scenarios"]) == 1
