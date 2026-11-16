"""Point-in-time forecasts and round-separated Ridge experiments."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import joblib
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .calculations import growth_g_per_kd_day, kd_day, stem_summary, season_context, local_date
from .db import Base, DATA_DIR, get_db
from .models import Bay, Cultivation, CultivationRevision, Harvest, MappingVersion, Measurement, PlanningVersion, Stem, Variety
from .auth import audit, require_user, require_write, require_admin
from .settings import AppSettings, SettingsVersion, effective_settings

router = APIRouter()
FEATURE_VERSION = "kd-point-in-time-v2-settings"
NUMERIC_KD10 = ["plant_week_sin", "plant_week_cos", "stage_daylength_hours", "stage_daylength_trend", "density", "m1_weight_g", "m1_length_cm", "m1_kd_day"]
NUMERIC_KD30 = NUMERIC_KD10 + ["m2_weight_g", "m2_length_cm", "m2_kd_day", "growth_g_day"]
ELIGIBLE_QUALITY = {"verified", "reliable"}
NL = ZoneInfo("Europe/Amsterdam")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ModelArtifact(Base):
    __tablename__ = "model_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operational_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    feature_version: Mapped[str] = mapped_column(String(80))
    features: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    training_ids: Mapped[list] = mapped_column(JSON)
    evaluation_ids: Mapped[list] = mapped_column(JSON)
    data_hash: Mapped[str] = mapped_column(String(64))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(String(300))
    interval_radius_g: Mapped[float] = mapped_column(Float)


class ForecastSnapshot(Base):
    __tablename__ = "forecast_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    cultivation_id: Mapped[int] = mapped_column(ForeignKey("cultivations.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("model_artifacts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    point_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    low_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_ids: Mapped[dict] = mapped_column(JSON)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response: Mapped[dict] = mapped_column(JSON)


class WeatherScenario(Base):
    __tablename__ = "weather_scenarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    cultivation_id: Mapped[int] = mapped_column(ForeignKey("cultivations.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    valid_at: Mapped[datetime] = mapped_column(DateTime)
    name: Mapped[str] = mapped_column(String(100))
    outside_radiation_j_cm2: Mapped[float | None] = mapped_column(Float, nullable=True)
    outside_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_lamp_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScenarioInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    cultivation_id: int
    issued_at: datetime
    valid_at: datetime
    name: str = Field(min_length=1, max_length=100)
    outside_radiation_j_cm2: float | None = Field(default=None, ge=0)
    outside_temp_c: float | None = None
    cloud_fraction: float | None = Field(default=None, ge=0, le=1)
    planned_lamp_hours: float | None = Field(default=None, ge=0, le=24)

    @model_validator(mode="after")
    def check_times(self):
        if utc_naive(self.valid_at) < utc_naive(self.issued_at):
            raise ValueError("valid_at moet na issued_at liggen")
        if utc_naive(self.issued_at) > utcnow():
            raise ValueError("issued_at mag niet in de toekomst liggen")
        return self


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value.replace(tzinfo=NL).astimezone(timezone.utc).replace(tzinfo=None)


def _setting_context(db: Session, as_of: datetime) -> tuple[AppSettings, SettingsVersion | None]:
    row = db.scalar(select(SettingsVersion).where(
        SettingsVersion.effective_from <= as_of, SettingsVersion.created_at <= as_of)
        .order_by(SettingsVersion.effective_from.desc(), SettingsVersion.id.desc()))
    return effective_settings(db, as_of), row


def _plan(db: Session, cultivation_id: int, as_of: datetime):
    return db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id == cultivation_id,
        PlanningVersion.created_at <= as_of).order_by(PlanningVersion.created_at.desc(), PlanningVersion.id.desc()))


def _revision(db: Session, cultivation_id: int, as_of: datetime):
    """No legacy fallback: an unversioned status cannot be reconstructed."""
    return db.scalar(select(CultivationRevision).where(
        CultivationRevision.cultivation_id == cultivation_id,
        CultivationRevision.created_at <= as_of)
        .order_by(CultivationRevision.created_at.desc(), CultivationRevision.id.desc()))


def _eligible(revision: CultivationRevision | None) -> bool:
    return bool(revision and revision.category == "standard" and
                revision.label_quality in ELIGIBLE_QUALITY)


def _measurement(db: Session, cultivation_id: int, kind: str, as_of: datetime):
    return db.scalar(select(Measurement).where(Measurement.cultivation_id == cultivation_id,
        Measurement.kind == kind, Measurement.created_at <= as_of, Measurement.measured_at <= as_of)
        .order_by(Measurement.measured_at.desc(), Measurement.id.desc()))


def _measurement_summary(db: Session, measurement: Measurement):
    stems = db.scalars(select(Stem).where(Stem.measurement_id == measurement.id)).all()
    if len(stems) != 5:
        return None
    try:
        result = stem_summary(stems)
    except ValueError:
        return None
    return result if result["n"] >= 3 else None


def feature_snapshot(db: Session, cultivation: Cultivation, kind: str, as_of: datetime) -> dict | None:
    """Only facts both observed and recorded by as_of; never harvest fields."""
    plan = _plan(db, cultivation.id, as_of)
    m1 = _measurement(db, cultivation.id, "KD10", as_of)
    if not plan or not plan.plant_date or not plan.light_out_date or not m1:
        return None
    s1 = _measurement_summary(db, m1)
    if not s1:
        return None
    week = plan.plant_date.isocalendar().week
    angle = 2 * np.pi * week / 52.1775
    settings, setting_row = _setting_context(db, as_of)
    latitude = settings.latitude_deg
    season = season_context(local_date(as_of), latitude_deg=latitude)
    features = {"variety_id": str(cultivation.variety_id),
                "plant_week_sin": float(np.sin(angle)), "plant_week_cos": float(np.cos(angle)),
                "stage_daylength_hours": season["daylength_hours"],
                "stage_daylength_trend": season["trend_hours_per_day"],
                "density": float(plan.density) if plan.density is not None else 0.0,
                "m1_weight_g": s1["weight_g"], "m1_length_cm": s1["length_cm"],
                "m1_kd_day": kd_day(plan.light_out_date, m1.measured_at)}
    sources = {"planning_id": plan.id, "m1_id": m1.id, "settings_id": setting_row.id if setting_row else None}
    available_at = max(plan.created_at, m1.created_at, m1.measured_at)
    if kind == "KD30":
        m2 = _measurement(db, cultivation.id, "KD30", as_of)
        if not m2:
            return None
        s2 = _measurement_summary(db, m2)
        if not s2 or m2.measured_at <= m1.measured_at:
            return None
        m2_kd = kd_day(plan.light_out_date, m2.measured_at)
        try:
            growth = growth_g_per_kd_day(s1["weight_g"], s2["weight_g"],
                                         features["m1_kd_day"], m2_kd)
        except ValueError:
            return None
        features.update(m2_weight_g=s2["weight_g"], m2_length_cm=s2["length_cm"],
                        m2_kd_day=m2_kd, growth_g_day=growth)
        sources["m2_id"] = m2.id
        available_at = max(available_at, m2.created_at, m2.measured_at)
    return {"features": features, "sources": sources, "available_at": available_at,
            "plant_date": plan.plant_date}


def _labeled_rows(db: Session, kind: str) -> list[dict]:
    rows = []
    for cultivation in db.scalars(select(Cultivation)).all():
        if not _eligible(_revision(db, cultivation.id, utcnow())):
            continue
        harvest = db.scalar(select(Harvest).where(Harvest.cultivation_id == cultivation.id)
                            .order_by(Harvest.created_at.desc(), Harvest.id.desc()))
        if (not harvest or not harvest.end_date or harvest.weight_g is None or harvest.weight_g <= 0
                or (harvest.quality_classes and len(harvest.quality_classes) > 1)):
            continue
        # Experimental cohort requires both between-crop measurements.
        complete = [db.scalar(select(Measurement).where(Measurement.cultivation_id == cultivation.id,
                   Measurement.kind == measurement_kind).order_by(Measurement.created_at, Measurement.id))
                    for measurement_kind in ("KD10", "KD30")]
        if (any(m is None or _measurement_summary(db, m) is None or
                max(m.created_at, m.measured_at) >= harvest.created_at or
                local_date(m.measured_at) >= harvest.end_date for m in complete)
                or complete[0].measured_at >= complete[1].measured_at):
            continue
        # A stored measurement can be entered late. Its own availability time
        # is the stage timestamp; final harvest data must not yet be available.
        measurements = db.scalars(select(Measurement).where(Measurement.cultivation_id == cultivation.id,
            Measurement.kind == kind).order_by(Measurement.created_at, Measurement.id)).all()
        for stage in measurements:
            cutoff = max(stage.created_at, stage.measured_at)
            if cutoff >= harvest.created_at or local_date(cutoff) >= harvest.end_date:
                continue
            snapshot = feature_snapshot(db, cultivation, kind, cutoff)
            if snapshot and snapshot["sources"].get("m2_id" if kind == "KD30" else "m1_id") == stage.id:
                rows.append({"id": cultivation.id, "round": cultivation.round_name,
                             "variety_id": cultivation.variety_id, "bay_number": cultivation.bay_number,
                             "plant_date": snapshot["plant_date"],
                             "available_at": snapshot["available_at"], "harvest_recorded_at": harvest.created_at,
                             "y": float(harvest.weight_g), "features": snapshot["features"],
                             "sources": snapshot["sources"], "harvest_id": harvest.id})
                break
    return rows


def _pipeline(kind: str, alpha: float = 10.0) -> Pipeline:
    return Pipeline([("encode", DictVectorizer()),
                     ("scale", StandardScaler(with_mean=False)),
                     ("ridge", Ridge(alpha=alpha))])


def _fit(rows: list[dict], kind: str, alpha: float = 10.0):
    model = _pipeline(kind, alpha)
    model.fit([r["features"] for r in rows], [r["y"] for r in rows])
    return model


def _baseline(rows: list[dict], training: list[dict]) -> list[float]:
    predictions = []
    for row in rows:
        eligible = [t for t in training if t["harvest_recorded_at"] <= row["available_at"]
                    and t["round"] != row["round"]]
        same = [t["y"] for t in eligible if t["variety_id"] == row["variety_id"]]
        predictions.append(float(np.mean(same if same else [t["y"] for t in eligible])) if eligible else float("nan"))
    return predictions


def _metrics(actual: list[float], predicted: list[float], radius: float | None = None) -> dict:
    error = np.asarray(predicted) - np.asarray(actual)
    result = {"n": len(actual), "mae_g": float(np.mean(np.abs(error))),
              "bias_g": float(np.mean(error)), "median_abs_error_g": float(np.median(np.abs(error))),
              "p90_abs_error_g": float(np.quantile(np.abs(error), .9))}
    if radius is not None:
        result["interval_80_coverage"] = float(np.mean(np.abs(error) <= radius))
    return result


def _department_at(db: Session, row: dict) -> str:
    day = local_date(row["available_at"])
    mapping = db.scalar(select(MappingVersion).where(
        MappingVersion.bay_number == row["bay_number"],
        MappingVersion.object_type == "department",
        MappingVersion.created_at <= row["available_at"],
        MappingVersion.valid_from <= day,
        (MappingVersion.valid_until.is_(None) | (MappingVersion.valid_until >= day)))
        .order_by(MappingVersion.created_at.desc(), MappingVersion.id.desc()))
    return str(mapping.object_number) if mapping else "unknown"


def _season_type(day: date) -> str:
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}[day.month]


def _breakdown(rows: list[dict], actual: list[float], predicted: list[float], key: str) -> dict:
    groups: dict[str, tuple[list[float], list[float]]] = {}
    for row, y, p in zip(rows, actual, predicted):
        a, b = groups.setdefault(str(row[key]), ([], []))
        a.append(y); b.append(p)
    return {name: _metrics(a, b) for name, (a, b) in groups.items()}


def train_kind(db: Session, kind: str) -> dict:
    settings, setting_row = _setting_context(db, utcnow())
    min_train = settings.min_training_cultivations
    required_rounds = settings.required_unseen_rounds
    alpha = settings.ridge_alpha
    rows = _labeled_rows(db, kind)
    if len(rows) < min_train:
        return {"kind": kind, "status": "insufficient_data", "complete_standard_cultivations": len(rows),
                "required": min_train, "settings_id": setting_row.id if setting_row else None}
    rounds: dict[str, list[dict]] = {}
    for row in rows:
        rounds.setdefault(row["round"], []).append(row)
    ordered = sorted(rounds, key=lambda name: (min(r["plant_date"] for r in rounds[name]), name))
    evaluations = []
    eval_ids = []
    all_actual, all_pred, all_base = [], [], []
    all_evaluated_rows, all_covered, all_fold_radii = [], [], []
    for index, name in enumerate(ordered):
        test = rounds[name]
        first_available = min(r["available_at"] for r in test)
        prior = [r for old in ordered[:index] for r in rounds[old]
                 if r["harvest_recorded_at"] <= first_available]
        if len(prior) < min_train:
            continue
        baseline = _baseline(test, prior)
        if not np.all(np.isfinite(baseline)):
            continue
        model = _fit(prior, kind, alpha)
        prior_residuals = [abs(r["y"] - float(p)) for r, p in zip(
            prior, model.predict([r["features"] for r in prior]))]
        fold_radius = float(max(1.0, np.quantile(prior_residuals, .8, method="higher")))
        predicted = [float(x) for x in model.predict([r["features"] for r in test])]
        actual = [r["y"] for r in test]
        eval_ids.extend(r["id"] for r in test)
        all_actual.extend(actual); all_pred.extend(predicted); all_base.extend(baseline)
        all_evaluated_rows.extend(test)
        all_fold_radii.extend([fold_radius] * len(test))
        all_covered.extend(abs(y - p) <= fold_radius for y, p in zip(actual, predicted))
        evaluations.append({"round": name, "round_index": index, "train_ids": [r["id"] for r in prior],
                            "eval_ids": [r["id"] for r in test],
                            "interval_radius_g_before_evaluation": fold_radius,
                            "interval_80_coverage": float(np.mean([abs(y - p) <= fold_radius for y, p in zip(actual, predicted)])),
                            "mae_g": _metrics(actual, predicted)["mae_g"],
                            "baseline_mae_g": _metrics(actual, baseline)["mae_g"]})
    final_model = _fit(rows, kind, alpha)
    train_residuals = [abs(r["y"] - float(p)) for r, p in zip(rows, final_model.predict([r["features"] for r in rows]))]
    # Use unseen rolling-origin errors to set the final future interval when
    # possible; evaluation coverage above uses ONLY each fold's prior radius.
    radius_source = [abs(y - p) for y, p in zip(all_actual, all_pred)] or train_residuals
    radius = float(max(1.0, np.quantile(radius_source, .8, method="higher")))
    # Release evidence uses ONE frozen model trained before all final rounds.
    # The rolling folds above remain useful diagnostics, but their second
    # model may already have learned the first holdout round.
    release_holdout = None
    if len(ordered) >= required_rounds:
        holdout_names = ordered[-required_rounds:]
        first_available = min(r["available_at"] for r in rounds[holdout_names[0]])
        fixed_prior = [r for name in ordered[:-required_rounds] for r in rounds[name]
                       if r["harvest_recorded_at"] <= first_available]
        if len(fixed_prior) >= min_train:
            frozen = _fit(fixed_prior, kind, alpha)
            holdout_results = []
            for name in holdout_names:
                test = rounds[name]
                predicted = [float(x) for x in frozen.predict([r["features"] for r in test])]
                baseline = _baseline(test, fixed_prior)
                if not np.all(np.isfinite(baseline)):
                    break
                holdout_results.append({"round": name, "eval_ids": [r["id"] for r in test],
                                        "mae_g": _metrics([r["y"] for r in test], predicted)["mae_g"],
                                        "baseline_mae_g": _metrics([r["y"] for r in test], baseline)["mae_g"]})
            if len(holdout_results) == required_rounds:
                release_holdout = {"frozen_train_ids": [r["id"] for r in fixed_prior],
                                   "rounds": holdout_results,
                                   "both_improved": all(e["mae_g"] < e["baseline_mae_g"] for e in holdout_results)}
    improved = bool(release_holdout and release_holdout["both_improved"])
    metrics = {"evaluation_rounds": evaluations, "unseen_rounds": len(evaluations),
               "release_gate": "passed" if improved else "experimental",
               "release_holdout": release_holdout,
               "settings_id": setting_row.id if setting_row else None,
               "settings_effective_from": setting_row.effective_from.isoformat() if setting_row else None,
               "settings": {"ridge_alpha": alpha, "min_training_cultivations": min_train,
                            "required_unseen_rounds": required_rounds, "latitude_deg": settings.latitude_deg},
               "historical_reference_mae_g": 3.8,
               "historical_reference_comparable": False,
               "interval_method": "rolling_origin_residual_80pct" if all_actual else "training_residual_80pct_unvalidated",
               "evaluation_coverage_method": "fold radius fixed from prior-round training residuals"}
    if all_actual:
        model_metrics = _metrics(all_actual, all_pred)
        model_metrics["interval_80_coverage"] = float(np.mean(all_covered))
        model_metrics["interval_radii_g"] = all_fold_radii
        for row in all_evaluated_rows:
            row["season_type"] = _season_type(row["plant_date"])
            row["department"] = _department_at(db, row)
        metrics.update(model=model_metrics,
                       baseline=_metrics(all_actual, all_base),
                       same_evaluation_ids=eval_ids,
                       by_variety=_breakdown(all_evaluated_rows, all_actual, all_pred, "variety_id"),
                       by_season=_breakdown(all_evaluated_rows, all_actual, all_pred, "season_type"),
                       by_department=_breakdown(all_evaluated_rows, all_actual, all_pred, "department"))
    serialized_rows = [{"id": r["id"], "round": r["round"], "features": r["features"],
                        "y": r["y"], "sources": r["sources"], "harvest_id": r["harvest_id"],
                        "available_at": r["available_at"].isoformat(),
                        "harvest_recorded_at": r["harvest_recorded_at"].isoformat()}
                       for r in sorted(rows, key=lambda x: x["id"])]
    data_hash = hashlib.sha256(json.dumps({"rows": serialized_rows, "feature_version": FEATURE_VERSION,
        "settings_id": setting_row.id if setting_row else None, "ridge_alpha": alpha,
        "min_training_cultivations": min_train, "required_unseen_rounds": required_rounds},
        sort_keys=True).encode()).hexdigest()
    model_dir = DATA_DIR / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / f"{kind.lower()}-{FEATURE_VERSION}-{data_hash}.joblib"
    with tempfile.NamedTemporaryFile(dir=model_dir, prefix=".model-", suffix=".tmp", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        joblib.dump(final_model, temporary)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(temporary.read_bytes()).hexdigest():
                raise RuntimeError("bestaand modelbestand heeft afwijkende inhoud")
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = ModelArtifact(kind=kind, feature_version=FEATURE_VERSION,
        features={"categorical": ["variety_id"], "numeric": NUMERIC_KD10 if kind == "KD10" else NUMERIC_KD30},
        metrics=metrics, training_ids=[r["id"] for r in rows], evaluation_ids=eval_ids,
        data_hash=data_hash, artifact_hash=artifact_hash, artifact_path=path.name,
        interval_radius_g=radius, operational_eligible=improved)
    db.add(artifact); db.flush()
    return model_dict(artifact)


def model_dict(model: ModelArtifact) -> dict:
    return {"id": model.id, "kind": model.kind, "created_at": model.created_at.isoformat(),
            "active": model.active, "activated_at": model.activated_at.isoformat() if model.activated_at else None,
            "operational_eligible": model.operational_eligible,
            "feature_version": model.feature_version, "features": model.features,
            "metrics": model.metrics, "training_ids": model.training_ids,
            "evaluation_ids": model.evaluation_ids, "data_hash": model.data_hash,
            "artifact_hash": model.artifact_hash, "interval_radius_g": model.interval_radius_g}


@router.get("/api/models")
def list_models(db: Session = Depends(get_db), user=Depends(require_user)):
    settings, setting_row = _setting_context(db, utcnow())
    return {"models": [model_dict(m) for m in db.scalars(select(ModelArtifact).order_by(ModelArtifact.id.desc())).all()],
            "required_complete_standard": settings.min_training_cultivations,
            "required_unseen_rounds": settings.required_unseen_rounds,
            "settings_id": setting_row.id if setting_row else None,
            "external_weather_provider": "off"}


@router.post("/api/models/train")
def train_models(db: Session = Depends(get_db), user=Depends(require_admin)):
    result = [train_kind(db, kind) for kind in ("KD10", "KD30")]
    audit(db, user, "models.train", "models", {"result": [{"kind": r["kind"], "status": r.get("status", r.get("metrics", {}).get("release_gate"))} for r in result]})
    db.commit()
    return {"models": result}


@router.post("/api/models/{model_id}/activate")
def activate_model(model_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    model = db.get(ModelArtifact, model_id)
    if not model:
        raise HTTPException(404, "model niet gevonden")
    if not model.operational_eligible:
        raise HTTPException(409, "model heeft vrijgavepoort niet gehaald")
    for prior in db.scalars(select(ModelArtifact).where(ModelArtifact.kind == model.kind, ModelArtifact.active == True)).all():
        prior.active = False
    model.active = True
    model.activated_at = utcnow()
    audit(db, user, "models.activate", model.id, {"kind": model.kind, "data_hash": model.data_hash})
    db.commit()
    return model_dict(model)


def _historical_baseline(db: Session, cultivation: Cultivation, as_of: datetime) -> tuple[float | None, list[int]]:
    latest: dict[int, tuple[Harvest, Cultivation]] = {}
    for harvest, other in db.execute(select(Harvest, Cultivation).join(Cultivation, Harvest.cultivation_id == Cultivation.id)
            .where(Harvest.created_at <= as_of, Harvest.cultivation_id != cultivation.id,
                   Cultivation.round_name != cultivation.round_name)).all():
        prior = latest.get(other.id)
        if prior is None or (harvest.created_at, harvest.id) > (prior[0].created_at, prior[0].id):
            latest[other.id] = (harvest, other)
    candidates = [(h, c) for h, c in latest.values() if h.end_date and
                  h.end_date <= local_date(as_of) and h.weight_g is not None and h.weight_g > 0 and
                  not (h.quality_classes and len(h.quality_classes) > 1) and
                  _eligible(_revision(db, c.id, as_of))]
    same = [h for h, c in candidates if c.variety_id == cultivation.variety_id]
    chosen = same or [h for h, _ in candidates]
    return (float(np.mean([h.weight_g for h in chosen])), [h.id for h in chosen]) if chosen else (None, [])


def _load_verified(model: ModelArtifact):
    path = DATA_DIR / "models" / model.artifact_path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != model.artifact_hash:
        raise HTTPException(503, "modelbestand ontbreekt of hash wijkt af")
    return joblib.load(path)


def forecast(db: Session, cultivation_id: int, as_of: datetime, *, persist: bool = True) -> dict:
    cultivation = db.get(Cultivation, cultivation_id)
    if not cultivation:
        raise HTTPException(404, "kapteelt niet gevonden")
    if as_of > utcnow():
        raise HTTPException(422, "peildatum mag niet in de toekomst liggen")
    if _revision(db, cultivation_id, as_of) is None:
        raise HTTPException(404, "kapteelt was op deze peildatum nog niet geregistreerd")
    variety = db.get(Variety, cultivation.variety_id)
    target = variety.target_weight_g if variety else None
    plan = _plan(db, cultivation_id, as_of)
    settings, setting_row = _setting_context(db, as_of)
    season = season_context(local_date(as_of), plan.expected_harvest_date if plan else None,
                            settings.latitude_deg)
    actual_kd = kd_day(plan.light_out_date, as_of) if plan and plan.light_out_date else None
    can_steer = actual_kd is not None and 0 <= actual_kd <= settings.steering_until_kd
    snapshot = feature_snapshot(db, cultivation, "KD30", as_of)
    kind = "KD30" if snapshot else "KD10"
    snapshot = snapshot or feature_snapshot(db, cultivation, "KD10", as_of)
    model = db.scalar(select(ModelArtifact).where(ModelArtifact.kind == kind,
        ModelArtifact.created_at <= as_of).order_by(ModelArtifact.active.desc(), ModelArtifact.id.desc())) if snapshot else None
    baseline, baseline_ids = _historical_baseline(db, cultivation, as_of)
    point = low = high = None
    status = "unknown"
    warnings = []
    if model and snapshot:
        prediction = float(_load_verified(model).predict([snapshot["features"]])[0])
        point = prediction
        low, high = prediction - model.interval_radius_g, prediction + model.interval_radius_g
        status = "operational" if (model.active and model.operational_eligible and
            model.activated_at is not None and model.activated_at <= as_of) else "experimental"
        if status == "experimental":
            warnings.append("Model is experimenteel en niet vrijgegeven voor operationeel bijsturen")
    elif baseline is not None:
        point, status = baseline, "baseline"
        warnings.append("Alleen historisch gemiddelde; geen gevalideerde KD-prognose")
    else:
        warnings.append("Geen eerdere betrouwbare oogsten beschikbaar voor een prognose")
    if not snapshot:
        warnings.append("Geen volledige KD10- of KD30-meting beschikbaar op deze peildatum")
    result = {"cultivation_id": cultivation_id, "as_of": as_of.isoformat(), "status": status,
              "point_g": point, "low_g": low, "high_g": high, "target_g": target,
              "delta_to_target_g": point - target if point is not None and target is not None else None,
              "model_kind": kind if snapshot else None, "model_id": model.id if model and snapshot else None,
              "baseline_g": baseline, "warnings": warnings,
              "source_ids": {**(snapshot["sources"] if snapshot else {}), "baseline_harvest_ids": baseline_ids},
              "feature_version": FEATURE_VERSION if snapshot else None}
    result["season"] = season
    result["settings_id"] = setting_row.id if setting_row else None
    result["model_training_settings_id"] = model.metrics.get("settings_id") if model and snapshot else None
    result["actual_kd_day"] = actual_kd
    result["steering_until_kd"] = settings.steering_until_kd
    result["can_steer"] = can_steer
    result["steering_note"] = "Alleen menselijke beoordeling; geen automatische kas- of middelenwijziging"
    if persist:
        db.add(ForecastSnapshot(cultivation_id=cultivation_id, as_of=as_of, model_id=result["model_id"],
            status=status, point_g=point, low_g=low, high_g=high,
            source_ids=result["source_ids"], features=snapshot["features"] if snapshot else None,
            response=result))
        db.commit()
    return result


@router.get("/api/cultivations/{cultivation_id}/forecast")
def get_forecast(cultivation_id: int, as_of: datetime | None = None,
                 db: Session = Depends(get_db), user=Depends(require_user)):
    return forecast(db, cultivation_id, utc_naive(as_of) if as_of else utcnow())


@router.get("/api/scenarios")
def list_scenarios(cultivation_id: int | None = None, as_of: datetime | None = None,
                   db: Session = Depends(get_db), user=Depends(require_user)):
    cutoff = utc_naive(as_of) if as_of else utcnow()
    query = select(WeatherScenario).where(WeatherScenario.issued_at <= cutoff,
                                         WeatherScenario.created_at <= cutoff)
    if cultivation_id is not None:
        query = query.where(WeatherScenario.cultivation_id == cultivation_id)
    items = db.scalars(query.order_by(WeatherScenario.issued_at.desc())).all()
    return {"provider": "off", "scenarios": [scenario_dict(x) for x in items]}


def scenario_dict(x: WeatherScenario) -> dict:
    return {"id": x.id, "cultivation_id": x.cultivation_id, "issued_at": x.issued_at.isoformat(),
            "valid_at": x.valid_at.isoformat(), "recorded_at": x.created_at.isoformat(), "name": x.name,
            "outside_radiation_j_cm2": x.outside_radiation_j_cm2,
            "outside_temp_c": x.outside_temp_c, "cloud_fraction": x.cloud_fraction,
            "planned_lamp_hours": x.planned_lamp_hours,
            "forecast_weight_g": None, "note": "Scenario is opgeslagen, maar zonder gevalideerd effectmodel geen gewichtsverschil berekend"}


@router.post("/api/scenarios")
def create_scenario(data: ScenarioInput, db: Session = Depends(get_db), user=Depends(require_write)):
    if not db.get(Cultivation, data.cultivation_id):
        raise HTTPException(404, "kapteelt niet gevonden")
    item = WeatherScenario(**{**data.model_dump(), "issued_at": utc_naive(data.issued_at),
                              "valid_at": utc_naive(data.valid_at)})
    audit(db, user, "scenarios.create", data.cultivation_id, {"issued_at": item.issued_at.isoformat(), "valid_at": item.valid_at.isoformat()})
    db.add(item); db.commit(); db.refresh(item)
    return scenario_dict(item)
