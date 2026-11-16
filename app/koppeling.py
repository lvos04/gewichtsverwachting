"""Bedrijfskoppeling API v1: alleen-lezen KPI's en datasets voor het Bedrijfsdashboard.

Server-naar-server met Bearer-koppelsleutels. Alleen de SHA-256-hash en een herkenbaar
voorvoegsel worden opgeslagen. Gewichtsuitkomsten worden alleen gedeeld als zij uit een
operationeel vrijgegeven model komen; een doelgewicht wordt nooit als voorspelling gedeeld.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import __version__
from .auth import audit, require_admin
from .db import DATA_DIR, Base, get_db
from .models import (Bay, Cultivation, Harvest, Measurement, PlanningVersion, Stem, User,
                     Variety, utcnow)

APP_SLUG = 'gewichtsverwachting'
APP_NAAM = 'Gewichtsverwachting chrysant'
API_VERSIE = '1'
PREFIX = '/api/koppeling/v1'
SLEUTEL_PREFIX = 'bk_'
LIMIET_PER_MINUUT = 120
GEBRUIK_BIJWERKEN_NA = timedelta(minutes=1)
DATASETS = ('teelten', 'oogstplanning', 'metingen', 'prognoses', 'oogsten')
HORIZON_DAGEN = 14


class Koppelsleutel(Base):
    __tablename__ = 'koppelsleutels'
    id: Mapped[int] = mapped_column(primary_key=True)
    naam: Mapped[str] = mapped_column(String(100))
    voorvoegsel: Mapped[str] = mapped_column(String(8))
    sleutel_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    aangemaakt_op: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    aangemaakt_door: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    laatst_gebruikt_op: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingetrokken_op: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingetrokken_door: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)


class KoppelingFout(Exception):
    def __init__(self, status: int, fout: str, **extra):
        self.status, self.fout, self.extra = status, fout, extra


async def _fout_handler(request: Request, error: KoppelingFout):
    headers = {'WWW-Authenticate': 'Bearer'} if error.status == 401 else None
    if error.status == 429:
        headers = {'Retry-After': '60'}
    return JSONResponse(status_code=error.status, content={'fout': error.fout, **error.extra}, headers=headers)


def sleutel_hash(sleutel: str) -> str:
    return hashlib.sha256(sleutel.encode()).hexdigest()


def nieuwe_sleutel() -> str:
    return SLEUTEL_PREFIX + secrets.token_urlsafe(32)


def tijd(value: datetime | None) -> str | None:
    """Opgeslagen tijden zijn naïeve UTC; altijd met expliciete zone uitgeven."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def datum(value: date | None) -> str | None:
    return value.isoformat() if value else None


def nu() -> datetime:
    return utcnow()


# --- Rate limiting (één ASGI-proces, dus geheugen volstaat) ---------------------------------
_verzoeken: dict[int, deque] = {}
_slot = threading.Lock()


def _binnen_limiet(sleutel_id: int) -> bool:
    moment = time.monotonic()
    with _slot:
        venster = _verzoeken.setdefault(sleutel_id, deque())
        while venster and moment - venster[0] >= 60:
            venster.popleft()
        if len(venster) >= LIMIET_PER_MINUUT:
            return False
        venster.append(moment)
        return True


def vereis_koppelsleutel(request: Request, db: Session = Depends(get_db)) -> Koppelsleutel:
    header = request.headers.get('authorization', '')
    schema, _, sleutel = header.partition(' ')
    sleutel = sleutel.strip()
    if schema.lower() != 'bearer' or not sleutel.startswith(SLEUTEL_PREFIX) or len(sleutel) > 200:
        raise KoppelingFout(401, 'ongeldige_koppelsleutel')
    digest = sleutel_hash(sleutel)
    rij = db.scalar(select(Koppelsleutel).where(Koppelsleutel.sleutel_hash == digest))
    if rij is None or not hmac.compare_digest(rij.sleutel_hash, digest) or rij.ingetrokken_op is not None:
        raise KoppelingFout(401, 'ongeldige_koppelsleutel')
    if not _binnen_limiet(rij.id):
        raise KoppelingFout(429, 'te_veel_verzoeken')
    moment = nu()
    if rij.laatst_gebruikt_op is None or moment - rij.laatst_gebruikt_op >= GEBRUIK_BIJWERKEN_NA:
        rij.laatst_gebruikt_op = moment
        db.commit()
    return rij


