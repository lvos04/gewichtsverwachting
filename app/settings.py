"""Audited effective-date application settings, managed through the web UI."""
from datetime import datetime, timezone
import os
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from .db import Base, get_db
from .models import User, utcnow
from .auth import audit, require_admin, require_user

router = APIRouter()

class AppSettings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra='forbid')
    latitude_deg: float = Field(default=52.0, ge=-66, le=66)
    measurement_tolerance_days: int = Field(default=1, ge=0, le=7)
    steering_until_kd: int = Field(default=35, ge=10, le=100)
    session_hours: int = Field(default=12, ge=1, le=168)
    login_max_attempts: int = Field(default=5, ge=3, le=50)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)
    backup_interval_hours: int = Field(default=24, ge=1, le=168)
    backup_keep_days: int = Field(default=0, ge=0, le=3650)
    ridge_alpha: float = Field(default=10.0, gt=0, le=10000)
    min_training_cultivations: int = Field(default=30, ge=30, le=10000)
    required_unseen_rounds: int = Field(default=2, ge=2, le=20)
    model_feature_profile: Literal['basic', 'light', 'climate'] = 'basic'
    environment_min_coverage: float = Field(default=1.0, ge=0.9, le=1.0)
    environment_include_daylight_estimate: bool = False
    environment_include_co2: bool = False
    environment_min_training_cultivations: int = Field(default=60, ge=30, le=10000)
    priority_algemeen_bedrijf: int = Field(default=50, ge=0, le=100)
    priority_klimaat: int = Field(default=40, ge=0, le=100)
    priority_belichting: int = Field(default=40, ge=0, le=100)
    priority_kranen: int = Field(default=40, ge=0, le=100)
    priority_algemeen_afdeling: int = Field(default=20, ge=0, le=100)

class SettingsVersion(Base):
    __tablename__ = 'settings_versions'
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON)
    effective_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reason: Mapped[str] = mapped_column(String(2000))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)

class SettingsChange(BaseModel):
    settings: AppSettings
    effective_from: datetime | None = None
    reason: str = Field(min_length=3, max_length=2000)

def effective_settings(db: Session, as_of: datetime | None = None) -> AppSettings:
    as_of = as_of or utcnow()
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    row = db.scalar(select(SettingsVersion).where(SettingsVersion.effective_from <= as_of,
                    SettingsVersion.created_at <= as_of).order_by(SettingsVersion.effective_from.desc(), SettingsVersion.id.desc()))
    if row:
        return AppSettings.model_validate(row.settings)
    # Legacy environment value is only a visible initial default; UI saves take precedence.
    return AppSettings(latitude_deg=float(os.environ.get('GREENHOUSE_LATITUDE_DEG', '52.0')))

@router.get('/api/settings')
def settings(user: User = Depends(require_user), db: Session = Depends(get_db)):
    versions = db.scalars(select(SettingsVersion).order_by(SettingsVersion.id.desc()).limit(200)).all() if user.role == 'admin' else []
    return {'settings': effective_settings(db).model_dump(), 'history': [
        {'id': v.id, 'settings': v.settings, 'effective_from': v.effective_from.isoformat() + 'Z',
         'created_at': v.created_at.isoformat() + 'Z', 'reason': v.reason} for v in versions],
        'infrastructure': 'Containerpoort, opslagmap en HTTPS worden in TrueNAS en de tunnel beheerd. Externe weer- en AI-providers staan uit.'}

@router.post('/api/settings')
def change_settings(data: SettingsChange, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    now = utcnow()
    effective = data.effective_from or now
    if effective.tzinfo is None and data.effective_from:
        effective = effective.replace(tzinfo=ZoneInfo('Europe/Amsterdam'))
    if effective.tzinfo is not None:
        effective = effective.astimezone(timezone.utc).replace(tzinfo=None)
    if (now - effective).total_seconds() > 60:
        raise HTTPException(422, 'Instellingen mogen niet achteraf worden gewijzigd. Kies nu of een toekomstig moment.')
    row = SettingsVersion(settings=data.settings.model_dump(), effective_from=effective,
                          created_at=now, reason=data.reason.strip(), user_id=user.id)
    db.add(row)
    db.flush()
    audit(db, user, 'settings_changed', f'settings:{row.id}', {'settings': row.settings, 'effective_from': effective.isoformat(), 'reason': row.reason})
    db.commit()
    return {'id': row.id, 'settings': row.settings, 'effective_from': effective.isoformat() + 'Z'}
