"""Archived Excel lighting, explicitly supplemental to Priva hours.

The original HistoryImport XLSX remains the fine-grained source. Streaming
parsing stores ~one row per day/module, not one ORM object per workbook cell.
Register router, recover_jobs() and shutdown_worker() in the application.
"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import threading
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring, iterparse
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import audit, require_user, require_write
from .db import Base, DATA_DIR, SessionLocal, get_db
from .history_import import HistoryImport
from .importer import Calibration, PrivaSource, PrivaValue, matching_calibration, parse_priva_date
from .models import User

router=APIRouter(tags=['Aanvullende Excel-belichting'])
NL=ZoneInfo('Europe/Amsterdam')
X='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
MAX_RAW=32*1024*1024
MAX_EXPANDED=512*1024*1024
MAX_ROWS=1_000_000
MAX_DAILY=150_000
MAX_MODULES=128
SAMPLE_SECONDS=300
SLOTS=86400//SAMPLE_SECONDS
LABEL='legacy_excel_full_power_hours'
PRIVA_HOURS={'Sum_24h_ModGrowlightSysHours','Sum_24h_GrowlightSysHours'}
_executor=None
_lock=threading.Lock()
_stop=threading.Event()
_scheduled=set()


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _midnight(day):
    return datetime.combine(day,time(),NL).astimezone(timezone.utc).replace(tzinfo=None)


def _path(value):
    path=Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Ongeldig opgeslagen bronpad')
    return DATA_DIR/path


def _checksum(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024):
            h.update(block)
    return h.hexdigest()


class LegacyLightingJob(Base):
    __tablename__='legacy_lighting_jobs'
    id: Mapped[int]=mapped_column(primary_key=True)
    history_import_id: Mapped[int]=mapped_column(ForeignKey('history_imports.id'),index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    filename: Mapped[str]=mapped_column(Text)
    raw_path: Mapped[str]=mapped_column(Text)
    checksum: Mapped[str]=mapped_column(String(64),index=True)
    source_key: Mapped[str|None]=mapped_column(String(64),index=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    supersedes_id: Mapped[int|None]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(String(30),default='queued')
    phase: Mapped[str]=mapped_column(String(20),default='preview')
    progress_rows: Mapped[int]=mapped_column(Integer,default=0)
    total_rows: Mapped[int]=mapped_column(Integer,default=0)
    resume_row: Mapped[int]=mapped_column(Integer,default=3)
    daily_rows: Mapped[int]=mapped_column(Integer,default=0)
    preview: Mapped[dict]=mapped_column(JSON,default=dict)
    interpretation: Mapped[dict|None]=mapped_column(JSON)
    reason: Mapped[str|None]=mapped_column(Text)
    error: Mapped[str|None]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=now)
    confirmed_at: Mapped[datetime|None]=mapped_column(DateTime)
    activated_at: Mapped[datetime|None]=mapped_column(DateTime,index=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime,index=True)


class LegacyLightingDay(Base):
    __tablename__='legacy_lighting_days'
    __table_args__=(UniqueConstraint('job_id','day','module'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    job_id: Mapped[int]=mapped_column(ForeignKey('legacy_lighting_jobs.id'),index=True)
    day: Mapped[date]=mapped_column(Date,index=True)
    module: Mapped[int]=mapped_column(Integer,index=True)
    column: Mapped[str]=mapped_column(String(8))
    row_count: Mapped[int]=mapped_column(Integer,default=0)
    valid_count: Mapped[int]=mapped_column(Integer,default=0)
    missing_count: Mapped[int]=mapped_column(Integer,default=0)
    invalid_count: Mapped[int]=mapped_column(Integer,default=0)
    formula_count: Mapped[int]=mapped_column(Integer,default=0)
    off_grid_count: Mapped[int]=mapped_column(Integer,default=0)
    duplicate_count: Mapped[int]=mapped_column(Integer,default=0)
    percent_sum: Mapped[float]=mapped_column(Float,default=0)
    percent_min: Mapped[float|None]=mapped_column(Float)
    percent_max: Mapped[float|None]=mapped_column(Float)
    seen_mask: Mapped[str]=mapped_column(Text,default='0')
    valid_mask: Mapped[str]=mapped_column(Text,default='0')
    row_ranges: Mapped[list]=mapped_column(JSON,default=list)
    quality: Mapped[list]=mapped_column(JSON,default=list)


class Interpretation(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    timezone: Literal['Europe/Amsterdam']='Europe/Amsterdam'
    sample_seconds: Literal[300]=300
    date_system: Literal['1900','1904']='1900'
    timestamp_role: Literal['unknown','interval_start','interval_end']='unknown'
    value_kind: Literal['unknown','linear_dim_percent']='unknown'
    unit: Literal['%']='%'


class ConfirmInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmed: Literal[True]
    reason: str=Field(min_length=3,max_length=2000)
    interpretation: Interpretation


class StartInput(BaseModel):
    new_version: bool=False


def _validate_archive(archive):
    infos=archive.infolist()
    if len(infos)>10000 or len({i.filename.casefold() for i in infos})!=len(infos):
        raise ValueError('Te veel of dubbele werkboekonderdelen')
    total=0
    for item in infos:
        path=PurePosixPath(item.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in item.filename or item.flag_bits&1:
            raise ValueError('Onveilig of versleuteld werkboekonderdeel')
        if (item.external_attr>>16)&0o170000==0o120000:
            raise ValueError('Symbolische links zijn niet toegestaan')
        total+=item.file_size
        if total>MAX_EXPANDED or item.file_size>MAX_EXPANDED:
            raise ValueError('Werkboek groter dan 512 MiB uitgepakt')
        if item.file_size>1024*1024 and item.file_size>max(1,item.compress_size)*250:
            raise ValueError('Onveilige werkboek-compressieverhouding')


def _small_xml(archive,name,limit=8*1024*1024):
    info=archive.getinfo(name)
    if info.file_size>limit:
        raise ValueError('Werkboekmetadata te groot')
    return fromstring(archive.read(name))


def _metadata(archive):
    _validate_archive(archive)
    workbook=_small_xml(archive,'xl/workbook.xml',2*1024*1024)
    properties=workbook.find(X+'workbookPr')
    epoch='1904' if properties is not None and properties.get('date1904') in ('1','true') else '1900'
    sheet=next((s for s in workbook.find(X+'sheets') if s.get('name','').casefold()=='data'),None)
    if sheet is None:
        raise ValueError('Werkblad data ontbreekt')
    rels=_small_xml(archive,'xl/_rels/workbook.xml.rels',2*1024*1024)
    relation=next((r for r in rels if r.get('Id')==sheet.get(R+'id')),None)
    if relation is None or relation.get('TargetMode')=='External':
        raise ValueError('Ongeldige werkbladverwijzing')
    target=relation.get('Target','')
    member=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
    if not member.startswith('xl/worksheets/') or '..' in PurePosixPath(member).parts:
        raise ValueError('Onveilig werkbladpad')
    strings=[]
    if 'xl/sharedStrings.xml' in archive.namelist():
        for element in _small_xml(archive,'xl/sharedStrings.xml'):
            strings.append(''.join(element.itertext()))
            if len(strings)>100000:
                raise ValueError('Te veel tekstlabels in werkboek')
    return dict(sheet=sheet.get('name'),member=member,date_system=epoch,strings=strings)


def _cell(cell,strings):
    kind=cell.get('t','n')
    val=cell.find(X+'v')
    raw=val.text if val is not None else None
    formula=cell.find(X+'f') is not None
    if kind=='s' and raw is not None:
        try: value=strings[int(raw)]
        except (ValueError,IndexError): raise ValueError('Ongeldige gedeelde tekstverwijzing')
    elif kind=='inlineStr':
        inline=cell.find(X+'is'); value=''.join(inline.itertext()) if inline is not None else None
    elif kind=='n' and raw is not None:
        try: value=float(raw)
        except ValueError: value=raw
    else:
        value=raw
    return value,formula,kind


def _excel_time(value,epoch):
    if not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('Tijdstempel is geen expliciete Excel-datumserie')
    # Workbook epoch is declared by XLSX. Reject unsupported century ranges.
    base=datetime(1904,1,1) if epoch=='1904' else datetime(1899,12,30)
    result=base+timedelta(seconds=round(value*86400,3))
    if not 2000<=result.year<=2099:
        raise ValueError('Datum buiten ondersteunde 2000–2099')
    return result


def _rows(path):
    """Yield header metadata then at most one parsed row at a time."""
    if path.stat().st_size>MAX_RAW:
        raise ValueError('Origineel werkboek groter dan 32 MiB')
    with zipfile.ZipFile(path) as archive:
        metadata=_metadata(archive)
        strings=metadata['strings']
        headers={}; columns=[]; parent=None; count=0
        with archive.open(metadata['member']) as stream:
            for event,element in iterparse(stream,events=('start','end')):
                if event=='start' and element.tag==X+'sheetData':parent=element
                if event!='end' or element.tag!=X+'row':continue
                row_number=int(element.get('r','0'))
                cells={}
                for cell in element:
                    ref=cell.get('r','')
                    column=re.sub(r'\d+$','',ref)
                    if not re.fullmatch('[A-Z]{1,3}',column):
                        raise ValueError('Ongeldige celverwijzing')
                    cells[column]=_cell(cell,strings)
                if row_number<=3:
                    headers[row_number]={col:value[0] for col,value in cells.items()}
                    if row_number==3:
                        for column,header in headers.get(2,{}).items():
                            match=re.fullmatch(r'(.+):MOD\.\s*LICHT\s+(\d+)',str(header),re.IGNORECASE)
                            if match and headers.get(1,{}).get(column)=='Gem. licht%' and headers[3].get(column)=='%':
                                columns.append(dict(module=int(match.group(2)),column=column,header=header,unit='%',pcu=match.group(1).strip()))
                        if not columns or len(columns)>MAX_MODULES or len({c['module'] for c in columns})!=len(columns) or len({c['pcu'] for c in columns})!=1:
                            raise ValueError('Werkelijke module-/percentagekoppen niet betrouwbaar herkend')
                        metadata={k:v for k,v in metadata.items() if k!='strings'}|dict(modules=columns,header_rows=headers)
                        yield ('header',metadata)
                else:
                    if not columns:
                        raise ValueError('Eerste drie kopregels ontbreken')
                    count+=1
                    if count>MAX_ROWS:
                        raise ValueError('Meer dan één miljoen tijdstempelrijen')
                    # Ignore styled blank tail rows, but preserve a warning if a
                    # populated module row has no usable timestamp.
                    stamp=cells.get('A',(None,False,'n'))
                    values=[cells.get(c['column'],(None,False,'n')) for c in columns]
                    if stamp[0] is None and all(v[0] is None for v in values):
                        element.clear()
                        if parent is not None:parent.clear()
                        continue
                    try:
                        stamp_value=_excel_time(stamp[0],metadata['date_system']) if not stamp[1] else None
                    except (ValueError,OverflowError): stamp_value=None
                    yield ('row',(row_number,stamp_value,values))
                element.clear()
                if parent is not None:parent.clear()


def scan_workbook(path,progress=None):
    metadata=None; rows=0; dates=set(); masks={}; quality=Counter(); sample=[]
    last=None; smallest=largest=None
    for kind,payload in _rows(path):
        if kind=='header':
            metadata=payload; continue
        row,stamp,values=payload; rows+=1
        if stamp is None:
            quality['invalid_timestamp_rows']+=1; continue
        dates.add(stamp.date())
        if last is not None:
            delta=(stamp-last).total_seconds()
            if delta<0:quality['out_of_order_segments']+=1
            elif delta!=SAMPLE_SECONDS:quality['timestamp_gap_segments']+=1
        last=stamp
        seconds=stamp.hour*3600+stamp.minute*60+stamp.second+stamp.microsecond/1e6
        slot=round(seconds/SAMPLE_SECONDS)
        if abs(seconds-slot*SAMPLE_SECONDS)>.01 or not 0<=slot<SLOTS:
            quality['off_grid_timestamp_rows']+=1
        else:
            mask=masks.get(stamp.date(),0); bit=1<<slot
            if mask&bit:quality['duplicate_timestamp_rows']+=1
            masks[stamp.date()]=mask|bit
        for value,formula,cell_type in values:
            if formula:quality['formula_cells']+=1
            if value is None:quality['missing_value_cells']+=1
            elif not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100:
                quality['invalid_percentage_cells']+=1
            else:
                smallest=value if smallest is None else min(smallest,value)
                largest=value if largest is None else max(largest,value)
        if len(sample)<5:
            sample.append(dict(row_number=row,timestamp=stamp.isoformat(),cells={c['column']:(str(v[0]) if isinstance(v[0],float) and not math.isfinite(v[0]) else v[0]) for c,v in zip(metadata['modules'],values)}))
        if rows%2000==0 and progress:progress(rows)
    if metadata is None or not rows or not dates:
        raise ValueError('Geen bruikbare belichtingstijdstempels gevonden')
    ordered=sorted(dates)
    expected=(ordered[-1]-ordered[0]).days+1
    quality['missing_calendar_days']=expected-len(dates)
    quality['incomplete_timestamp_days']=sum(mask.bit_count()!=SLOTS for mask in masks.values())
    quality['dst_unresolved_days']=sum((_midnight(d+timedelta(days=1))-_midnight(d)).total_seconds()!=86400 for d in dates)
    source_key=hashlib.sha256(json.dumps([[(c['pcu'],c['module']) for c in metadata['modules']],ordered[0].isoformat(),ordered[-1].isoformat()]).encode()).hexdigest()
    return metadata|dict(row_count=rows,start=ordered[0].isoformat(),end=ordered[-1].isoformat(),days=len(dates),
          quality=dict(quality),sample=sample,min_percent=smallest,max_percent=largest,source_key=source_key,
          source_kind='legacy_excel',priority='Priva-uren gaan altijd voor; ook ontbrekende/onbetrouwbare Priva-waarden blokkeren Excel-overlap.',
          interpretation_required=['tijdstempel begin/einde interval','percentage is lineaire dimfractie','werkelijke onafhankelijke lampcapaciteit'],
          warnings=['Geen hardwarekanaal in deze bron; channel is leeg.','De bron is aanvullend en nog niet fysisch gevalideerd.'])


def _new_stats(column):
    return dict(column=column,row_count=0,valid_count=0,missing_count=0,invalid_count=0,
          formula_count=0,off_grid_count=0,duplicate_count=0,percent_sum=0.,percent_min=None,
          percent_max=None,seen_mask=0,valid_mask=0,row_ranges=[])


def _add_sample(stats,row,stamp,value,formula,kind):
    stats['row_count']+=1
    if len(stats['row_ranges'])<128:
        if stats['row_ranges'] and stats['row_ranges'][-1][1]+1==row:
            stats['row_ranges'][-1][1]=row
        else: stats['row_ranges'].append([row,row])
    seconds=stamp.hour*3600+stamp.minute*60+stamp.second+stamp.microsecond/1e6
    slot=round(seconds/SAMPLE_SECONDS)
    grid=abs(seconds-slot*SAMPLE_SECONDS)<=.01 and 0<=slot<SLOTS
    if not grid: stats['off_grid_count']+=1
    else:
        bit=1<<slot
        if stats['seen_mask']&bit:stats['duplicate_count']+=1
        stats['seen_mask']|=bit
    if formula:stats['formula_count']+=1
    if value is None:
        stats['missing_count']+=1; return
    if kind!='n' or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100:
        stats['invalid_count']+=1; return
    stats['valid_count']+=1; stats['percent_sum']+=value
    stats['percent_min']=value if stats['percent_min'] is None else min(stats['percent_min'],value)
    stats['percent_max']=value if stats['percent_max'] is None else max(stats['percent_max'],value)
    if grid and not formula:stats['valid_mask']|=1<<slot


def _quality(day,record,interpretation):
    flags=[]
    if interpretation.get('value_kind')!='linear_dim_percent':flags.append('percentage_meaning_unknown')
    if interpretation.get('timestamp_role') not in ('interval_start','interval_end'):flags.append('timestamp_role_unknown')
    if (_midnight(day+timedelta(days=1))-_midnight(day)).total_seconds()!=86400:flags.append('dst_clock_unresolved')
    if int(record.valid_mask,16).bit_count()!=SLOTS:flags.append('incomplete_samples')
    if len(record.row_ranges)>=128:flags.append('provenance_ranges_truncated')
    for field,flag in [('missing_count','missing_values'),('invalid_count','invalid_values'),('formula_count','formula_values'),
                       ('off_grid_count','off_grid_timestamps'),('duplicate_count','duplicate_timestamps')]:
        if getattr(record,field):flags.append(flag)
    return flags


def _flush_day(db,job,day,stats,last_row,progress_rows):
    previous={r.module:r for r in db.scalars(select(LegacyLightingDay).where(LegacyLightingDay.job_id==job.id,LegacyLightingDay.day==day))}
    for module,values in stats.items():
        record=previous.get(module)
        if record is None:
            job.daily_rows=(job.daily_rows or 0)+1
            if job.daily_rows>MAX_DAILY:
                raise ValueError('Meer dan 150.000 dag/module-aggregaten')
            record=LegacyLightingDay(job_id=job.id,day=day,module=module,**(values|dict(seen_mask=hex(values['seen_mask'])[2:],valid_mask=hex(values['valid_mask'])[2:])))
            db.add(record)
        else:
            old_seen=int(record.seen_mask,16)
            record.duplicate_count+=values['duplicate_count']+(old_seen&values['seen_mask']).bit_count()
            record.seen_mask=hex(old_seen|values['seen_mask'])[2:]
            record.valid_mask=hex(int(record.valid_mask,16)|values['valid_mask'])[2:]
            for key in ('row_count','valid_count','missing_count','invalid_count','formula_count','off_grid_count','percent_sum'):
                setattr(record,key,getattr(record,key)+values[key])
            for key,fn in [('percent_min',min),('percent_max',max)]:
                candidates=[v for v in (getattr(record,key),values[key]) if v is not None]
                setattr(record,key,fn(candidates) if candidates else None)
            record.row_ranges=(record.row_ranges+values['row_ranges'])[:128]
        record.quality=_quality(day,record,job.interpretation)
    job.resume_row=last_row; job.progress_rows=progress_rows
    db.commit()


def _import_daily(db,job,path):
    stats={}; bucket=None; group_last=job.resume_row; group_count=job.progress_rows; rows_seen=0
    for kind,payload in _rows(path):
        if _stop.is_set(): return False
        if kind=='header':
            metadata=payload
            if metadata['date_system']!=job.interpretation['date_system']:
                raise ValueError('Bevestigde datumepoch wijkt af van het werkboek')
            continue
        row,stamp,values=payload; rows_seen+=1
        if row<=job.resume_row:continue
        if stamp is None:continue
        effective=stamp-timedelta(seconds=SAMPLE_SECONDS) if job.interpretation['timestamp_role']=='interval_end' else stamp
        day=effective.date()
        if bucket is not None and day!=bucket:
            _flush_day(db,job,bucket,stats,group_last,group_count)
            stats={}
        bucket=day
        for module,value in zip(metadata['modules'],values):
            if module['module'] not in stats:
                stats[module['module']]=_new_stats(module['column'])
            entry=stats[module['module']]
            _add_sample(entry,row,effective,*value)
        group_last=row; group_count=rows_seen
    if bucket is not None:
        _flush_day(db,job,bucket,stats,group_last,group_count)
    job.progress_rows=job.total_rows
    db.commit()
    return True


def _job_dict(job):
    return {name:getattr(job,name) for name in ('id','history_import_id','filename','checksum','source_key','version','supersedes_id',
          'status','phase','progress_rows','total_rows','daily_rows','preview','interpretation','reason','error','created_at','confirmed_at','activated_at','revoked_at')}|dict(source_kind='legacy_excel',primary_policy='Priva gaat altijd voor')


def _get_job(db,job_id):
    job=db.get(LegacyLightingJob,job_id)
    if job is None:raise HTTPException(404,'Excel-belichtingsimport niet gevonden')
    return job


def run_job(job_id):
    try:
        with SessionLocal() as db:
            job=db.get(LegacyLightingJob,job_id)
            if job is None or job.revoked_at:return
            path=_path(job.raw_path)
            if not path.is_file() or _checksum(path)!=job.checksum:
                raise ValueError('Origineel ontbreekt of checksum is gewijzigd; herstel de bron uit de back-up')
            job.error=None
            if job.phase=='preview':
                job.status='previewing'; job.progress_rows=0; db.commit()
                def progress(rows):
                    if _stop.is_set():raise InterruptedError('Verwerking wordt hervat na herstart')
                    job.progress_rows=rows; db.commit()
                preview=scan_workbook(path,progress)
                job.preview=preview; job.total_rows=preview['row_count']; job.progress_rows=job.total_rows
                job.source_key=preview['source_key']
                previous=db.scalar(select(LegacyLightingJob).where(LegacyLightingJob.source_key==job.source_key,LegacyLightingJob.id!=job.id).order_by(LegacyLightingJob.version.desc(),LegacyLightingJob.id.desc()))
                if previous:
                    job.version=max(job.version,previous.version+1)
                    if job.supersedes_id is None:job.supersedes_id=previous.id
                job.status='preview'; db.commit()
                return
            job.status='importing'; db.commit()
            if not _import_daily(db,job,path):return
            if db.scalar(select(func.count(LegacyLightingDay.id)).where(LegacyLightingDay.job_id==job.id))>MAX_DAILY:
                raise ValueError('Meer dan 150.000 dag/module-aggregaten')
            job.activated_at=now(); job.status='completed'
            audit(db,db.get(User,job.user_id),'legacy_lighting_completed',str(job.id),{'rows':job.total_rows,'source_kind':'legacy_excel','checksum':job.checksum})
            db.commit()
    except InterruptedError:
        pass
    except Exception as exc:
        with SessionLocal() as db:
            job=db.get(LegacyLightingJob,job_id)
            if job is not None and job.revoked_at is None:
                job.status='failed'; job.error=str(exc)[:1000]; db.commit()
    finally:
        with _lock:_scheduled.discard(job_id)
        if not _stop.is_set():
            with SessionLocal() as db:
                pending=db.get(LegacyLightingJob,job_id)
                requeue=bool(pending and pending.status=='queued' and pending.revoked_at is None)
            if requeue:schedule(job_id)


def schedule(job_id):
    global _executor
    with _lock:
        if job_id in _scheduled:return
        if _executor is None:
            _stop.clear(); _executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='excel-lighting')
        _scheduled.add(job_id); _executor.submit(run_job,job_id)


def recover_jobs():
    with SessionLocal() as db:
        ids=list(db.scalars(select(LegacyLightingJob.id).where(LegacyLightingJob.status.in_(('queued','previewing','importing')))))
    for job_id in ids:schedule(job_id)


def shutdown_worker():
    global _executor
    _stop.set()
    with _lock:executor=_executor; _executor=None
    if executor:executor.shutdown(wait=True,cancel_futures=True)
    with _lock:_scheduled.clear()


@router.post('/api/legacy-lighting/from-history/{history_import_id}')
def from_history(history_import_id:int,payload:StartInput|None=None,db:Session=Depends(get_db),user=Depends(require_write)):
    history=db.get(HistoryImport,history_import_id)
    if history is None:raise HTTPException(404,'Geüpload historisch werkboek niet gevonden')
    prior=db.scalar(select(LegacyLightingJob).where(LegacyLightingJob.checksum==history.sha256).order_by(LegacyLightingJob.id.desc()))
    if prior and prior.revoked_at is None and not (payload and payload.new_version):return _job_dict(prior)
    if prior and prior.status in ('queued','previewing','importing'):raise HTTPException(409,'De eerdere belichtingsimport wordt nog verwerkt')
    job=LegacyLightingJob(history_import_id=history.id,user_id=user.id,filename=history.filename,raw_path=history.raw_path,
         checksum=history.sha256,status='queued',version=prior.version+1 if prior else 1,supersedes_id=prior.id if prior else None)
    db.add(job); db.flush(); audit(db,user,'legacy_lighting_start',str(job.id),{'history_import_id':history.id,'checksum':history.sha256})
    db.commit(); result=_job_dict(job); schedule(job.id)
    return result


@router.get('/api/legacy-lighting')
def list_jobs(offset:int=0,limit:int=100,db:Session=Depends(get_db),user=Depends(require_user)):
    if offset<0 or not 1<=limit<=500:raise HTTPException(400,'Ongeldige paginering')
    return [_job_dict(j) for j in db.scalars(select(LegacyLightingJob).order_by(LegacyLightingJob.id.desc()).offset(offset).limit(limit))]


@router.get('/api/legacy-lighting/{job_id:int}')
def job_status(job_id:int,db:Session=Depends(get_db),user=Depends(require_user)):
    return _job_dict(_get_job(db,job_id))


@router.post('/api/legacy-lighting/{job_id:int}/confirm')
def confirm_job(job_id:int,payload:ConfirmInput,db:Session=Depends(get_db),user=Depends(require_write)):
    job=_get_job(db,job_id)
    if job.status=='completed':
        if job.interpretation!=payload.interpretation.model_dump():
            raise HTTPException(409,'Een nieuwe interpretatie vereist een nieuwe bronversie via from-history met new_version:true')
        return _job_dict(job)
    if job.status!='preview':raise HTTPException(409,'Controleer eerst het volledige bronvoorbeeld')
    if payload.interpretation.date_system!=job.preview['date_system']:
        raise HTTPException(422,'Datumepoch moet overeenkomen met het bronwerkboek')
    if len(payload.reason.strip())<3:raise HTTPException(422,'Geef een reden voor de broninterpretatie')
    job.interpretation=payload.interpretation.model_dump(); job.reason=payload.reason.strip()
    job.confirmed_at=now(); job.phase='import'; job.status='queued'; job.progress_rows=0
    audit(db,user,'legacy_lighting_confirm',str(job.id),{'interpretation':job.interpretation,'reason':job.reason,'checksum':job.checksum,'priority':'Priva altijd eerst'})
    db.commit(); result=_job_dict(job); schedule(job.id)
    return result


@router.post('/api/legacy-lighting/{job_id:int}/resume')
def resume_job(job_id:int,db:Session=Depends(get_db),user=Depends(require_write)):
    job=_get_job(db,job_id)
    if job.status!='failed' or job.revoked_at:raise HTTPException(409,'Alleen een mislukte import kan worden hervat')
    job.status='queued'; job.error=None; audit(db,user,'legacy_lighting_resume',str(job.id),{})
    db.commit(); result=_job_dict(job); schedule(job.id)
    return result


@router.post('/api/legacy-lighting/{job_id:int}/revoke')
def revoke_job(job_id:int,db:Session=Depends(get_db),user=Depends(require_write)):
    job=_get_job(db,job_id)
    if job.status in ('queued','previewing','importing'):raise HTTPException(409,'Wacht tot de lopende verwerking is afgerond')
    if job.revoked_at is None:
        job.revoked_at=now(); job.status='revoked'
        audit(db,user,'legacy_lighting_revoke',str(job.id),{'original_and_daily_rows_preserved':True}); db.commit()
    return _job_dict(job)


def _active_jobs(db,as_of):
    ranked=select(LegacyLightingJob.id,func.row_number().over(partition_by=LegacyLightingJob.source_key,
        order_by=(LegacyLightingJob.activated_at.desc(),LegacyLightingJob.id.desc())).label('rank')).where(
        LegacyLightingJob.created_at<=as_of,LegacyLightingJob.activated_at<=as_of,
        or_(LegacyLightingJob.revoked_at.is_(None),LegacyLightingJob.revoked_at>as_of)).subquery()
    jobs=list(db.scalars(select(LegacyLightingJob).join(ranked,ranked.c.id==LegacyLightingJob.id)
         .where(ranked.c.rank==1).limit(5001)))
    return jobs[:5000],len(jobs)>5000


def _priva_presence(db,modules,start,end,as_of):
    ranked=select(PrivaSource.id,func.row_number().over(partition_by=PrivaSource.logical_key,
        order_by=(PrivaSource.version.desc(),PrivaSource.id.desc())).label('rank')).where(
        PrivaSource.created_at<=as_of,PrivaSource.activated_at<=as_of,
        or_(PrivaSource.revoked_at.is_(None),PrivaSource.revoked_at>as_of)).subquery()
    ids=list(db.scalars(select(PrivaSource.id).join(ranked,ranked.c.id==PrivaSource.id).where(ranked.c.rank==1).limit(5001)))
    if len(ids)>5000:return set(),True
    query=select(PrivaValue.local_date,PrivaValue.end_date,PrivaValue.object_number,PrivaValue.raw_json).where(
        PrivaValue.source_id.in_(ids),PrivaValue.object_type=='light',PrivaValue.object_number.in_(modules),
        PrivaValue.label.in_(PRIVA_HOURS),or_(PrivaValue.local_date.is_(None),
        (PrivaValue.local_date<=end.isoformat()) & or_(PrivaValue.local_date>=start.isoformat(),
        PrivaValue.end_date>=start.isoformat(),PrivaValue.end_date.is_(None))))
    found=set()
    for index,(first,last,module,raw_json) in enumerate(db.execute(query.limit(200001)).yield_per(500)):
        if index>=200000:return found,True
        try:raw=json.loads(raw_json)
        except (ValueError,TypeError):raw={}
        try:first_day=date.fromisoformat(first)
        except (ValueError,TypeError):
            try:first_day=parse_priva_date(raw.get('startdate','')).date()
            except (ValueError,TypeError):first_day=None
        try:last_day=date.fromisoformat(last)
        except (ValueError,TypeError):
            try:last_day=parse_priva_date(raw.get('enddate','')).date()
            except (ValueError,TypeError):last_day=None
        if first_day is None:
            # An unplaceable primary period cannot establish a safe Excel gap.
            day,finish=start,end
        else:
            if last_day is None or last_day<first_day:last_day=first_day
            day,finish=max(start,first_day),min(end,last_day)
        while day<=finish:
            found.add((day,module)); day+=timedelta(days=1)
    return found,False


def supplemental_daily_light(db:Session,modules:set[int],start:date,end:date,as_of:datetime,
                             priva_presence:set[tuple[date,int]]|None=None) -> dict:
    """Return module/day Excel contributions, never replacing any Priva hours.

    keys in days are ISO dates, then integer modules. Knowledge gates are UTC;
    start/end are inclusive local calendar dates. A full_power_hours value is
    already dim-weighted. Its lamp calibration MUST have dim_factor=1.
    """
    as_of=_utc(as_of); modules={int(m) for m in modules}
    if end<start or (end-start).days>3660 or len(modules)>MAX_MODULES:
        raise ValueError('Ongeldige begrensde belichtingsperiode of modulelijst')
    result=dict(days={},blocked_by_priva=[],warnings=[],truncated=False,
        source_kind='legacy_excel',primary_policy='Priva gaat altijd voor',
        source_ids=dict(legacy_lighting_job_ids=[],legacy_lighting_day_ids=[],history_import_ids=[],
                        calibration_ids=[],source_checksums=[]))
    if not modules:return result
    primary,primary_truncated=_priva_presence(db,modules,start,end,as_of)
    for day,module in priva_presence or set():
        primary.add((date.fromisoformat(day) if isinstance(day,str) else day,int(module)))
    jobs,jobs_truncated=_active_jobs(db,as_of)
    jobmap={j.id:j for j in jobs}
    calibrations=list(db.scalars(select(Calibration).where(Calibration.kind=='lamp',Calibration.created_at<=as_of,
        Calibration.confirmed==True,Calibration.valid_from<=end,
        or_(Calibration.valid_until.is_(None),Calibration.valid_until>=start)).limit(20001)))
    calibration_truncated=len(calibrations)>20000
    calibrations=calibrations[:20000]
    result['truncated']=primary_truncated or jobs_truncated or calibration_truncated
    selected={}; pcus={}
    query=select(LegacyLightingDay).where(LegacyLightingDay.job_id.in_(jobmap),LegacyLightingDay.module.in_(modules),
        LegacyLightingDay.day>=start,LegacyLightingDay.day<=end).order_by(LegacyLightingDay.id).limit(MAX_DAILY+1)
    for index,row in enumerate(db.scalars(query).yield_per(500)):
        if index>=MAX_DAILY:
            result['truncated']=True;break
        key=(row.day,row.module);old=selected.get(key)
        module_header=next((c for c in jobmap[row.job_id].preview.get('modules',[]) if c['module']==row.module),{})
        pcu=module_header.get('pcu') or str(module_header.get('header','')).split(':')[0]
        pcus.setdefault(key,set()).add(pcu)
        rank=(jobmap[row.job_id].activated_at,row.job_id,row.id)
        if old is None or rank>old[1]:selected[key]=(row,rank)
    jobids=set();dayids=set();historyids=set();calids=set();checksums=set()
    for (day,module),(row,_) in selected.items():
        job=jobmap[row.job_id]
        entry=dict(mol_m2_day=None,full_power_hours=None,quality='unverified',coverage=int(row.valid_mask,16).bit_count()/SLOTS,
             raw_percent_hours=row.percent_sum*SAMPLE_SECONDS/3600,raw_mean_percent=row.percent_sum/row.valid_count if row.valid_count else None,
             samples=row.row_count,valid_samples=row.valid_count,expected_samples=SLOTS,raw_unit='%',channel=None,
             warnings=list(row.quality),calibration_ids=[],
             source_ids=dict(legacy_lighting_job_id=job.id,legacy_lighting_day_id=row.id,history_import_id=job.history_import_id,checksum=job.checksum),
             cell_refs=[f"{job.preview.get('sheet','data')}!{row.column}{a}:{row.column}{b}" for a,b in row.row_ranges])
        jobids.add(job.id);dayids.add(row.id);historyids.add(job.history_import_id);checksums.add(job.checksum)
        if (day,module) in primary or primary_truncated:
            entry['quality']='blocked_by_priva'; entry['warnings'].append('Priva-uren aanwezig of de primaire periode is onzeker; Excel mag deze periode niet aanvullen, ook niet bij ongeldige Priva-waarden.')
            result['blocked_by_priva'].append(dict(date=day.isoformat(),module=module))
        elif len(pcus.get((day,module),set()))>1:
            entry['warnings'].append('Meerdere PCU-identiteiten voor dezelfde module/dag; geen fysieke toewijzing bevestigd.')
        elif _midnight(day+timedelta(days=1))>as_of:
            entry['warnings'].append('Lokale dag is op de kennispeildatum nog niet voltooid.')
        elif not result['truncated'] and not row.quality and job.interpretation and job.interpretation.get('value_kind')=='linear_dim_percent' and job.interpretation.get('timestamp_role') in ('interval_start','interval_end'):
            entry['full_power_hours']=row.percent_sum*SAMPLE_SECONDS/(100*3600)
            setting=matching_calibration(calibrations,'lamp',day,object_type='light',object_number=module,label=LABEL,channel='')
            config=json.loads(setting.value_json) if setting else {}
            if (setting and config.get('hours_label')==LABEL and config.get('time_unit')=='h' and
                  config.get('independent_contribution') is True and config.get('dim_factor')==1):
                from .calculations import lamp_dli
                try:
                    answer=lamp_dli([dict(id=f'excel-module-{module}',source_validated=True,
                        ppfd_umol_m2_s=config.get('ppfd'),hours=entry['full_power_hours'],dim_fraction=1)],configuration_validated=True)
                except (ValueError,TypeError):answer={'mol_m2_day':None}
                if answer['mol_m2_day'] is not None:
                    entry['mol_m2_day']=answer['mol_m2_day'];entry['quality']='verified_supplemental'
                    entry['calibration_ids']=[setting.id];calids.add(setting.id)
            if entry['mol_m2_day'] is None:
                entry['warnings'].append('Bevestig de onafhankelijke werkelijke PPFD voor legacy_excel_full_power_hours, leeg kanaal en dim_factor 1; de uren zijn al dimgewogen.')
        result['days'].setdefault(day.isoformat(),{})[module]=entry
    result['source_ids']=dict(legacy_lighting_job_ids=sorted(jobids),legacy_lighting_day_ids=sorted(dayids),
         history_import_ids=sorted(historyids),calibration_ids=sorted(calids),source_checksums=sorted(checksums))
    if result['truncated']:
        result['warnings'].append('Begrensde selectie afgekapt; geen aanvullende Excel-lichtsom mag worden gebruikt.')
        for dayrows in result['days'].values():
            for entry in dayrows.values():
                entry['mol_m2_day']=None;entry['full_power_hours']=None
                if entry['quality']!='blocked_by_priva':entry['quality']='unverified'
    return result


def supplemental_lighting(db,objects,start,end,knowledge_cutoff,primary_coverage_keys=None,**kwargs):
    return supplemental_daily_light(db,set(objects),start,end,knowledge_cutoff,primary_coverage_keys)


@router.get('/api/legacy-lighting/daily')
def daily_report(start:date,end:date,modules:str='101',as_of:datetime|None=None,
                 db:Session=Depends(get_db),user=Depends(require_user)):
    try:
        numbers={int(v.strip()) for v in modules.split(',') if v.strip()}
        cutoff=as_of or now()
        if cutoff.tzinfo is None and as_of is not None:
            if cutoff.replace(tzinfo=NL,fold=0).utcoffset()!=cutoff.replace(tzinfo=NL,fold=1).utcoffset():
                raise ValueError('Gebruik een expliciete tijdzone bij zomer-/wintertijd')
            cutoff=cutoff.replace(tzinfo=NL)
        cutoff=_utc(cutoff)
        if cutoff>now():raise ValueError('Kennispeildatum mag niet in de toekomst liggen')
        return supplemental_daily_light(db,numbers,start,end,cutoff)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