router = APIRouter(prefix=PREFIX, dependencies=[Depends(vereis_koppelsleutel)])
beheer = APIRouter()


def install(app: FastAPI) -> None:
    app.add_exception_handler(KoppelingFout, _fout_handler)
    app.include_router(router)
    app.include_router(beheer)


# --- Gedeelde gegevens -----------------------------------------------------------------------
def _vandaag() -> date:
    from .core import NL
    return datetime.now(NL).date()


def _laatste_planning(db: Session) -> dict[int, PlanningVersion]:
    latest: dict[int, PlanningVersion] = {}
    for plan in db.scalars(select(PlanningVersion).order_by(PlanningVersion.created_at.desc(), PlanningVersion.id.desc())):
        latest.setdefault(plan.cultivation_id, plan)
    return latest


def _soorten(db: Session) -> dict[int, str]:
    return {v.id: v.name for v in db.scalars(select(Variety))}


def _afdelingen(db: Session) -> dict[int, int]:
    return {b.number: b.department for b in db.scalars(select(Bay))}


def _model_status(db: Session) -> dict[str, bool]:
    from .forecast import ModelArtifact, active_model_at
    moment = nu()
    status = {}
    for kind in ('KD10', 'KD30'):
        model_id = active_model_at(db, kind, moment)
        model = db.get(ModelArtifact, model_id) if model_id else None
        status[kind] = bool(model and model.operational_eligible)
    return status


PROGNOSE_STATUS = {
    'operational': ('gevalideerd', 'Operationeel vrijgegeven model'),
    'experimental': ('onbekend', 'Model is experimenteel en niet vrijgegeven; geen gewichtsuitkomst gedeeld'),
    'baseline': ('onbekend', 'Alleen historisch gemiddelde beschikbaar; geen gevalideerde prognose'),
    'unknown': ('onbekend', 'Geen gevalideerd model of onvoldoende gegevens; gewicht onbekend'),
}


def _laatste_prognoses(db: Session) -> dict[int, object]:
    from .forecast import ForecastSnapshot
    # Prognoses groeien dagelijks per teelt; alleen de nieuwste rij per teelt laden.
    # Snapshots worden append-only met oplopend id geschreven.
    nieuwste = select(func.max(ForecastSnapshot.id)).group_by(ForecastSnapshot.cultivation_id)
    return {snap.cultivation_id: snap for snap in db.scalars(select(ForecastSnapshot).where(ForecastSnapshot.id.in_(nieuwste)))}


# --- Endpoints -------------------------------------------------------------------------------
@router.get('/info')
def info():
    return {'app': APP_SLUG, 'naam': APP_NAAM, 'versie': __version__, 'api_versie': API_VERSIE,
            'bedrijf': None, 'datasets': ['samenvatting', *DATASETS], 'tijd': tijd(nu())}


def _kpi(sleutel, label, waarde, eenheid, status, toelichting=None):
    return {'sleutel': sleutel, 'label': label, 'waarde': waarde, 'eenheid': eenheid,
            'status': status, 'toelichting': toelichting}


