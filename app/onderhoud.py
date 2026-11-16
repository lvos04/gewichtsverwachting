"""Achtergrondonderhoud: prognoses bijwerken, opschonen en back-upbeheer.

* Prognoses worden alleen opnieuw doorgerekend als er sinds de vorige ronde een
  relevant feit is vastgelegd (dirty-vlag via een SQLAlchemy-gebeurtenis), als de
  lokale datum is veranderd, of als een vooruit ingepland feit (bijv. een
  instelling met toekomstige ingangsdatum) intussen is ingegaan.
* Dagelijks worden verlopen sessies, verlopen MFA-stappen en oude aanmeldpogingen
  verwijderd. Auditregels worden alleen ingekort als `audit_keep_days` > 0
  (standaard 0 = onbeperkt bewaren).
* Back-upstatus met schijfruimte en bewaaradvies voor het Beheer-scherm.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from .auth import audit, require_admin
from .db import DATA_DIR, SessionLocal, get_db
from .models import (
    AuditLog,
    AuthSession,
    CropProfileVersion,
    Cultivation,
    CultivationRevision,
    Event,
    Harvest,
    LoginAttempt,
    MappingRevision,
    Measurement,
    MfaChallenge,
    PlanningVersion,
    User,
    VarietyTargetVersion,
    utcnow,
)

log = logging.getLogger("chrysant")
router = APIRouter()
ADVIES_BEWAARDAGEN = 30


# --- Prognoses: alleen herberekenen na wijziging ----------------------------------------------
def _tracked_classes():
    from . import forecast, importer, legacy_lighting, settings

    return (
        Measurement,
        PlanningVersion,
        Event,
        Harvest,
        Cultivation,
        CultivationRevision,
        MappingRevision,
        VarietyTargetVersion,
        CropProfileVersion,
        importer.PrivaSource,
        importer.Calibration,
        forecast.ModelArtifact,
        forecast.ModelActivation,
        legacy_lighting.LegacyLightingJob,
        settings.SettingsVersion,
    )


class _RefreshState:
    def __init__(self):
        self.dirty = threading.Event()
        self.dirty.set()  # na het starten altijd één volledige controle
        self.last_date: date | None = None
        self.next_due: datetime | None = None
        self._classes: tuple | None = None

    def classes(self):
        if self._classes is None:
            self._classes = _tracked_classes()
        return self._classes


STATE = _RefreshState()


@event.listens_for(Session, "after_flush")
def _mark_dirty(session, _context):
    if STATE.dirty.is_set():
        return
    tracked = STATE.classes()
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, tracked):
            STATE.dirty.set()
            return


def mark_forecasts_dirty() -> None:
    STATE.dirty.set()


def _global_change_times(db: Session, now: datetime) -> tuple[list[datetime], datetime | None]:
    """Laatste ingegane wijziging per bron, plus het eerstvolgende vooruit geplande moment."""
    from . import forecast, importer, legacy_lighting, settings

    columns = (
        importer.PrivaSource.activated_at,
        importer.PrivaSource.revoked_at,
        importer.Calibration.created_at,
        MappingRevision.created_at,
        VarietyTargetVersion.created_at,
        CropProfileVersion.created_at,
        CultivationRevision.created_at,
        forecast.ModelArtifact.created_at,
        forecast.ModelActivation.created_at,
        Harvest.created_at,
        legacy_lighting.LegacyLightingJob.activated_at,
        legacy_lighting.LegacyLightingJob.revoked_at,
    )
    times, future = [], []
    for column in columns:
        value = db.scalar(select(func.max(column)).where(column <= now))
        if value is not None:
            times.append(value)
        upcoming = db.scalar(select(func.min(column)).where(column > now))
        if upcoming is not None:
            future.append(upcoming)
    sv = settings.SettingsVersion
    for column in (sv.created_at, sv.effective_from):
        value = db.scalar(select(func.max(column)).where(sv.created_at <= now, sv.effective_from <= now))
        if value is not None:
            times.append(value)
    for column in (sv.created_at, sv.effective_from):
        upcoming = db.scalar(select(func.min(column)).where(column > now))
        if upcoming is not None:
            future.append(upcoming)
    for table in (Measurement, PlanningVersion, Event):
        upcoming = db.scalar(select(func.min(table.created_at)).where(table.created_at > now))
        if upcoming is not None:
            future.append(upcoming)
    return times, min(future, default=None)


def refresh_due_forecasts(now: datetime | None = None, *, force: bool = False) -> bool:
    """Ververs afgeleide prognoses dagelijks en wanneer een vastgelegd feit is gewijzigd.

    Geeft True terug als er een volledige controle is uitgevoerd.
    """
    from . import forecast

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    today = forecast.local_date(now)
    due = (
        force
        or STATE.dirty.is_set()
        or STATE.last_date != today
        or (STATE.next_due is not None and now >= STATE.next_due)
    )
    if not due:
        return False
    # Eerst wissen: wijzigingen tijdens deze ronde zetten de vlag opnieuw.
    STATE.dirty.clear()
    try:
        with SessionLocal() as db:
            # Imported observations, changed calibrations, model switches and target
            # edits affect existing crops too. Include scheduled settings when they
            # take effect, while ignoring future-recorded facts.
            global_times, next_due = _global_change_times(db, now)
            global_change = max(global_times, default=None)
            cultivations = db.scalars(
                select(Cultivation).where(
                    Cultivation.status.in_(["active", "planned"]),
                    Cultivation.id.in_(
                        select(CultivationRevision.cultivation_id).where(CultivationRevision.created_at <= now)
                    ),
                )
            ).all()
            ids = [c.id for c in cultivations]
            last_snapshot = (
                dict(
                    db.execute(
                        select(forecast.ForecastSnapshot.cultivation_id, func.max(forecast.ForecastSnapshot.created_at))
                        .where(forecast.ForecastSnapshot.cultivation_id.in_(ids))
                        .group_by(forecast.ForecastSnapshot.cultivation_id)
                    ).all()
                )
                if ids
                else {}
            )
            changed: dict[int, list] = {}
            for table in (Measurement, PlanningVersion, Event):
                if not ids:
                    break
                for cid, value in db.execute(
                    select(table.cultivation_id, func.max(table.created_at))
                    .where(table.cultivation_id.in_(ids))
                    .group_by(table.cultivation_id)
                ):
                    changed.setdefault(cid, []).append(value)
            for cultivation in cultivations:
                last = last_snapshot.get(cultivation.id)
                fresh_fact = max(
                    (v for v in [*changed.get(cultivation.id, []), global_change] if v is not None), default=None
                )
                if (
                    last is None
                    or (now - last).total_seconds() >= 86400
                    or forecast.local_date(last) < forecast.local_date(now)
                    or (fresh_fact and fresh_fact > last)
                ):
                    forecast.forecast(db, cultivation.id, now)
    except Exception:
        STATE.dirty.set()
        raise
    STATE.last_date = today
    STATE.next_due = next_due
    return True


# --- Dagelijkse opschoning --------------------------------------------------------------------
_last_cleanup: dict[str, date | None] = {"date": None}


def cleanup(db: Session, now: datetime | None = None) -> dict:
    """Verlopen sessies en aanmeldgegevens verwijderen; audit alleen inkorten als ingesteld."""
    from .settings import effective_settings

    now = now or utcnow()
    settings = effective_settings(db, now)
    # Aanmeldpogingen tellen alleen binnen het venster; ruim alles ouder dan twee vensters
    # (minimaal twee dagen) op, zodat een venstervergroting geen effect heeft op tellingen.
    attempts_cutoff = now - max(timedelta(days=2), timedelta(minutes=2 * settings.login_window_minutes))
    result = {
        "sessies": db.execute(delete(AuthSession).where(AuthSession.expires_at <= now)).rowcount or 0,
        "mfa_stappen": db.execute(delete(MfaChallenge).where(MfaChallenge.expires_at <= now)).rowcount or 0,
        "aanmeldpogingen": db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < attempts_cutoff)).rowcount
        or 0,
        "auditregels": 0,
    }
    if settings.audit_keep_days > 0:
        cutoff = now - timedelta(days=settings.audit_keep_days)
        result["auditregels"] = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff)).rowcount or 0
        if result["auditregels"]:
            audit(
                db,
                None,
                "audit_ingekort",
                "audit_logs",
                {
                    "verwijderd": result["auditregels"],
                    "ouder_dan": cutoff.isoformat(),
                    "bewaardagen": settings.audit_keep_days,
                },
            )
    db.commit()
    return result


def daily_cleanup(now: datetime | None = None) -> dict | None:
    today = datetime.now(timezone.utc).date() if now is None else now.date()
    if _last_cleanup["date"] == today:
        return None
    with SessionLocal() as db:
        result = cleanup(db, now)
    _last_cleanup["date"] = today
    if any(result.values()):
        log.info("Dagelijkse opschoning: %s", result)
    return result


# --- Back-ups ---------------------------------------------------------------------------------
def backup_dir():
    return DATA_DIR / "backups"


def prune_backups(keep_days: int, now_ts: float | None = None) -> list[str]:
    """Verwijder archieven ouder dan `keep_days` (0 = onbeperkt); de nieuwste blijft altijd."""
    if not keep_days:
        return []
    now_ts = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    cutoff = now_ts - keep_days * 86400
    archives = sorted(backup_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in archives[1:]:
        if old.stat().st_mtime < cutoff:
            old.unlink()
            removed.append(old.name)
    return removed


def backup_status(keep_days: int) -> dict:
    folder = backup_dir()
    archives = list(folder.glob("*.zip")) if folder.exists() else []
    usage = shutil.disk_usage(folder if folder.exists() else DATA_DIR)
    total_bytes = sum(p.stat().st_size for p in archives)
    advies = []
    if keep_days == 0:
        advies.append(
            f"Back-ups worden nu onbeperkt bewaard. Advies: kies een bewaartermijn van bijvoorbeeld "
            f"{ADVIES_BEWAARDAGEN} dagen, zodat de dataset niet volloopt."
        )
    if usage.total and usage.free / usage.total < 0.10:
        advies.append("Minder dan 10% schijfruimte vrij op de dataset. Ruim back-ups op of verkort de bewaartermijn.")
    return {
        "aantal": len(archives),
        "totaal_bytes": total_bytes,
        "nieuwste": max((p.stat().st_mtime for p in archives), default=None),
        "bewaardagen": keep_days,
        "advies_bewaardagen": ADVIES_BEWAARDAGEN,
        "schijf": {"totaal_bytes": usage.total, "gebruikt_bytes": usage.used, "vrij_bytes": usage.free},
        "advies": advies,
        "offsite_waarschuwing": (
            "Back-ups staan op dezelfde dataset als de app. Bij defecte schijven, diefstal of ransomware ben je ze "
            "dan ook kwijt. Download ze regelmatig of laat TrueNAS (Cloud Sync/Replication) een kopie op een andere "
            "locatie maken."
        ),
    }


@router.get("/api/backups/status")
def backups_status(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .settings import effective_settings

    return backup_status(effective_settings(db).backup_keep_days)
