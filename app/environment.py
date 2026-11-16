"""Point-in-time, observed Priva features for a cultivation.

Internal naive datetimes are UTC, matching the database and forecast module.
Only complete local daily records are used. No daily sum is prorated across a
measurement instant, and no missing measurement is filled with zero. Database
reads are streamed and bounded; exceeding a bound always prevents eligibility.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .auth import require_user
from .calculations import lamp_dli, natural_dli
from .db import get_db
from .importer import Calibration, PrivaSource, PrivaValue, matching_calibration, parse_priva_date
from .models import Cultivation, MappingRevision, Measurement, PlanningVersion, utcnow
from .settings import SettingsVersion, effective_settings
from .legacy_lighting import supplemental_daily_light, LABEL as EXCEL_HOURS

router = APIRouter(tags=['Teeltomgeving'])
FEATURE_VERSION = 'priva-environment-v2-supplemental'
NL = ZoneInfo('Europe/Amsterdam')
MAX_ROWS = 200_000
MAX_SOURCES = 5_000
MAX_METADATA = 20_000
MAX_DAYS = 3660
METRICS = ('lamp_light','temperature','rh','water','co2','daylight_estimate')
FEATURES = {'lamp_light':'env_lamp_light_mol_m2','temperature':'env_temperature_c',
            'rh':'env_rh_pct','water':'env_water_l_m2','co2':'env_co2_ppm',
            'daylight_estimate':'env_daylight_estimate_mol_m2'}
LABELS = {'Ave_24h_CompTemp','Ave_24h_CompRV','Ave_24h_CompCO2','Sum_24h_Valve/m2',
          'Sum_24h_ModGrowlightSysHours','Sum_24h_GrowlightSysHours','Sum_24h_Radiation'}
BLOCKING_QUALITY = {'invalid_date','invalid_value','missing_value','untrusted_co2_zero',
                    'invalid_par_zero','implausible_value'}


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()+'Z'


def _midnight(day: date) -> datetime:
    return datetime.combine(day,time(),NL).astimezone(timezone.utc).replace(tzinfo=None)


def _local_day(value: datetime) -> date:
    return _utc(value).replace(tzinfo=timezone.utc).astimezone(NL).date()


def _bounded(items, limit):
    rows=list(items)
    return rows[:limit], len(rows)>limit


def _sources_as_of(db,cutoff):
    # Revoking or correcting a source later must not rewrite an earlier snapshot.
    ranked=select(PrivaSource.id,func.row_number().over(partition_by=PrivaSource.logical_key,
                order_by=(PrivaSource.version.desc(),PrivaSource.id.desc())).label('rank')).where(
                PrivaSource.created_at<=cutoff,PrivaSource.activated_at<=cutoff,
                or_(PrivaSource.revoked_at.is_(None),PrivaSource.revoked_at>cutoff)).subquery()
    return _bounded(db.scalars(select(PrivaSource).join(ranked,ranked.c.id==PrivaSource.id)
                    .where(ranked.c.rank==1).order_by(PrivaSource.id).limit(MAX_SOURCES+1)),MAX_SOURCES)


def _mappings_as_of(db,bay_number,cutoff):
    # Filtering bay_number before ranking would resurrect an older mapping if a
    # later immutable revision moves that mapping to a different bay.
    ranked=select(MappingRevision.id,func.row_number().over(partition_by=MappingRevision.mapping_id,
                order_by=(MappingRevision.created_at.desc(),MappingRevision.id.desc())).label('rank')).where(
                MappingRevision.created_at<=cutoff).subquery()
    return _bounded(db.scalars(select(MappingRevision).join(ranked,ranked.c.id==MappingRevision.id)
                    .where(ranked.c.rank==1,MappingRevision.bay_number==bay_number)
                    .order_by(MappingRevision.id).limit(MAX_METADATA+1)),MAX_METADATA)


def _daily_interval(item):
    try:
        raw=json.loads(item.raw_json)
        begin=parse_priva_date(raw['startdate'])
        finish=parse_priva_date(raw['enddate'])
        # The actual examples export daily labels with equal date-only bounds.
        # Multi-day sums cannot be distributed without inventing daily values.
        if (begin.date()!=finish.date() or begin.date().isoformat()!=item.local_date or
                finish.date().isoformat()!=item.end_date or
                any((value.hour,value.minute,value.second)!=(0,0,0) for value in (begin,finish))):
            return None
        return begin.date(),_midnight(begin.date()),_midnight(begin.date()+timedelta(days=1))
    except (ValueError,TypeError,KeyError,json.JSONDecodeError):
        return None


def _valid_number(item):
    flags=set(json.loads(item.quality_json))
    return item.value is not None and math.isfinite(item.value) and not flags.intersection(BLOCKING_QUALITY)


def _setting(calibrations,kind,day,item=None,**scope):
    if item is not None:
        scope=dict(object_type=item.object_type,object_number=item.object_number,label=item.label,channel=item.channel)
    return matching_calibration(calibrations,kind,day,**scope)


def _unit(calibrations,day,item):
    setting=_setting(calibrations,'unit',day,item)
    if not setting:
        return None,None
    try:
        return json.loads(setting.value_json)['unit'],setting.id
    except (ValueError,KeyError,TypeError):
        return None,None


def _mapping_day(mappings,day,kind):
    return [m for m in mappings if m.object_type==kind and m.valid_from<=day and
            (m.valid_until is None or day<=m.valid_until)]


def _one_candidate(items,quality):
    """A mapping does not identify a PCU; never average two different PCUs."""
    if len(items)!=1:
        if len(items)>1:
            quality['ambiguous_measurement_series']+=1
        return None
    return items[0]


def _aggregate(days,daily,metric,min_coverage):
    found=[daily[d][metric] for d in days if metric in daily.get(d,{})]
    expected=len(days); count=len(found)
    coverage=dict(expected_days=expected,valid_days=count,fraction=count/expected if expected else 0.0,
                  missing_dates=[d.isoformat() for d in days if metric not in daily.get(d,{})])
    if not found:
        return None,coverage
    if metric in ('temperature','rh','co2'):
        weight=sum(item['seconds'] for item in found)
        number=sum(item['value']*item['seconds'] for item in found)/weight
    else:
        number=sum(item['value'] for item in found)
    if not math.isfinite(number):
        coverage.update(observed_value=None, quality='invalid', reason='non_finite_aggregate')
        return None,coverage
    coverage['observed_value']=number
    coverage['unit']={'lamp_light':'mol/m2','daylight_estimate':'mol/m2','temperature':'°C','rh':'%',
                      'water':'L/m2','co2':'ppm'}[metric]
    coverage['quality']='estimate' if metric=='daylight_estimate' else 'verified'
    return (number if count/expected>=min_coverage else None),coverage


def environment_snapshot(db:Session,cultivation,plan,as_of:datetime,m1=None,m2=None,
                         profile='basic',min_coverage=1.0,include_daylight_estimate=False,
                         include_co2=False,observed_until:datetime|None=None) -> dict:
    """Return numeric observed features and reproducible provenance at a cutoff.

    Coverage is over completed local calendar days, ending at the most recent
    local midnight. The current partial day is explicitly omitted. Phase means
    use actual elapsed hours (including 23/25-hour DST days); phase sums never
    prorate a daily record crossing an exact measurement time.
    """
    if profile not in ('basic','light','climate'):
        raise ValueError('Onbekend omgevingsprofiel')
    if not isinstance(min_coverage,(float,int)) or isinstance(min_coverage,bool) or not math.isfinite(min_coverage) or not 0<=min_coverage<=1:
        raise ValueError('Dekking moet tussen 0 en 1 liggen')
    cutoff=_utc(as_of)
    observation_cutoff=min(cutoff,_utc(observed_until)) if observed_until is not None else cutoff
    settings=effective_settings(db,cutoff)
    settings_row=db.scalar(select(SettingsVersion).where(SettingsVersion.created_at<=cutoff,
                  SettingsVersion.effective_from<=cutoff).order_by(SettingsVersion.effective_from.desc(),SettingsVersion.id.desc()))
    priorities={name:getattr(settings,'priority_'+name) for name in
                ('algemeen_bedrijf','klimaat','belichting','kranen','algemeen_afdeling')}
    result=dict(feature_version=FEATURE_VERSION,profile=profile,features={},eligible=profile=='basic',
                missing=[],warnings=[],coverage={},phases={},quality={},truncated=False,
                configuration=dict(min_coverage=min_coverage,include_daylight_estimate=include_daylight_estimate,
                                   include_co2=include_co2,priorities=priorities),
                source_ids=dict(priva_source_ids=[],priva_value_ids=[],priva_value_ids_sha256=None,
                                priva_value_count=0,mapping_revision_ids=[],calibration_ids=[],
                                settings_id=settings_row.id if settings_row else None,priva_sources=[]),
                window=dict(start=None,end=None,as_of=_iso(cutoff),knowledge_cutoff=_iso(cutoff),observed_until=_iso(observation_cutoff)))
    if (plan is None or not plan.plant_date or not plan.light_out_date or
            plan.created_at is None or _utc(plan.created_at)>cutoff or
            plan.cultivation_id!=cultivation.id):
        result['missing'].append('planning_not_known_at_cutoff')
        return result
    start=plan.plant_date
    last_day=_local_day(observation_cutoff)
    if last_day<start or plan.light_out_date<start:
        result['missing'].append('invalid_cultivation_window')
        return result
    count=(last_day-start).days
    completed_end=_midnight(last_day)
    result['window']=dict(start=_iso(_midnight(start)),end=_iso(completed_end),as_of=_iso(cutoff),knowledge_cutoff=_iso(cutoff),observed_until=_iso(observation_cutoff),
                          complete_days=count,partial_day_excluded=observation_cutoff>completed_end,
                          unobserved_partial_day_seconds=max(0,(observation_cutoff-completed_end).total_seconds()),
                          coverage_basis='Voltooide lokale kalenderdagen; geen invulling van de lopende dag.')
    if count>MAX_DAYS:
        result['missing'].append('window_exceeds_bound'); result['truncated']=True; result['eligible']=False
        return result
    days=[start+timedelta(days=i) for i in range(count)]
    sources,source_truncated=_sources_as_of(db,cutoff)
    mappings,mapping_truncated=_mappings_as_of(db,cultivation.bay_number,cutoff)
    calibrations,calibration_truncated=_bounded(db.scalars(select(Calibration).where(Calibration.created_at<=cutoff,
            Calibration.confirmed==True,Calibration.valid_from<=last_day,
            or_(Calibration.valid_until.is_(None),Calibration.valid_until>=start))
            .order_by(Calibration.id).limit(MAX_METADATA+1)),MAX_METADATA)
    result['truncated']=source_truncated or mapping_truncated or calibration_truncated
    source_map={s.id:s for s in sources}
    mapped_numbers={kind:{m.object_number for m in mappings if m.object_type==kind}
                    for kind in ('department','valve','light')}
    relevant=or_(*(and_(PrivaValue.object_type==kind,PrivaValue.object_number.in_(numbers))
                    for kind,numbers in mapped_numbers.items()),
                    and_(PrivaValue.object_type.in_(('weather','radiation')),PrivaValue.label=='Sum_24h_Radiation'))
    query=select(PrivaValue).where(PrivaValue.source_id.in_(source_map),PrivaValue.local_date>=start.isoformat(),
            PrivaValue.local_date<=last_day.isoformat(),PrivaValue.label.in_(LABELS),relevant)
    quality=Counter(); selected={}
    for index,item in enumerate(db.scalars(query.order_by(PrivaValue.id).limit(MAX_ROWS+1)).yield_per(500)):
        if index>=MAX_ROWS:
            result['truncated']=True; break
        interval=_daily_interval(item)
        if interval is None:
            quality['invalid_or_multiday_interval']+=1; continue
        day,begin,end=interval
        if begin<_midnight(start) or end>observation_cutoff:
            quality['unfinished_daily_rows_excluded']+=1; continue
        if item.object_type in mapped_numbers and not any(m.object_number==item.object_number for m in _mapping_day(mappings,day,item.object_type)):
            quality['mapping_not_valid_on_day']+=1; continue
        key=(item.pcu,item.label,item.object_type,item.object_number,item.subtype,item.channel,day)
        source=source_map[item.source_id]
        rank=(_valid_number(item),priorities.get(source.profile,0),source.activated_at,source.id,item.row_number)
        old=selected.get(key)
        if old:
            quality['duplicate_values' if old[0].value==item.value else 'source_conflicts']+=1
        if old is None or rank>old[1]:
            selected[key]=(item,rank)
    by_day=defaultdict(list)
    for key,(item,_) in selected.items():
        by_day[key[-1]].append(item)
    used_values=set(); used_sources=set(); used_calibrations=set(); used_mappings={m.id for m in mappings}
    daily=defaultdict(dict)
    # The helper independently checks all active Priva hour records, including
    # invalid and multi-day records rejected above. No source priority setting
    # can allow Excel to replace a present primary record.
    supplement = supplemental_daily_light(db, mapped_numbers['light'], start,
        last_day-timedelta(days=1), cutoff) if days else dict(days={}, source_ids={}, warnings=[], truncated=False)
    result['truncated'] = result['truncated'] or supplement['truncated']
    result['supplemental_lighting'] = dict(primary_policy='Priva gaat altijd voor',
        source_ids=supplement['source_ids'], used_days=[], warnings=supplement['warnings'])

    def mark(items=(),calibration_ids=(),mapping_rows=()):
        for item in items:
            used_values.add(item.id); used_sources.add(item.source_id)
        used_calibrations.update(i for i in calibration_ids if i is not None)
        used_mappings.update(m.id for m in mapping_rows)

    for day in days:
        items=by_day.get(day,[])
        seconds=(_midnight(day+timedelta(days=1))-_midnight(day)).total_seconds()
        departments=_mapping_day(mappings,day,'department')
        if len({m.object_number for m in departments})!=1:
            quality['missing_or_ambiguous_department_mapping_days']+=1
        else:
            department=departments[0].object_number
            for metric,label,units,low,high in (
                ('temperature','Ave_24h_CompTemp',{'°C','C'},-40,65),
                ('rh','Ave_24h_CompRV',{'%'},0,100),
                ('co2','Ave_24h_CompCO2',{'ppm'},1,10000)):
                item=_one_candidate([i for i in items if i.object_type=='department' and i.object_number==department and i.label==label and not i.subtype],quality)
                if not item:
                    continue
                mark([item],mapping_rows=departments)
                unit,calibration_id=_unit(calibrations,day,item)
                if unit not in units:
                    quality['unknown_or_incompatible_unit_rows']+=1; continue
                mark(calibration_ids=[calibration_id])
                if metric=='co2' and (item.value==0 or 'untrusted_co2_zero' in json.loads(item.quality_json)):
                    quality['untrusted_co2_zero']+=1; continue
                if not _valid_number(item) or not low<=item.value<=high:
                    quality['invalid_'+metric+'_rows']+=1; continue
                daily[day][metric]=dict(value=item.value,seconds=seconds)
        valves=_mapping_day(mappings,day,'valve')
        if len({m.object_number for m in valves})!=1:
            quality['missing_or_ambiguous_valve_mapping_days']+=1
        else:
            item=_one_candidate([i for i in items if i.object_type=='valve' and i.object_number==valves[0].object_number and i.label=='Sum_24h_Valve/m2' and not i.subtype],quality)
            if item:
                mark([item],mapping_rows=valves)
                unit,unit_id=_unit(calibrations,day,item)
                if unit not in ('L/m2','L/m²'):
                    quality['unknown_or_incompatible_unit_rows']+=1
                elif _valid_number(item) and item.value>=0:
                    mark(calibration_ids=[unit_id]); daily[day]['water']=dict(value=item.value,seconds=seconds)
                else:
                    quality['invalid_water_rows']+=1
        lights=_mapping_day(mappings,day,'light')
        expected_modules={m.object_number for m in lights}
        hour_rows=[i for i in items if i.object_type=='light' and i.object_number in expected_modules and i.label in ('Sum_24h_ModGrowlightSysHours','Sum_24h_GrowlightSysHours')]
        observed_groups={(i.object_number,i.channel) for i in hour_rows}
        configured_groups={(c.object_number,c.channel) for c in calibrations if c.kind=='lamp' and c.object_type=='light' and c.object_number in expected_modules and c.channel is not None and json.loads(c.value_json).get('hours_label')!=EXCEL_HOURS and c.valid_from<=day and (c.valid_until is None or day<=c.valid_until)}
        primary_modules={i.object_number for i in hour_rows}
        required_groups={g for g in configured_groups if g[0] in primary_modules}
        groups=[]; lamp_complete=bool(expected_modules) and required_groups.issubset(observed_groups)
        group_modules=set()
        for number,channel in sorted(observed_groups):
            item=_one_candidate([i for i in hour_rows if i.object_number==number and i.channel==channel],quality)
            if item is None:
                lamp_complete=False; continue
            mark([item],mapping_rows=[m for m in lights if m.object_number==number])
            setting=_setting(calibrations,'lamp',day,item)
            unit,unit_id=_unit(calibrations,day,item)
            config=json.loads(setting.value_json) if setting else {}
            if (not setting or config.get('independent_contribution') is not True or config.get('hours_label')!=item.label or
                    unit not in ('s','h') or unit!=config.get('time_unit') or not _valid_number(item)):
                lamp_complete=False; quality['unverified_lamp_groups']+=1; continue
            group=dict(id=f'{number}:{channel}',source_validated=True,ppfd_umol_m2_s=config.get('ppfd'),
                       hours=item.value/3600 if unit=='s' else item.value,dim_fraction=config.get('dim_factor'))
            try:
                checked=lamp_dli([group],configuration_validated=True)
                if checked['mol_m2_day'] is None:
                    raise ValueError('Lampbijdrage niet gevalideerd')
            except (ValueError,TypeError):
                lamp_complete=False; quality['invalid_lamp_groups']+=1; continue
            groups.append(group); group_modules.add(number); mark(calibration_ids=[setting.id,unit_id])
        supplemental_sum=0.0
        supplemental_used=[]
        for module in sorted(expected_modules-primary_modules):
            extra=supplement['days'].get(day.isoformat(),{}).get(module)
            if extra and extra['quality']=='verified_supplemental' and extra['mol_m2_day'] is not None:
                supplemental_sum+=extra['mol_m2_day']; group_modules.add(module)
                used_calibrations.update(extra['calibration_ids'])
                supplemental_used.append(dict(date=day.isoformat(),module=module,**extra['source_ids']))
        if group_modules!=expected_modules:
            lamp_complete=False
        if lamp_complete:
            answer=lamp_dli(groups,configuration_validated=True) if groups else {'mol_m2_day':0.0}
            value=answer['mol_m2_day']+supplemental_sum
            if math.isfinite(value):
                daily[day]['lamp_light']=dict(value=value,seconds=seconds)
                result['supplemental_lighting']['used_days'].extend(supplemental_used)
                if supplemental_used:
                    quality['supplemental_excel_lamp_days']+=1
        else:
            quality['incomplete_lamp_days']+=1
        if include_daylight_estimate:
            radiation=_one_candidate([i for i in items if i.label=='Sum_24h_Radiation' and i.object_type in ('weather','radiation')],quality)
            transmission=_setting(calibrations,'transmission',day,object_type='bay',object_number=cultivation.bay_number)
            config=json.loads(transmission.value_json) if transmission else {}
            if radiation:
                mark([radiation])
                unit,unit_id=_unit(calibrations,day,radiation)
                if transmission and config.get('no_screen_effect') is True and _valid_number(radiation):
                    try:
                        answer=natural_dli(radiation.value,unit=unit,transmission=config.get('factor'))
                    except (ValueError,TypeError):
                        answer={'mol_m2_day':None}
                    if answer['mol_m2_day'] is not None:
                        daily[day]['daylight_estimate']=dict(value=answer['mol_m2_day'],seconds=seconds)
                        mark(calibration_ids=[transmission.id,unit_id])
    used_excel=result['supplemental_lighting']['used_days']
    result['source_ids']['legacy_lighting'] = {plural:sorted({row[single] for row in used_excel})
        for single,plural in (('legacy_lighting_job_id','job_ids'),('legacy_lighting_day_id','day_ids'),
                              ('history_import_id','history_import_ids'),('checksum','source_checksums'))}
    if used_excel:
        result['warnings'].append('supplemental_excel_lighting_used_Priva_always_primary')
    required=[] if profile=='basic' else ['lamp_light']
    if profile=='climate':
        required+=['temperature','rh','water']
        if include_co2:
            required.append('co2')
    if include_daylight_estimate and profile!='basic':
        required.append('daylight_estimate')
    for metric in METRICS:
        number,coverage=_aggregate(days,daily,metric,min_coverage)
        result['coverage'][metric]=coverage
        if metric in required:
            if number is None:
                result['missing'].append(FEATURES[metric])
            else:
                result['features'][FEATURES[metric]]=float(number)
                if coverage['fraction']<1:
                    result['warnings'].append('partial_coverage_'+metric)
    valid_measurements=[]
    for measurement in (m1,m2):
        valid=bool(measurement and measurement.created_at is not None and measurement.measured_at is not None and
                   measurement.cultivation_id==cultivation.id and _utc(measurement.created_at)<=cutoff and
                   _utc(measurement.measured_at)<=observation_cutoff and _utc(measurement.measured_at)>=_midnight(start))
        valid_measurements.append(_utc(measurement.measured_at) if valid else None)
    first,second=valid_measurements
    light_out=_midnight(plan.light_out_date)
    if first is not None and first<light_out:
        first=second=None; result['warnings'].append('measurement_before_light_out')
    if second is not None and (first is None or second<=first):
        second=None; result['warnings'].append('invalid_second_measurement_boundary')
    phase_bounds={
        'LD':(_midnight(start),min(light_out,observation_cutoff)),
        'KD_toM1':(light_out,min(first or observation_cutoff,observation_cutoff)),
        'M1_toM2':(first,min(second or observation_cutoff,observation_cutoff)) if first else (None,None),
        'M2_toasof':(second,observation_cutoff) if second else (None,None)}
    for phase,(begin,end) in phase_bounds.items():
        if begin is not None and end is not None and begin>end:
            begin=end=None
        phase_days=[d for d in days if begin is not None and end is not None and _midnight(d)>=begin and _midnight(d+timedelta(days=1))<=end]
        crossing=[d.isoformat() for d in days if begin is not None and end is not None and _midnight(d)<end and _midnight(d+timedelta(days=1))>begin and d not in phase_days]
        entry=dict(window=dict(start=_iso(begin) if begin else None,end=_iso(end) if end else None),features={},coverage={},boundary_days_excluded=crossing)
        for metric in METRICS:
            number,coverage=_aggregate(phase_days,daily,metric,min_coverage)
            entry['coverage'][metric]=coverage
            if number is not None:
                entry['features'][FEATURES[metric]]=float(number)
        result['phases'][phase]=entry
        if crossing:
            result['warnings'].append('daily_records_cross_phase_boundary_'+phase)
    if observation_cutoff>completed_end:
        result['warnings'].append('current_partial_day_not_observed')
    if include_daylight_estimate:
        result['warnings'].append('daylight_is_estimated_not_verified')
    if result['truncated']:
        result['missing'].append('bounded_read_truncated')
    result['eligible']=not result['missing'] and not result['truncated']
    value_ids=sorted(used_values)
    result['source_ids'].update(priva_source_ids=sorted(used_sources),priva_value_ids=value_ids if len(value_ids)<=2000 else [],
        priva_value_ids_sha256=hashlib.sha256(json.dumps(value_ids,separators=(',',':')).encode()).hexdigest(),
        priva_value_count=len(value_ids),mapping_revision_ids=sorted(used_mappings),calibration_ids=sorted(used_calibrations),
        priva_sources=[dict(id=source_map[i].id,checksum=source_map[i].checksum,version=source_map[i].version,
                          activated_at=_iso(source_map[i].activated_at)) for i in sorted(used_sources)])
    result['quality']=dict(quality)
    return result


@router.get('/api/cultivations/{cultivation_id}/environment')
def get_environment(cultivation_id:int,as_of:datetime|None=None,observed_until:datetime|None=None,
                    profile:Literal['basic','light','climate']|None=None,
                    min_coverage:float|None=Query(default=None,ge=0,le=1),
                    include_daylight_estimate:bool|None=None,include_co2:bool|None=None,
                    db:Session=Depends(get_db),user=Depends(require_user)):
    def request_time(value):
        if value is None:
            return None
        if value.tzinfo is None:
            if value.replace(tzinfo=NL,fold=0).utcoffset()!=value.replace(tzinfo=NL,fold=1).utcoffset():
                raise HTTPException(422,'Gebruik een expliciete tijdzone bij zomer-/wintertijd')
            value=value.replace(tzinfo=NL)
        return _utc(value)
    cutoff=request_time(as_of) if as_of else utcnow()
    observed=request_time(observed_until)
    if cutoff>utcnow():
        raise HTTPException(422,'Omgevingswaarnemingen kunnen niet in de toekomst worden opgevraagd')
    cultivation=db.get(Cultivation,cultivation_id)
    if cultivation is None:
        raise HTTPException(404,'Kapteelt niet gevonden')
    plan=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==cultivation_id,
         PlanningVersion.created_at<=cutoff).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    def measurement(kind):
        return db.scalar(select(Measurement).where(Measurement.cultivation_id==cultivation_id,Measurement.kind==kind,
              Measurement.created_at<=cutoff,Measurement.measured_at<=cutoff).order_by(Measurement.measured_at.desc(),Measurement.id.desc()))
    configured=effective_settings(db,cutoff)
    return environment_snapshot(db,cultivation,plan,cutoff,measurement('KD10'),measurement('KD30'),
        profile=profile if profile is not None else getattr(configured,'model_feature_profile','basic'),
        min_coverage=min_coverage if min_coverage is not None else getattr(configured,'environment_min_coverage',1.0),
        include_daylight_estimate=include_daylight_estimate if include_daylight_estimate is not None else getattr(configured,'environment_include_daylight_estimate',False),
        include_co2=include_co2 if include_co2 is not None else getattr(configured,'environment_include_co2',False),
        observed_until=observed)