def samenvatting_data(db: Session) -> dict:
    from .core import dashboard_data
    from .settings import effective_settings
    settings = effective_settings(db)
    today = _vandaag()
    horizon = today + timedelta(days=HORIZON_DAGEN)
    cultivations = db.scalars(select(Cultivation)).all()
    lopend = [c for c in cultivations if c.status == 'active']
    gepland = [c for c in cultivations if c.status == 'planned']
    open_teelten = lopend + gepland
    plans = _laatste_planning(db)
    kpis, meldingen = [], []

    bezet = len({c.bay_number for c in lopend})
    kpis.append(_kpi('actieve_teelten', 'Lopende teelten', len(lopend), 'teelten', 'ok',
                     f'{bezet} van 40 kappen bezet · {len(gepland)} gepland'))

    komend, verlopen = [], []
    for c in open_teelten:
        plan = plans.get(c.id)
        if not plan or not plan.expected_harvest_date:
            continue
        if today <= plan.expected_harvest_date <= horizon:
            komend.append((plan.expected_harvest_date, c))
        elif plan.expected_harvest_date < today:
            verlopen.append(c)
    komend.sort(key=lambda item: (item[0], item[1].bay_number))
    kpis.append(_kpi('oogsten_komende_14_dagen', 'Geplande oogsten komende 14 dagen', len(komend), 'teelten',
                     'let_op' if verlopen else 'ok',
                     (f'Eerste: kap {komend[0][1].bay_number} op {komend[0][0].isoformat()}' if komend else 'Geen oogst gepland')
                     + (f' · {len(verlopen)} voorbij verwachte oogstdatum' if verlopen else '')))
    if verlopen:
        meldingen.append({'niveau': 'waarschuwing', 'tekst': f'{len(verlopen)} teelt(en) voorbij de verwachte oogstdatum zonder geregistreerde oogst', 'sinds': None})

    taken = dashboard_data(db)['tasks']
    achterstallig = [t for t in taken if t['status'] == 'overdue']
    vandaag_te_meten = [t for t in taken if t['status'] == 'due']
    status = 'ok' if not achterstallig else 'let_op' if len(achterstallig) < 3 else 'actie'
    kpis.append(_kpi('metingen_achterstallig', 'Metingen achterstallig', len(achterstallig), 'metingen', status,
                     f'{len(vandaag_te_meten)} vandaag te meten (KD10/KD30/pilot)'))
    if achterstallig:
        oudste = min(t['due_date'] for t in achterstallig)
        meldingen.append({'niveau': 'waarschuwing' if len(achterstallig) < 3 else 'kritiek',
                          'tekst': f'{len(achterstallig)} KD-meting(en) achterstallig',
                          'sinds': tijd(datetime.fromisoformat(oudste))})

    laatste = db.scalar(select(Measurement).order_by(Measurement.measured_at.desc(), Measurement.id.desc()))
    gemiddeld = None
    if laatste:
        gemiddeld = db.scalar(select(func.avg(Stem.weight_g)).where(Stem.measurement_id == laatste.id, Stem.excluded.is_(False)))
    if laatste and gemiddeld is not None:
        teelt = db.get(Cultivation, laatste.cultivation_id)
        kpis.append(_kpi('laatst_gemeten_gewicht', 'Laatst gemeten gemiddeld takgewicht', round(float(gemiddeld), 1), 'g', 'ok',
                         f'{laatste.kind} · kap {teelt.bay_number if teelt else "?"} · gemeten {datum(laatste.measured_at.date())}; meting, geen prognose'))
    else:
        kpis.append(_kpi('laatst_gemeten_gewicht', 'Laatst gemeten gemiddeld takgewicht', None, 'g', 'onbekend',
                         'Nog geen meting met bruikbare takken'))

    modellen = _model_status(db)
    vrijgegeven = [kind for kind, ok in modellen.items() if ok]
    kpis.append(_kpi('gevalideerde_modellen', 'Vrijgegeven prognosemodellen', len(vrijgegeven), 'van 2 (KD10/KD30)',
                     'ok' if len(vrijgegeven) == 2 else 'let_op',
                     ('Vrijgegeven: ' + ', '.join(vrijgegeven)) if vrijgegeven else
                     'Nog geen gevalideerd model; gewichtsprognoses blijven onbekend'))
    if not vrijgegeven:
        meldingen.append({'niveau': 'info', 'tekst': 'Geen vrijgegeven gewichtsmodel; prognoses worden als onbekend gedeeld', 'sinds': None})

    minimum = max(settings.min_training_cultivations, settings.environment_min_training_cultivations
                  if settings.model_feature_profile != 'basic' else settings.min_training_cultivations)
    dekking = _volledige_teelten(db)
    kpis.append(_kpi('gegevensdekking', 'Teelten met KD10, KD30 en oogstgewicht', dekking, 'teelten',
                     'ok' if dekking >= minimum else 'let_op',
                     f'Minimaal {minimum} complete standaardteelten nodig voor modeltraining; definitieve toetsing gebeurt bij training'))

    prognoses = _laatste_prognoses(db)
    gevalideerd = [prognoses[c.id].point_g for _, c in komend
                   if c.id in prognoses and prognoses[c.id].status == 'operational' and prognoses[c.id].point_g is not None]
    if gevalideerd:
        kpis.append(_kpi('prognose_komende_oogsten', 'Verwacht gemiddeld takgewicht komende oogsten',
                         round(sum(gevalideerd) / len(gevalideerd), 1), 'g', 'ok',
                         f'Gevalideerde prognose voor {len(gevalideerd)} van {len(komend)} komende oogsten'))
    else:
        kpis.append(_kpi('prognose_komende_oogsten', 'Verwacht gemiddeld takgewicht komende oogsten', None, 'g', 'onbekend',
                         'Geen gevalideerde prognose beschikbaar; het doelgewicht wordt niet als voorspelling getoond'))

    from .importer import ImportJob
    week = nu() - timedelta(days=7)
    mislukt = db.scalars(select(ImportJob).where(ImportJob.status == 'failed', ImportJob.revoked_at.is_(None),
                                                 ImportJob.created_at >= week).order_by(ImportJob.created_at)).all()
    if mislukt:
        meldingen.append({'niveau': 'waarschuwing', 'tekst': f'{len(mislukt)} Priva-import(en) mislukt in de afgelopen week',
                          'sinds': tijd(mislukt[0].created_at)})
    archieven = list((DATA_DIR / 'backups').glob('*.zip'))
    if archieven:
        recent = max(p.stat().st_mtime for p in archieven)
        if time.time() - recent > 2 * settings.backup_interval_hours * 3600:
            meldingen.append({'niveau': 'kritiek', 'tekst': 'Laatste lokale back-up is ouder dan twee back-upintervallen',
                              'sinds': tijd(datetime.fromtimestamp(recent, timezone.utc))})
    elif cultivations:
        meldingen.append({'niveau': 'waarschuwing', 'tekst': 'Nog geen lokale back-up gevonden', 'sinds': None})
    return {'app': APP_SLUG, 'gegenereerd_op': tijd(nu()), 'kpis': kpis, 'meldingen': meldingen}


