"""Durable, bounded Priva imports. Originals and source versions are append only.

Call recover_jobs() at startup and shutdown_worker() before process shutdown. One
application process owns the worker; deployment must use one ASGI worker.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import threading
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, DATA_DIR
from .auth import require_user, require_write, require_admin, audit
from pydantic import BaseModel, Field, model_validator
from typing import Literal

router = APIRouter(tags=["Priva-import"])
TZ = ZoneInfo("Europe/Amsterdam")
HEADERS = ('label','pcu','type_1','idx_1','type_2','idx_2','startdate','enddate','value')
MAX_UPLOAD = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILES = 2000
BATCH = 500
PRIORITY = {"algemeen_bedrijf": 50, "klimaat": 40, "belichting": 40, "kranen": 40, "algemeen_afdeling": 20}
ROOT = DATA_DIR / "imports"
_executor = None
_lock = threading.Lock()
_scheduled = set()
_stop = threading.Event()


class ImportUploadLimitMiddleware:
    """Bound multipart transfer before Starlette can spool an oversized body.

    Register with app.add_middleware(ImportUploadLimitMiddleware). The extra
    allowance covers multipart headers; individual files retain stricter caps.
    """
    def __init__(self, app, max_bytes=MAX_TOTAL+16*1024*1024):
        self.app=app
        self.max_bytes=max_bytes

    async def __call__(self,scope,receive,send):
        if scope.get('type')!='http' or scope.get('method')!='POST' or scope.get('path','').rstrip('/') not in ('/api/imports','/api/scans','/api/history-imports'):
            return await self.app(scope,receive,send)
        from starlette.responses import JSONResponse
        path = scope.get('path','').rstrip('/')
        max_bytes = self.max_bytes if path == '/api/imports' else (33*1024*1024 if path == '/api/history-imports' else 16*1024*1024)
        headers=dict(scope.get('headers',[]))
        try:
            length=int(headers.get(b'content-length',b'0'))
        except ValueError:
            return await JSONResponse({'detail':'Ongeldige Content-Length'},status_code=400)(scope,receive,send)
        if length>max_bytes:
            return await JSONResponse({'detail':'Upload te groot'},status_code=413)(scope,receive,send)
        transferred=0
        async def bounded_receive():
            nonlocal transferred
            message=await receive()
            transferred+=len(message.get('body',b''))
            if transferred>max_bytes:
                raise HTTPException(413,'Upload te groot')
            return message
        return await self.app(scope,bounded_receive,send)


def priority_policy(db) -> dict:
    """Resolve one auditable settings snapshot for this preview or read."""
    from .settings import effective_settings, SettingsVersion
    as_of=utcnow()
    configured=effective_settings(db,as_of=as_of)
    version=db.scalar(select(SettingsVersion).where(SettingsVersion.effective_from<=as_of,SettingsVersion.created_at<=as_of).order_by(SettingsVersion.effective_from.desc(),SettingsVersion.id.desc()))
    priorities={profile:getattr(configured,f'priority_{profile}',default) for profile,default in PRIORITY.items()}
    names={'algemeen_bedrijf':'Algemeen Bedrijf','klimaat':'Klimaat','belichting':'Belichting','kranen':'Kranen','algemeen_afdeling':'Algemeen Afdeling'}
    ranks=', '.join(f'{names[profile]} ({rank})' for profile,rank in sorted(priorities.items(),key=lambda item:(-item[1],item[0])))
    policy=f'Eerst bruikbare numerieke waarden; daarna ingestelde bronprioriteit (hoog naar laag): {ranks}. Bij gelijke prioriteit geldt de laatst bevestigde bronversie; elke botsing blijft zichtbaar.'
    return dict(source_priority=policy,priorities=priorities,priority_settings_version_id=version.id if version else None,priority_settings_as_of=as_of.isoformat()+'Z')


def stored_path(path: Path) -> str:
    """Database stores dataset-relative paths, so backup restoration is portable."""
    return path.resolve().relative_to(DATA_DIR.resolve()).as_posix()


def original_path(value: str) -> Path:
    path=Path(value)
    if path.is_absolute():
        # Read compatibility for pre-relative-path development snapshots.
        if path.exists():
            return path
        if 'imports' in path.parts:
            return DATA_DIR.joinpath(*path.parts[path.parts.index('imports'):])
        return path
    if '..' in path.parts:
        raise ValueError('Ongeldig opgeslagen bronpad')
    return DATA_DIR / path


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Calibration(Base):
    __tablename__ = "priva_calibrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30),index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    object_type: Mapped[str | None] = mapped_column(String(80))
    object_number: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(160))
    channel: Mapped[str | None] = mapped_column(String(80))
    value_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(Boolean)
    user_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime,default=utcnow)


class CalibrationInput(BaseModel):
    kind: Literal['unit','lamp','transmission']
    valid_from: date
    valid_until: date | None = None
    object_type: str | None = Field(default=None,max_length=80)
    object_number: int | None = None
    label: str | None = Field(default=None,max_length=160)
    channel: str | None = Field(default=None,max_length=80)
    value: dict
    reason: str = Field(min_length=3,max_length=2000)
    confirmed: Literal[True]

    @model_validator(mode='after')
    def validate_calibration(self):
        if self.valid_until and self.valid_until<self.valid_from:
            raise ValueError('Einddatum vóór begindatum')
        v=self.value
        if self.kind=='unit':
            if not self.label or not isinstance(v.get('unit'),str) or not 1<=len(v['unit'])<=40:
                raise ValueError('Eenheid vereist label en value.unit')
        if self.kind=='lamp':
            if self.object_type!='light' or not self.object_number or self.channel is None:
                raise ValueError('Lamp vereist exact lichtmodule-nummer en kanaal')
            if not isinstance(v.get('hours_label'),str) or v.get('time_unit') not in ('s','h'):
                raise ValueError('Lamp vereist hours_label en time_unit s of h')
            for field,low,high in [('ppfd',0,3000),('dim_factor',0,1)]:
                number=v.get(field)
                if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not low<=number<=high:
                    raise ValueError(f'Ongeldige {field}')
            if v.get('independent_contribution') is not True:
                raise ValueError('Bevestig independent_contribution:true om dubbel tellen te voorkomen')
        if self.kind=='transmission':
            factor=v.get('factor')
            if self.object_type!='bay' or self.object_number is None or not 1<=self.object_number<=40:
                raise ValueError('Transmissie vereist object_type bay en kapnummer 1–40')
            if isinstance(factor,bool) or not isinstance(factor,(int,float)) or not math.isfinite(factor) or not 0<=factor<=1:
                raise ValueError('Transmissiefactor moet tussen 0 en 1 liggen')
        # JSON must never contain NaN/Infinity or unbounded arbitrary data.
        if len(json.dumps(v,allow_nan=False))>8192:
            raise ValueError('Kalibratie te groot')
        return self


def calibration_dict(c):
    return dict(id=c.id,kind=c.kind,valid_from=c.valid_from,valid_until=c.valid_until,object_type=c.object_type,
                object_number=c.object_number,label=c.label,channel=c.channel,value=json.loads(c.value_json),
                reason=c.reason,confirmed=c.confirmed,user_id=c.user_id,created_at=c.created_at)


@router.get('/api/calibrations')
def list_calibrations(user=Depends(require_user)):
    with SessionLocal() as db:
        return [calibration_dict(c) for c in db.scalars(select(Calibration).order_by(Calibration.id.desc()).limit(2000))]


@router.post('/api/calibrations')
def create_calibration(payload:CalibrationInput,user=Depends(require_admin)):
    values=payload.model_dump(exclude={'value'})
    with SessionLocal() as db:
        item=Calibration(**values,value_json=json.dumps(payload.value,allow_nan=False),user_id=user.id)
        db.add(item); db.flush()
        audit(db,user,'calibration_create',str(item.id),payload.model_dump(mode='json')); db.commit()
        return calibration_dict(item)


def matching_calibration(calibrations,kind,day,*,object_type,object_number,label='',channel=''):
    found=[]
    for c in calibrations:
        if c.kind!=kind or not c.confirmed or c.valid_from>day or (c.valid_until is not None and c.valid_until<day):
            continue
        if any(expected is not None and expected!=actual for expected,actual in ((c.object_type,object_type),(c.object_number,object_number),(c.label,label),(c.channel,channel))):
            continue
        specificity=sum(x is not None for x in (c.object_type,c.object_number,c.label,c.channel))
        found.append((specificity,c.valid_from,c.id,c))
    return max(found,key=lambda x:x[:3])[3] if found else None


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    phase: Mapped[str] = mapped_column(String(20), default="preview")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    progress_rows: Mapped[int] = mapped_column(Integer, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    preview_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)


class ImportFile(Base):
    __tablename__ = "import_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    archive: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[int | None] = mapped_column(Integer)
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_json: Mapped[str] = mapped_column(Text, default="{}")


class PrivaSource(Base):
    __tablename__ = "priva_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    profile: Mapped[str] = mapped_column(String(40))
    logical_key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(Integer)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrivaValue(Base):
    __tablename__ = "priva_values"
    __table_args__ = (UniqueConstraint("source_id", "row_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("priva_sources.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(160), index=True)
    pcu: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(80), index=True)
    object_number: Mapped[int | None] = mapped_column(Integer)
    subtype: Mapped[str] = mapped_column(String(80))
    channel: Mapped[str] = mapped_column(String(80))
    local_date: Mapped[str | None] = mapped_column(String(10), index=True)
    end_date: Mapped[str | None] = mapped_column(String(10))
    timestamp_utc: Mapped[datetime | None] = mapped_column(DateTime)
    value: Mapped[float | None] = mapped_column(Float)
    original_value: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(40))
    quality_json: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text)


def parse_priva_date(value: str) -> datetime:
    """Explicit 2000..2069 policy for two-digit years; older years need 4 digits."""
    match = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*", value)
    if not match:
        raise ValueError("ongeldige datum; verwacht dd-mm-jj of dd-mm-jjjj")
    d, m, y, h, minute, sec = match.groups()
    year = int(y)
    if len(y) == 2:
        if year > 69:
            raise ValueError("eeuw onzeker; lever een viercijferig jaar")
        year += 2000
    if not 2000 <= year <= 2099:
        raise ValueError("jaar buiten expliciete importgrenzen 2000–2099")
    naive = datetime(year, int(m), int(d), int(h or 0), int(minute or 0), int(sec or 0))
    local = naive.replace(tzinfo=TZ)
    # Nonexistent/ambiguous clock times cannot silently choose a DST fold.
    if local.utcoffset() != naive.replace(tzinfo=TZ, fold=1).utcoffset():
        raise ValueError("zomer-/wintertijd ambigu of ongeldig; gebruik daggegevens")
    return local


def normalize_row(row: dict, number: int) -> dict:
    flags = []
    local = end = None
    try:
        local = parse_priva_date(row['startdate'])
        end = parse_priva_date(row['enddate'])
        if end < local:
            raise ValueError("einddatum vóór begindatum")
    except (ValueError, KeyError):
        flags.append('invalid_date')
        local = end = None
    raw = row.get('value', '').strip()
    val = None
    if not raw:
        flags.append('missing_value')
    else:
        try:
            if not re.fullmatch(r'[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?', raw):
                raise ValueError()
            val = float(raw.replace(',', '.'))
            if not math.isfinite(val):
                raise ValueError()
        except ValueError:
            flags.append('invalid_value')
            val = None
    types = {'Afdeling':'department', 'Kraan':'valve', 'modgroeilichtsturing':'light', 'Groeilicht sturing':'light_string', 'Straling':'radiation'}
    object_type = types.get(row.get('type_1',''), row.get('type_1','') or 'weather')
    idx = row.get('idx_1','')
    object_number = int(idx) if idx.isdigit() else None
    label = row.get('label', '')
    if val == 0 and 'CompCO2' in label and object_type == 'department' and object_number in (1, 3):
        flags.append('untrusted_co2_zero')
        val = None
    if val == 0 and ('MeasuredPAR' in label or 'ModGrowlightPAR' in label):
        flags.append('invalid_par_zero')
        val = None
    if object_type == 'valve' and (object_number is None or object_number > 40 or object_number < 1):
        flags.append('unmapped_valve')
    if object_type == 'light' and row.get('idx_2') == '2':
        flags.append('unverified_light_channel')
    if 'Curtain' in label:
        flags.append('unverified_curtain_identity')
    # Priva's example schema supplies no units. Do not infer them from names.
    flags.append('unknown_unit')
    if 'CompRV' in label and val is not None and not 0 <= val <= 100:
        flags.append('implausible_value')
        val = None
    if ('CompTemp' in label or 'OutsideTemp' in label) and val is not None and not -40 <= val <= 65:
        flags.append('implausible_value')
        val = None
    return dict(row_number=number, label=label, pcu=row.get('pcu',''), object_type=object_type,
                object_number=object_number, subtype=row.get('type_2',''), channel=row.get('idx_2',''),
                local_date=local.date().isoformat() if local else None,
                end_date=end.date().isoformat() if end else None,
                timestamp_utc=local.astimezone(timezone.utc).replace(tzinfo=None) if local else None,
                value=val, original_value=raw, unit=None,
                quality_json=json.dumps(flags), raw_json=json.dumps(row, ensure_ascii=False))


def iter_rows(path: Path):
    # Export examples are UTF-8. CP1252 is accepted after a streaming validation.
    encoding = 'utf-8-sig'
    try:
        with path.open(encoding=encoding) as stream:
            while stream.read(65536):
                pass
    except UnicodeDecodeError:
        encoding = 'cp1252'
    with path.open(encoding=encoding, newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if reader.fieldnames != list(HEADERS):
            raise ValueError('CSV-kop moet label;pcu;type_1;idx_1;type_2;idx_2;startdate;enddate;value zijn')
        for n, row in enumerate(reader, 1):
            if n > 5_000_000:
                raise ValueError('Meer dan 5 miljoen rijen in één bestand')
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Onjuist aantal velden op rij {n + 1}')
            if any(len(value) > 8192 for value in row.values()):
                raise ValueError(f'Te lang CSV-veld op rij {n + 1}')
            yield n, row


def recognize_profile(labels: set, types: set, departments: set) -> str:
    if any('Valve' in x for x in labels) and 'Kraan' in types:
        return 'kranen'
    if any('ModGrowlight' in x for x in labels) or 'Groeilicht sturing' in types:
        return 'belichting'
    if any(x.startswith('Sum_24h_Vg') or 'MoistDrain' in x for x in labels):
        return 'algemeen_afdeling'
    if len(departments) > 1 or any('CO2ProgActive' in x or x == 'Sum_Day_Radiation' for x in labels):
        return 'algemeen_bedrijf'
    if any('Comp' in x or 'Curtain' in x or 'DewPointDifference' in x for x in labels):
        return 'klimaat'
    raise ValueError('Onbekend Priva-profiel: inhoud niet betrouwbaar herkend')


def inspect_file(path: Path, name: str) -> dict:
    labels, types, departments, objects, dates, pcus = set(), set(), set(), set(), set(), set()
    quality, sample = Counter(), []
    row_count = 0
    # Only series identities are held, bounded independently from row count.
    coverage = defaultdict(int)
    for n, row in iter_rows(path):
        row_count = n
        item = normalize_row(row, n)
        labels.add(row['label']); types.add(row['type_1']); pcus.add(row['pcu'])
        objects.add((row['type_1'],row['idx_1'],row['type_2'],row['idx_2']))
        if len(objects) > 10000 or len(labels) > 10000:
            raise ValueError('Te veel verschillende objecten/labels')
        if item['object_type'] == 'department' and item['object_number']:
            departments.add(item['object_number'])
        quality.update(json.loads(item['quality_json']))
        series_key=(item['label'],item['object_type'],item['object_number'],item['subtype'],item['channel'])
        coverage.setdefault(series_key,0)
        if len(coverage)>10000:
            raise ValueError('Meer dan 10.000 verschillende meetreeksen')
        if item['local_date']:
            dates.add(item['local_date'])
            if item['value'] is not None:
                # A 100-year bitset caps memory per series at about 5 KB.
                offset=(date.fromisoformat(item['local_date'])-date(2000,1,1)).days
                coverage[series_key] |= 1 << offset
        if len(sample) < 8:
            sample.append({k:v for k,v in item.items() if k not in ('timestamp_utc','raw_json','quality_json')} | {'quality':json.loads(item['quality_json'])})
    if not row_count:
        raise ValueError('Leeg CSV-bestand')
    profile = recognize_profile(labels, types, departments)
    days = sorted(dates)
    expected_days = (date.fromisoformat(days[-1])-date.fromisoformat(days[0])).days + 1 if days else 0
    missing = sum(max(0, expected_days-found.bit_count()) for found in coverage.values())
    if missing:
        quality['missing_series_days'] = missing
    key = json.dumps([profile,sorted(pcus),sorted(objects),days[0] if days else None,days[-1] if days else None])
    return dict(name=name,profile=profile,row_count=row_count,start=days[0] if days else None,end=days[-1] if days else None,
                years=sorted({int(d[:4]) for d in days}),weeks=sorted({f'{date.fromisoformat(d).isocalendar().year}-W{date.fromisoformat(d).isocalendar().week:02}' for d in days}),
                labels=sorted(labels),departments=sorted(departments),days=len(days),quality=dict(quality),sample=sample,
                logical_key=hashlib.sha256(key.encode()).hexdigest(),date_policy='dd-mm-jj: 00–69 = 2000–2069; 70–99 vereist viercijferig jaar; Europe/Amsterdam',
                unit_policy='Alle eenheden onbevestigd; geen DLI- of litersom afgeleid.')


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024*1024):
            h.update(chunk)
    return h.hexdigest()


def expand_archive(db, record: ImportFile):
    existing = db.scalars(select(ImportFile).where(ImportFile.job_id == record.job_id, ImportFile.archive == False)).all()
    if sha_file(original_path(record.path)) != record.checksum:
        raise ValueError('Checksum van origineel ZIP-bestand gewijzigd')
    total = sum(original_path(f.path).stat().st_size for f in existing if original_path(f.path).is_file())
    file_count = len(existing)
    with zipfile.ZipFile(original_path(record.path)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2:
            raise ValueError('ZIP bevat te veel items')
        seen_names=set()
        for index, entry in enumerate(entries):
            name = entry.filename
            if name in seen_names:
                raise ValueError('Dubbele padnaam in ZIP')
            seen_names.add(name)
            p = PurePosixPath(name.replace('\\','/'))
            if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or '\x00' in name:
                raise ValueError('Onveilig pad in ZIP')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symbolische links niet toegestaan in ZIP')
            if entry.is_dir():
                continue
            if not name.lower().endswith('.csv'):
                raise ValueError('ZIP mag alleen CSV-bestanden en mappen bevatten')
            if entry.flag_bits & 1:
                raise ValueError('Versleutelde ZIP niet ondersteund')
            display = f'{record.name}/{name}'
            path = original_path(record.path).parent / f'zip-{record.id}-{index}.csv'
            if db.scalar(select(ImportFile).where(ImportFile.job_id == record.job_id, ImportFile.path == stored_path(path))):
                continue
            total += entry.file_size
            file_count += 1
            if file_count > MAX_FILES:
                raise ValueError(f'Import bevat meer dan {MAX_FILES} CSV-bestanden')
            if entry.file_size > MAX_UPLOAD or total > MAX_EXPANDED or (entry.file_size > 1024*1024 and entry.file_size > max(entry.compress_size,1)*200):
                raise ValueError('ZIP overschrijdt limiet voor uitgepakte grootte/compressieverhouding')
            size = 0
            with archive.open(entry) as src, path.open('wb') as target:
                while chunk := src.read(1024*1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError('ZIP-item te groot')
                    target.write(chunk)
            db.add(ImportFile(job_id=record.job_id,name=display,path=stored_path(path),checksum=sha_file(path)))
            db.commit()


def job_dict(db, job):
    files = db.scalars(select(ImportFile).where(ImportFile.job_id == job.id)).all()
    sources = []
    for f in files:
        s = db.get(PrivaSource,f.source_id) if f.source_id else None
        sources.append(dict(id=f.id,name=f.name,checksum=f.checksum,archive=f.archive,source_id=f.source_id,duplicate=f.duplicate,
                            version=s.version if s else None,revoked=bool(s and s.revoked_at),supersedes_id=s.supersedes_id if s else None))
    return dict(id=job.id,status=job.status,phase=job.phase,created_at=job.created_at,confirmed_at=job.confirmed_at,
                progress_rows=job.progress_rows,total_rows=job.total_rows,preview=json.loads(job.preview_json),error=job.error,sources=sources)


def run_job(job_id):
    try:
        with SessionLocal() as db:
            job = db.get(ImportJob,job_id)
            if not job or job.revoked_at:
                return
            if job.phase == 'preview':
                job.status='previewing'; job.error=None; db.commit()
                for record in db.scalars(select(ImportFile).where(ImportFile.job_id==job_id,ImportFile.archive==True)).all():
                    expand_archive(db,record)
                previews=[]; counts=Counter()
                for record in db.scalars(select(ImportFile).where(ImportFile.job_id==job_id,ImportFile.archive==False)).all():
                    if _stop.is_set():
                        return
                    if sha_file(original_path(record.path)) != record.checksum:
                        raise ValueError('Checksum van origineel CSV-bestand gewijzigd')
                    report=inspect_file(original_path(record.path),record.name)
                    existing=db.scalar(select(PrivaSource).where(PrivaSource.checksum==record.checksum).order_by(PrivaSource.id.desc()))
                    previous=db.scalar(select(PrivaSource).where(PrivaSource.logical_key==report['logical_key']).order_by(PrivaSource.version.desc()))
                    report['duplicate_source_id']=existing.id if existing else None
                    report['duplicate_source_revoked']=bool(existing and existing.revoked_at)
                    report['corrects_source_id']=previous.id if previous and not existing else None
                    report['checksum']=record.checksum
                    active_ids=[s.id for s in active_sources(db)]
                    overlaps=db.scalars(select(PrivaValue.source_id).where(PrivaValue.source_id.in_(active_ids),PrivaValue.local_date>=str(report['start']),PrivaValue.local_date<=str(report['end']),PrivaValue.label.in_(report['labels'])).distinct().limit(101)).all() if active_ids else []
                    report['overlapping_source_ids']=overlaps[:100]
                    report['overlap_check']='Bronnen met mogelijk overlappende labels/datums; concrete waardebotsingen worden bij kalenderselectie gemeld.'
                    record.preview_json=json.dumps(report,ensure_ascii=False)
                    previews.append(report); counts.update(report['quality'])
                    job.progress_rows=sum(p['row_count'] for p in previews)
                    db.commit()
                if not previews:
                    raise ValueError('Geen CSV-bestanden gevonden')
                job.total_rows=sum(p['row_count'] for p in previews)
                scope_counts=Counter(p['logical_key'] for p in previews)
                duplicate_scopes=[key for key,count in scope_counts.items() if count>1]
                if duplicate_scopes:
                    counts['overlapping_files_in_batch']=sum(scope_counts[key]-1 for key in duplicate_scopes)
                job.preview_json=json.dumps(dict(files=previews,quality=dict(counts),overlapping_file_scopes=duplicate_scopes,**priority_policy(db)),ensure_ascii=False)
                job.status='preview'; db.commit()
                return
            job.status='importing'; job.error=None; db.commit()
            for record in db.scalars(select(ImportFile).where(ImportFile.job_id==job_id,ImportFile.archive==False)).all():
                if _stop.is_set():
                    return
                report=json.loads(record.preview_json)
                if sha_file(original_path(record.path)) != record.checksum:
                    raise ValueError('Checksum van origineel gewijzigd sinds voorbeeldcontrole')
                source=db.scalar(select(PrivaSource).where(PrivaSource.checksum==record.checksum,PrivaSource.revoked_at.is_(None)).order_by(PrivaSource.id.desc()))
                if source and source.job_id != job_id:
                    if source.activated_at is None:
                        raise ValueError(f'Identieke bron hoort bij onvoltooide import {source.job_id}; hervat die import eerst')
                    record.source_id=source.id; record.duplicate=True; db.commit(); continue
                if source is None:
                    previous=db.scalar(select(PrivaSource).where(PrivaSource.logical_key==report['logical_key']).order_by(PrivaSource.version.desc()))
                    source=PrivaSource(job_id=job_id,name=record.name,path=record.path,checksum=record.checksum,profile=report['profile'],logical_key=report['logical_key'],version=previous.version+1 if previous else 1,supersedes_id=previous.id if previous else None)
                    db.add(source); db.flush(); record.source_id=source.id
                    from .models import User
                    audit(db,db.get(User,job.user_id),'import_source_version',str(source.id),{'checksum':source.checksum,'version':source.version,'supersedes_id':source.supersedes_id})
                    db.commit()
                else:
                    record.source_id=source.id
                    db.commit()
                batch=[]
                for n,row in iter_rows(original_path(record.path)):
                    if n <= source.imported_rows:
                        continue
                    if _stop.is_set():
                        return
                    batch.append(normalize_row(row,n))
                    if len(batch)>=BATCH:
                        _write_batch(db,job,source,batch); batch=[]
                if batch:
                    _write_batch(db,job,source,batch)
            # Entire import job becomes visible atomically after all files succeed.
            for source in db.scalars(select(PrivaSource).where(PrivaSource.job_id==job_id)).all():
                source.activated_at=utcnow()
            job.status='completed'; job.progress_rows=job.total_rows
            from .models import User
            audit(db,db.get(User,job.user_id),'import_completed',str(job.id),{'rows':job.total_rows})
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            job=db.get(ImportJob,job_id)
            if job and not job.revoked_at:
                job.status='failed'; job.error=str(exc)[:1000]; db.commit()
    finally:
        with _lock:
            _scheduled.discard(job_id)
        # Confirmation may arrive after the preview transaction commits but
        # before this worker releases its scheduling slot. Honor that enqueue.
        if not _stop.is_set():
            with SessionLocal() as db:
                pending=db.get(ImportJob,job_id)
                requeue=bool(pending and pending.status=='queued' and not pending.revoked_at)
            if requeue:
                schedule(job_id)


def _write_batch(db,job,source,batch):
    db.add_all([PrivaValue(source_id=source.id,**item) for item in batch])
    source.imported_rows=batch[-1]['row_number']
    job.progress_rows=db.scalar(select(func.coalesce(func.sum(PrivaSource.imported_rows),0)).where(PrivaSource.job_id==job.id)) or 0
    db.commit()


def schedule(job_id):
    global _executor
    with _lock:
        if job_id in _scheduled:
            return
        if _executor is None:
            _stop.clear(); _executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='priva')
        _scheduled.add(job_id)
        _executor.submit(run_job,job_id)


def recover_jobs():
    with SessionLocal() as db:
        for interrupted in db.scalars(select(ImportJob).where(ImportJob.status=='receiving')).all():
            interrupted.status='failed'; interrupted.error='Upload onderbroken; controleer bestanden en upload opnieuw.'
        db.commit()
        ids=db.scalars(select(ImportJob.id).where(ImportJob.status.in_(['queued','previewing','importing']))).all()
    for job_id in ids:
        schedule(job_id)


def shutdown_worker():
    global _executor
    _stop.set()
    with _lock:
        executor=_executor; _executor=None
    if executor:
        executor.shutdown(wait=True,cancel_futures=True)
    with _lock:
        _scheduled.clear()


@router.get('/api/imports')
def list_imports(offset:int=0,limit:int=100,user=Depends(require_user)):
    if offset<0 or not 1<=limit<=500:
        raise HTTPException(400,'Ongeldige paginering')
    with SessionLocal() as db:
        return [job_dict(db,j) for j in db.scalars(select(ImportJob).order_by(ImportJob.id.desc()).offset(offset).limit(limit)).all()]


@router.post('/api/imports')
async def upload_import(files: list[UploadFile] = File(...),user=Depends(require_write)):
    if not files or len(files)>MAX_FILES:
        raise HTTPException(400,f'Lever 1–{MAX_FILES} CSV/ZIP-bestanden')
    ROOT.mkdir(parents=True,exist_ok=True)
    with SessionLocal() as db:
        job=ImportJob(user_id=user.id,status='receiving'); db.add(job); db.commit(); db.refresh(job)
        folder=ROOT/str(job.id); folder.mkdir()
        total=0
        try:
            for index, upload in enumerate(files):
                name=PurePosixPath((upload.filename or '').replace('\\','/')).name
                suffix=Path(name).suffix.lower()
                if suffix not in ('.csv','.zip'):
                    raise ValueError('Alleen CSV en ZIP toegestaan')
                path=folder/f'upload-{index}{suffix}'
                size=0; h=hashlib.sha256()
                with path.open('wb') as target:
                    while chunk:=await upload.read(1024*1024):
                        size+=len(chunk); total+=len(chunk)
                        if size>MAX_UPLOAD or total>MAX_TOTAL:
                            raise ValueError('Upload te groot (64 MB/bestand, 256 MB/totaal)')
                        target.write(chunk); h.update(chunk)
                db.add(ImportFile(job_id=job.id,name=name,path=stored_path(path),checksum=h.hexdigest(),archive=suffix=='.zip'))
            job.status='queued'; audit(db,user,'import_upload',str(job.id),{'files':len(files)}); db.commit()
        except Exception as exc:
            job.status='failed'; job.error=str(exc); db.commit()
            raise HTTPException(400,str(exc))
        result=job_dict(db,job)
    schedule(job.id)
    return result


def get_job(db,job_id):
    job=db.get(ImportJob,job_id)
    if not job:
        raise HTTPException(404,'Import niet gevonden')
    return job


@router.get('/api/imports/{job_id}')
def import_status(job_id:int,user=Depends(require_user)):
    with SessionLocal() as db:
        return job_dict(db,get_job(db,job_id))


@router.post('/api/imports/{job_id}/confirm')
def confirm_import(job_id:int,user=Depends(require_write)):
    with SessionLocal() as db:
        job=get_job(db,job_id)
        if job.status=='completed':
            return job_dict(db,job)
        if job.status!='preview':
            raise HTTPException(409,'Alleen een gecontroleerd importvoorbeeld kan worden bevestigd')
        job.phase='import'; job.status='queued'; job.confirmed_at=utcnow(); job.progress_rows=0
        preview=json.loads(job.preview_json)
        audit(db,user,'import_confirm',str(job.id),{'priority':preview.get('source_priority'),'priorities':preview.get('priorities'),'settings_version_id':preview.get('priority_settings_version_id'),'priority_settings_as_of':preview.get('priority_settings_as_of')}); db.commit(); result=job_dict(db,job)
    schedule(job_id)
    return result


@router.post('/api/imports/{job_id}/resume')
def resume_import(job_id:int,user=Depends(require_write)):
    with SessionLocal() as db:
        job=get_job(db,job_id)
        if job.status!='failed' or job.revoked_at:
            raise HTTPException(409,'Alleen een mislukte import kan worden hervat')
        if any(not original_path(f.path).is_file() for f in db.scalars(select(ImportFile).where(ImportFile.job_id==job_id)).all()):
            raise HTTPException(409,'Een origineel ontbreekt; herstel eerst de back-up')
        job.status='queued'; job.error=None; audit(db,user,'import_resume',str(job.id),{}); db.commit(); result=job_dict(db,job)
    schedule(job_id)
    return result


@router.post('/api/imports/{job_id}/revoke')
def revoke_import(job_id:int,user=Depends(require_write)):
    with SessionLocal() as db:
        job=get_job(db,job_id)
        if job.status in ('queued','previewing','importing','receiving'):
            raise HTTPException(409,'Wacht tot de lopende verwerking is afgerond')
        if not job.revoked_at:
            job.status='revoked'; job.revoked_at=utcnow()
            for source in db.scalars(select(PrivaSource).where(PrivaSource.job_id==job_id)).all():
                source.revoked_at=job.revoked_at
            audit(db,user,'import_revoke',str(job.id),{'originals_preserved':True}); db.commit()
        return job_dict(db,job)


def active_sources(db):
    rows=db.scalars(select(PrivaSource).where(PrivaSource.activated_at.is_not(None),PrivaSource.revoked_at.is_(None)).order_by(PrivaSource.version.desc(),PrivaSource.id.desc())).all()
    found={}
    for source in rows:
        found.setdefault(source.logical_key,source)
    return list(found.values())


def effective_unit_quality(item,calibrations):
    day=date.fromisoformat(item.local_date)
    setting=matching_calibration(calibrations,'unit',day,object_type=item.object_type,object_number=item.object_number,label=item.label,channel=item.channel)
    flags=json.loads(item.quality_json)
    if setting:
        flags=[f for f in flags if f!='unknown_unit']
    return (json.loads(setting.value_json)['unit'] if setting else item.unit),flags


def derive_daily_light(values,mappings,calibrations,start,end,bay_number,truncated):
    from .calculations import lamp_dli, natural_dli
    reasons=set()
    if not bay_number or truncated:
        return dict(lamp_dli=None,daylight_dli=None,total_dli=None,daily=[],reasons=['Selecteer één kap en een volledige, niet-afgekorte periode voor lichtsommen.'])
    by_day=defaultdict(list)
    for item in values:
        by_day[item.local_date].append(item)
    daily=[]
    day=start
    while day<=end:
        items=by_day[day.isoformat()]
        expected={m.object_number for m in mappings if m.object_type=='light' and m.valid_from<=day and (m.valid_until is None or day<=m.valid_until)}
        groups=[]; covered=set(); incomplete=False; calibration_ids=set(); used_sources=set()
        hours_rows=[i for i in items if i.object_type=='light' and 'GrowlightSysHours' in i.label]
        for item in hours_rows:
            setting=matching_calibration(calibrations,'lamp',day,object_type=item.object_type,object_number=item.object_number,label=item.label,channel=item.channel)
            unit,flags=effective_unit_quality(item,calibrations)
            config=json.loads(setting.value_json) if setting else {}
            if not setting or config.get('hours_label')!=item.label or unit!=config.get('time_unit') or item.value is None or item.end_date!=item.local_date:
                incomplete=True; reasons.add('Lampuren, eenheid of kanaalbijdrage ontbreken of zijn niet bevestigd.'); continue
            hours=item.value/3600 if unit=='s' else item.value
            group=dict(id=f'{item.object_number}:{item.channel}',source_validated=True,ppfd_umol_m2_s=config['ppfd'],hours=hours,dim_fraction=config['dim_factor'])
            try:
                single=lamp_dli([group],configuration_validated=True)
            except ValueError:
                incomplete=True; reasons.add('Lampduur valt buiten 0–24 uur.'); continue
            groups.append(group); covered.add(item.object_number); calibration_ids.add(setting.id); used_sources.add(item.source_id)
        if not expected or covered!=expected:
            incomplete=True; reasons.add('Niet alle op deze datum gekoppelde lichtmodules hebben bevestigde meetwaarden.')
        try:
            lamp=lamp_dli(groups,configuration_validated=not incomplete)
        except ValueError:
            lamp={'mol_m2_day':None,'quality':'unknown'}
            reasons.add('Dubbele of ongeldige lampbijdragen.')
        transmission=matching_calibration(calibrations,'transmission',day,object_type='bay',object_number=bay_number)
        tx=json.loads(transmission.value_json) if transmission else {}
        radiation=[i for i in items if i.label=='Sum_24h_Radiation' and i.object_type in ('weather','radiation') and i.value is not None and i.end_date==i.local_date]
        natural={'mol_m2_day':None,'quality':'unknown'}
        if len(radiation)==1 and tx.get('no_screen_effect') is True:
            unit,_=effective_unit_quality(radiation[0],calibrations)
            try:
                natural=natural_dli(radiation[0].value,unit=unit,transmission=tx.get('factor'))
            except ValueError:
                reasons.add('Ongeldige stralingswaarde of transmissie.')
            if transmission: calibration_ids.add(transmission.id)
            used_sources.add(radiation[0].source_id)
        else:
            reasons.add('Daglicht vereist één stralingsbron, bevestigde J/cm2, transmissie en expliciet geen onbekend schermeffect; schermuren worden niet vermenigvuldigd.')
        if natural['mol_m2_day'] is None:
            reasons.add('Stralingseenheid/transmissie of schermeffect onvoldoende bevestigd.')
        lv,nv=lamp['mol_m2_day'],natural['mol_m2_day']
        daily.append(dict(date=day.isoformat(),lamp_dli=lv,daylight_dli=nv,total_dli=lv+nv if lv is not None and nv is not None else None,
                          quality='estimate' if lv is not None and nv is not None else 'incomplete',lamp_quality=lamp['quality'],daylight_quality=natural['quality'],
                          source_ids=sorted(used_sources),calibration_ids=sorted(calibration_ids)))
        day+=timedelta(days=1)
    def complete_sum(field):
        return sum(d[field] for d in daily) if daily and all(d[field] is not None for d in daily) else None
    return dict(lamp_dli=complete_sum('lamp_dli'),daylight_dli=complete_sum('daylight_dli'),total_dli=complete_sum('total_dli'),daily=daily,
                reasons=sorted(reasons),unit='mol/m2',quality='estimate' if complete_sum('total_dli') is not None else 'incomplete',
                assumptions='Daglichtfactor 0,0215 is een benadering; geen dagtotaal × schermuren. Kalibraties zijn expliciete gebruikersbevestigingen.')


@router.get('/api/climate')
def climate(start:date|None=None,end:date|None=None,bay_number:int|None=None,user=Depends(require_user)):
    from .models import MappingVersion
    end=end or datetime.now(TZ).date(); start=start or end-timedelta(days=30)
    if end<start or (end-start).days>3660:
        raise HTTPException(400,'Ongeldige periode (maximaal tien jaar)')
    if bay_number is not None and not 1<=bay_number<=40:
        raise HTTPException(400,'Kap moet tussen 1 en 40 liggen')
    with SessionLocal() as db:
        policy=priority_policy(db)
        calibrations=db.scalars(select(Calibration).where(Calibration.confirmed==True,Calibration.valid_from<=end)).all()
        sources=active_sources(db); source_map={s.id:s for s in sources}
        mappings=db.scalars(select(MappingVersion).where(MappingVersion.bay_number==bay_number)).all() if bay_number else []
        query=select(PrivaValue).where(PrivaValue.source_id.in_(source_map),PrivaValue.local_date>=start.isoformat(),PrivaValue.local_date<=end.isoformat()).order_by(PrivaValue.local_date,PrivaValue.id).limit(50001)
        grouped={}; counts=Counter(); unknown_dates=set(); departments=set(); conflicts=[]
        if bay_number:
            mapping_day=start
            while mapping_day<=end:
                for object_kind in ('department','valve','light'):
                    if not any(m.object_type==object_kind and m.valid_from<=mapping_day and (m.valid_until is None or mapping_day<=m.valid_until) for m in mappings):
                        unknown_dates.add(mapping_day.isoformat()); break
                mapping_day+=timedelta(days=1)
        truncated=False
        for index,item in enumerate(db.scalars(query).yield_per(BATCH)):
            if index>=50000:
                truncated=True; counts['response_truncated']=1; break
            day=date.fromisoformat(item.local_date)
            if bay_number and item.object_type not in ('weather','radiation'):
                applicable=[m for m in mappings if m.object_type==item.object_type and m.object_number==item.object_number and m.valid_from<=day and (m.valid_until is None or day<=m.valid_until)]
                if not applicable:
                    if not any(m.object_type==item.object_type and m.valid_from<=day and (m.valid_until is None or day<=m.valid_until) for m in mappings):
                        unknown_dates.add(item.local_date)
                    continue
            if item.object_type=='department': departments.add(item.object_number)
            unit,flags=effective_unit_quality(item,calibrations); counts.update(flags)
            key=(item.label,item.pcu,item.object_type,item.object_number,item.subtype,item.channel,item.local_date,item.end_date)
            previous=grouped.get(key)
            source=source_map[item.source_id]
            priority=(item.value is not None,policy['priorities'].get(source.profile,0),source.activated_at,source.id,item.row_number)
            if previous:
                old,old_priority=previous
                if old.value != item.value:
                    if len(conflicts)<100:
                        conflicts.append(dict(date=item.local_date,label=item.label,object_type=item.object_type,object_number=item.object_number,source_ids=[old.source_id,item.source_id],values=[old.value,item.value],selected_source_id=item.source_id if priority>old_priority else old.source_id))
                    counts['source_conflicts']+=1
                else:
                    counts['duplicate_values']+=1
            if previous is None or priority>previous[1]:
                grouped[key]=(item,priority)
        series={}
        dates=set()
        for key,(item,_) in grouped.items():
            group_key=key[:6]
            unit,flags=effective_unit_quality(item,calibrations)
            entry=series.setdefault(group_key,dict(label=item.label,pcu=item.pcu,object_type=item.object_type,object_number=item.object_number,subtype=item.subtype,channel=item.channel,unit=unit,dates=[]))
            if entry['unit']!=unit:
                entry['unit']=None
            entry['dates'].append(dict(date=item.local_date,end_date=item.end_date,value=item.value,source_id=item.source_id,count=1,unit=unit,quality=flags))
            dates.add(item.local_date)
        expected=(end-start).days+1
        counts['missing_calendar_days']=max(0,expected-len(dates))
        if unknown_dates: counts['mapping_uncertain_days']=len(unknown_dates)
        if bay_number is None:
            counts['missing_departments']=len({1,2,3,4}-departments)
        result=dict(start=start,end=end,bay_number=bay_number,series=list(series.values()),quality=dict(counts),mapping_uncertain=bool(unknown_dates) or bool(bay_number and not mappings),
                    conflicts=conflicts,truncated=truncated,**policy,sources=[dict(id=s.id,profile=s.profile,version=s.version,checksum=s.checksum) for s in sources],
                    derived=derive_daily_light([item for item,_ in grouped.values()],mappings,calibrations,start,end,bay_number,truncated),
                    date_basis='Werkelijke kalenderdatum Europe/Amsterdam; geen koppeling via weeknummer.')
        # Optionele Priva Bridge (standaard uit): voegt alleen iets toe als de instelling aan staat.
        from .bridge import apply_to_climate
        own={(item.local_date,feature) for item,_ in grouped.values() for label,feature in (('Ave_24h_CompTemp','temperature'),('Ave_24h_CompRV','rh'))
             if item.label==label and item.object_type=='department' and item.value is not None and not item.subtype}
        apply_to_climate(db,result,start=start,end=end,bay_number=bay_number,own=own)
        return result