def _volledige_teelten(db: Session) -> int:
    """Indicatie: standaardteelten met bruikbare labelkwaliteit, beide KD-metingen en één oogstgewicht."""
    from .forecast import ELIGIBLE_QUALITY
    met_gewicht = {h.cultivation_id for h in db.scalars(select(Harvest).where(
        Harvest.weight_g.is_not(None), Harvest.weight_g > 0, Harvest.end_date.is_not(None)))
        if not (h.quality_classes and len(h.quality_classes) > 1)}
    gemeten: dict[int, set] = {}
    for cid, kind in db.execute(select(Measurement.cultivation_id, Measurement.kind).where(Measurement.kind.in_(['KD10', 'KD30']))):
        gemeten.setdefault(cid, set()).add(kind)
    return sum(1 for c in db.scalars(select(Cultivation).where(Cultivation.category == 'standard',
               Cultivation.label_quality.in_(ELIGIBLE_QUALITY)))
               if c.id in met_gewicht and gemeten.get(c.id) == {'KD10', 'KD30'})


@router.get('/samenvatting')
def samenvatting(db: Session = Depends(get_db)):
    return samenvatting_data(db)


# --- Datasets --------------------------------------------------------------------------------
def _parse_sinds(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        # Een niet-geëscapete '+' in de query komt als spatie binnen.
        parsed = datetime.fromisoformat(re.sub(r'(T[\d:.]+) (\d{2}:?\d{2})$', r'\1+\2', value.strip()))
    except ValueError:
        raise KoppelingFout(422, 'ongeldige_parameter', veld='sinds')
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        text = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode()
        kind, _, number = text.partition(':')
        if kind != 'id' or not number.isdigit():
            raise ValueError
        return int(number)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise KoppelingFout(422, 'ongeldige_parameter', veld='cursor')


def _cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(f'id:{last_id}'.encode()).decode().rstrip('=')


def _pagina(items: list[dict], sinds: datetime | None, na: int, limiet: int) -> tuple[list[dict], str | None]:
    """Stabiel gesorteerd op id; `sinds` filtert op bijgewerkt_op (naïeve UTC)."""
    selectie = [i for i in items if i['_id'] > na and (sinds is None or (i['_bijgewerkt'] is not None and i['_bijgewerkt'] >= sinds))]
    selectie.sort(key=lambda i: i['_id'])
    pagina = selectie[:limiet]
    volgende = _cursor(pagina[-1]['_id']) if len(selectie) > limiet else None
    return [{k: v for k, v in i.items() if not k.startswith('_')} for i in pagina], volgende


def _teelt_basis(c: Cultivation, soorten: dict, afdelingen: dict) -> dict:
    return {'teelt_id': c.id, 'kap': c.bay_number, 'afdeling': afdelingen.get(c.bay_number),
            'soort': soorten.get(c.variety_id), 'ronde': c.round_name}


def _ds_teelten(db: Session, sinds, na, limiet, oogstplanning=False):
    from .models import CultivationRevision
    soorten, afdelingen, plans = _soorten(db), _afdelingen(db), _laatste_planning(db)
    revisies = dict(db.execute(select(CultivationRevision.cultivation_id, func.max(CultivationRevision.created_at))
                               .group_by(CultivationRevision.cultivation_id)).all())
    today = _vandaag()
    items = []
    for c in db.scalars(select(Cultivation)):
        plan = plans.get(c.id)
        bijgewerkt = max((t for t in (revisies.get(c.id), plan.created_at if plan else None) if t is not None), default=None)
        if oogstplanning:
            if c.status not in ('planned', 'active') or not plan or not plan.expected_harvest_date:
                continue
            items.append({'_id': c.id, '_bijgewerkt': bijgewerkt, 'id': c.id, **_teelt_basis(c, soorten, afdelingen),
                          'status': c.status, 'verwachte_oogstdatum': datum(plan.expected_harvest_date),
                          'dagen_tot_oogst': (plan.expected_harvest_date - today).days,
                          'geplande_takken': plan.planned_stems, 'bedden': c.beds,
                          'bijgewerkt_op': tijd(bijgewerkt)})
            continue
        kd = (today - plan.light_out_date).days if plan and plan.light_out_date and c.status in ('planned', 'active') else None
        items.append({'_id': c.id, '_bijgewerkt': bijgewerkt, 'id': c.id, 'kap': c.bay_number,
                      'afdeling': afdelingen.get(c.bay_number), 'soort': soorten.get(c.variety_id), 'ronde': c.round_name,
                      'bedden': c.beds, 'categorie': c.category, 'status': c.status, 'labelkwaliteit': c.label_quality,
                      'plantdatum': datum(plan.plant_date) if plan else None,
                      'licht_uit_datum': datum(plan.light_out_date) if plan else None,
                      'verwachte_oogstdatum': datum(plan.expected_harvest_date) if plan else None,
                      'dichtheid': plan.density if plan else None, 'geplande_takken': plan.planned_stems if plan else None,
                      'kd_dag': kd, 'bijgewerkt_op': tijd(bijgewerkt)})
    return _pagina(items, sinds, na, limiet)


def _ds_metingen(db: Session, sinds, na, limiet):
    query = select(Measurement).where(Measurement.id > na)
    if sinds is not None:
        query = query.where(Measurement.created_at >= sinds)
    rows = db.scalars(query.order_by(Measurement.id).limit(limiet + 1)).all()
    pagina = rows[:limiet]
    ids = [m.id for m in pagina]
    stats = {mid: (n, w, l) for mid, n, w, l in db.execute(
        select(Stem.measurement_id, func.count(), func.avg(Stem.weight_g), func.avg(Stem.length_cm))
        .where(Stem.measurement_id.in_(ids), Stem.excluded.is_(False)).group_by(Stem.measurement_id))} if ids else {}
    teelten = {c.id: c for c in db.scalars(select(Cultivation).where(Cultivation.id.in_({m.cultivation_id for m in pagina})))} if pagina else {}
    soorten = _soorten(db)
    items = []
    for m in pagina:
        c = teelten.get(m.cultivation_id)
        n, w, l = stats.get(m.id, (0, None, None))
        items.append({'id': m.id, 'teelt_id': m.cultivation_id, 'kap': c.bay_number if c else None,
                      'soort': soorten.get(c.variety_id) if c else None, 'ronde': c.round_name if c else None,
                      'type': m.kind, 'gemeten_op': tijd(m.measured_at), 'kd_dag': m.actual_kd_day,
                      'aantal_takken': n, 'gemiddeld_gewicht_g': round(float(w), 2) if w is not None else None,
                      'gemiddelde_lengte_cm': round(float(l), 2) if l is not None else None,
                      'bijgewerkt_op': tijd(m.created_at)})
    return items, (_cursor(pagina[-1].id) if len(rows) > limiet else None)


def _ds_oogsten(db: Session, sinds, na, limiet):
    query = select(Harvest).where(Harvest.id > na)
    if sinds is not None:
        query = query.where(Harvest.created_at >= sinds)
    rows = db.scalars(query.order_by(Harvest.id).limit(limiet + 1)).all()
    pagina = rows[:limiet]
    teelten = {c.id: c for c in db.scalars(select(Cultivation).where(Cultivation.id.in_({h.cultivation_id for h in pagina})))} if pagina else {}
    soorten = _soorten(db)
    items = []
    for h in pagina:
        c = teelten.get(h.cultivation_id)
        klassen = len(h.quality_classes) if isinstance(h.quality_classes, list) else 0
        items.append({'id': h.id, 'teelt_id': h.cultivation_id, 'kap': c.bay_number if c else None,
                      'soort': soorten.get(c.variety_id) if c else None, 'ronde': c.round_name if c else None,
                      'begindatum': datum(h.start_date), 'einddatum': datum(h.end_date),
                      'gemiddeld_takgewicht_g': h.weight_g, 'aantal_takken': h.stem_count,
                      'uitvalfractie': h.loss_fraction, 'aantal_kwaliteitsklassen': klassen,
                      'bijgewerkt_op': tijd(h.created_at)})
    return items, (_cursor(pagina[-1].id) if len(rows) > limiet else None)


def _ds_prognoses(db: Session, sinds, na, limiet):
    soorten, plans = _soorten(db), _laatste_planning(db)
    teelten = {c.id: c for c in db.scalars(select(Cultivation).where(Cultivation.status.in_(['planned', 'active'])))}
    items = []
    for cid, snap in _laatste_prognoses(db).items():
        c = teelten.get(cid)
        if c is None:
            continue
        gevalideerd = snap.status == 'operational' and snap.point_g is not None
        status, toelichting = PROGNOSE_STATUS['operational' if gevalideerd else snap.status
                                              if snap.status in PROGNOSE_STATUS and snap.status != 'operational' else 'unknown']
        plan = plans.get(cid)
        items.append({'_id': snap.id, '_bijgewerkt': snap.created_at, 'id': snap.id, 'teelt_id': cid,
                      'kap': c.bay_number, 'soort': soorten.get(c.variety_id), 'ronde': c.round_name,
                      'verwachte_oogstdatum': datum(plan.expected_harvest_date) if plan else None,
                      'peilmoment': tijd(snap.as_of),
                      'modeltype': (snap.response or {}).get('model_kind') if gevalideerd else None,
                      'status': status,
                      'gewicht_g': round(snap.point_g, 1) if gevalideerd else None,
                      'ondergrens_g': round(snap.low_g, 1) if gevalideerd and snap.low_g is not None else None,
                      'bovengrens_g': round(snap.high_g, 1) if gevalideerd and snap.high_g is not None else None,
                      'toelichting': toelichting, 'bijgewerkt_op': tijd(snap.created_at)})
    return _pagina(items, sinds, na, limiet)


@router.get('/datasets/{naam}')
def dataset(naam: str, sinds: str | None = None, limiet: int = Query(100, ge=1, le=500),
            cursor: str | None = None, db: Session = Depends(get_db)):
    if naam not in DATASETS:
        raise KoppelingFout(404, 'onbekende_dataset')
    since, na = _parse_sinds(sinds), _parse_cursor(cursor)
    if naam == 'teelten':
        items, volgende = _ds_teelten(db, since, na, limiet)
    elif naam == 'oogstplanning':
        items, volgende = _ds_teelten(db, since, na, limiet, oogstplanning=True)
    elif naam == 'metingen':
        items, volgende = _ds_metingen(db, since, na, limiet)
    elif naam == 'oogsten':
        items, volgende = _ds_oogsten(db, since, na, limiet)
    else:
        items, volgende = _ds_prognoses(db, since, na, limiet)
    return {'app': APP_SLUG, 'dataset': naam, 'gegenereerd_op': tijd(nu()), 'items': items, 'volgende_cursor': volgende}


# --- Beheer (sessie-auth, oorsprongcontrole via require_admin) -------------------------------
class SleutelIn(BaseModel):
    naam: str = Field(min_length=1, max_length=100)


def sleutel_dict(db: Session, rij: Koppelsleutel) -> dict:
    door = db.get(User, rij.aangemaakt_door) if rij.aangemaakt_door else None
    return {'id': rij.id, 'naam': rij.naam, 'voorvoegsel': rij.voorvoegsel,
            'aangemaakt_op': tijd(rij.aangemaakt_op), 'aangemaakt_door': door.username if door else None,
            'laatst_gebruikt_op': tijd(rij.laatst_gebruikt_op), 'ingetrokken_op': tijd(rij.ingetrokken_op),
            'actief': rij.ingetrokken_op is None}


def maak_sleutel(db: Session, naam: str, user: User | None) -> tuple[Koppelsleutel, str]:
    naam = naam.strip()
    if not naam:
        raise HTTPException(422, 'Naam is verplicht')
    sleutel = nieuwe_sleutel()
    rij = Koppelsleutel(naam=naam, voorvoegsel=sleutel[:8], sleutel_hash=sleutel_hash(sleutel),
                        aangemaakt_op=nu(), aangemaakt_door=user.id if user else None)
    db.add(rij)
    db.flush()
    audit(db, user, 'koppelsleutel.aanmaken', f'koppelsleutel:{rij.id}',
          {'naam': rij.naam, 'voorvoegsel': rij.voorvoegsel, **({} if user else {'via': 'cli'})})
    db.commit()
    return rij, sleutel


def trek_in(db: Session, sleutel_id: int, user: User | None) -> Koppelsleutel:
    rij = db.get(Koppelsleutel, sleutel_id)
    if rij is None:
        raise HTTPException(404, 'Koppelsleutel niet gevonden')
    if rij.ingetrokken_op is None:
        rij.ingetrokken_op = nu()
        rij.ingetrokken_door = user.id if user else None
        audit(db, user, 'koppelsleutel.intrekken', f'koppelsleutel:{rij.id}',
              {'naam': rij.naam, 'voorvoegsel': rij.voorvoegsel, **({} if user else {'via': 'cli'})})
        db.commit()
    return rij


@beheer.get('/api/koppelsleutels')
def lijst(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return [sleutel_dict(db, r) for r in db.scalars(select(Koppelsleutel).order_by(Koppelsleutel.id.desc()))]


@beheer.post('/api/koppelsleutels')
def aanmaken(data: SleutelIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rij, sleutel = maak_sleutel(db, data.naam, user)
    return {**sleutel_dict(db, rij), 'sleutel': sleutel,
            'melding': 'Kopieer deze sleutel nu; hij wordt niet opnieuw getoond.'}


@beheer.delete('/api/koppelsleutels/{sleutel_id}')
def intrekken(sleutel_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return sleutel_dict(db, trek_in(db, sleutel_id, user))
