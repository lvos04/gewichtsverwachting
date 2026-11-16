"""Season-specific user-supplied growth targets with point-in-time provenance."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import audit, require_user, require_admin
from .db import get_db
from .models import User, Variety, Cultivation, PlanningVersion, Measurement, Stem, CropProfileVersion
from .core import obj, to_utc, contains_nonfinite
import statistics

router=APIRouter()
NL=ZoneInfo('Europe/Amsterdam')
class ProfileSchema(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False,extra='forbid')
    @model_validator(mode='before')
    @classmethod
    def finite(cls,value):
        if contains_nonfinite(value): raise HTTPException(422,'Getallen moeten eindig zijn')
        return value
class CurvePoint(ProfileSchema):
    kd_day:int=Field(ge=0,le=500)
    length_cm:float=Field(gt=0,le=500)
class ProfileIn(ProfileSchema):
    variety_id:int
    season_start:str=Field(pattern=r'^\d{2}-\d{2}$')
    season_end:str=Field(pattern=r'^\d{2}-\d{2}$')
    valid_from:date
    valid_until:date|None=None
    target_weight_g:float|None=Field(default=None,gt=0,le=1000)
    length_curve:list[CurvePoint]=Field(default_factory=list,max_length=100)
    interlight_description:str|None=Field(default=None,max_length=2000)
    source:str=Field(min_length=1,max_length=2000)
    supersedes_id:int|None=None
    @model_validator(mode='after')
    def validate_profile(self):
        try:
            date.fromisoformat('2000-'+self.season_start); date.fromisoformat('2000-'+self.season_end)
        except ValueError: raise ValueError('Ongeldige maand en dag')
        if self.valid_until and self.valid_until<self.valid_from: raise ValueError('Einddatum ligt vóór ingangsdatum')
        if len({point.kd_day for point in self.length_curve})!=len(self.length_curve): raise ValueError('Elke KD-dag mag één keer voorkomen')
        if not self.length_curve and self.target_weight_g is None and not (self.interlight_description or '').strip(): raise ValueError('Geef een lengtecurve, doelgewicht of tussenlichtbeschrijving op')
        if not self.source.strip(): raise ValueError('Bron is verplicht')
        return self

def in_season(day, start, end):
    md=day.strftime('%m-%d')
    return start<=md<=end if start<=end else md>=start or md<=end

def season_days(start,end):
    from datetime import timedelta
    first=date(2000,1,1)
    return {i for i in range(366) if in_season(first+timedelta(days=i),start,end)}

def interpolate(curve, kd_day):
    if kd_day is None or not curve: return None
    points=sorted(curve,key=lambda p:p['kd_day'])
    if kd_day<points[0]['kd_day'] or kd_day>points[-1]['kd_day']: return None
    for point in points:
        if kd_day==point['kd_day']: return point['length_cm']
    for left,right in zip(points,points[1:]):
        if left['kd_day']<kd_day<right['kd_day']:
            return left['length_cm']+(right['length_cm']-left['length_cm'])*(kd_day-left['kd_day'])/(right['kd_day']-left['kd_day'])
    return None

def select_profile(db,variety_id,plant_date,as_of):
    if not plant_date: return None,'plant_date_unknown'
    day=as_of.replace(tzinfo=timezone.utc).astimezone(NL).date()
    candidates=db.scalars(select(CropProfileVersion).where(CropProfileVersion.variety_id==variety_id,CropProfileVersion.created_at<=as_of,CropProfileVersion.valid_from<=day)).all()
    candidates=[p for p in candidates if (p.valid_until is None or p.valid_until>=day) and in_season(plant_date,p.season_start,p.season_end)]
    all_known=db.scalars(select(CropProfileVersion).where(CropProfileVersion.variety_id==variety_id,CropProfileVersion.created_at<=as_of)).all()
    by_id={p.id:p for p in all_known}
    superseded=set()
    for candidate in candidates:
        ancestor=candidate.supersedes_id
        while ancestor is not None and ancestor not in superseded:
            superseded.add(ancestor)
            ancestor=by_id[ancestor].supersedes_id if ancestor in by_id else None
    candidates=[p for p in candidates if p.id not in superseded]
    if len(candidates)>1: return None,'ambiguous'
    return (candidates[0],None) if candidates else (None,'no_profile')

@router.get('/api/crop-profiles')
def profiles(db:Session=Depends(get_db),user:User=Depends(require_user)):
    return [obj(p) for p in db.scalars(select(CropProfileVersion).order_by(CropProfileVersion.id.desc()))]
@router.post('/api/crop-profiles')
def create_profile(data:ProfileIn,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if not db.get(Variety,data.variety_id): raise HTTPException(404,'Soort niet gevonden')
    existing=db.scalars(select(CropProfileVersion).where(CropProfileVersion.variety_id==data.variety_id)).all()
    by_id={p.id:p for p in existing}; ancestors=set(); cursor=data.supersedes_id
    while cursor is not None:
        if cursor in ancestors or cursor not in by_id: raise HTTPException(422,'Te vervangen profiel bestaat niet bij deze soort')
        ancestors.add(cursor); cursor=by_id[cursor].supersedes_id
    season=season_days(data.season_start,data.season_end)
    for p in existing:
        overlaps_dates=(p.valid_until is None or p.valid_until>=data.valid_from) and (data.valid_until is None or p.valid_from<=data.valid_until)
        if p.id not in ancestors and overlaps_dates and season.intersection(season_days(p.season_start,p.season_end)):
            raise HTTPException(409,'Profielen overlappen; vervang het bestaande profiel of kies een andere periode')
    values=data.model_dump(); values['length_curve']=sorted(values['length_curve'],key=lambda p:p['kd_day']); values['source']=data.source.strip()
    p=CropProfileVersion(**values,author_id=user.id); db.add(p); db.flush(); audit(db,user,'create','crop_profile',{'id':p.id,'supersedes_id':p.supersedes_id}); db.commit()
    return obj(p)

@router.get('/api/cultivations/{id}/growth-target')
def growth_target(id:int,as_of:datetime|None=None,db:Session=Depends(get_db),user:User=Depends(require_user)):
    c=db.get(Cultivation,id)
    if not c: raise HTTPException(404,'Kapteelt niet gevonden')
    cutoff=to_utc(as_of) if as_of else datetime.now(timezone.utc).replace(tzinfo=None)
    plan=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==id,PlanningVersion.created_at<=cutoff).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    profile,reason=select_profile(db,c.variety_id,plan.plant_date if plan else None,cutoff)
    day=cutoff.replace(tzinfo=timezone.utc).astimezone(NL).date()
    actual_kd=(day-plan.light_out_date).days if plan and plan.light_out_date else None
    curve=profile.length_curve if profile else []
    rows=db.scalars(select(Measurement).where(Measurement.cultivation_id==id,Measurement.created_at<=cutoff,Measurement.measured_at<=cutoff).order_by(Measurement.measured_at)).all()
    comparisons=[]
    for measurement in rows:
        kd=(measurement.measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date()-plan.light_out_date).days if plan and plan.light_out_date else None
        stems=db.scalars(select(Stem).where(Stem.measurement_id==measurement.id,Stem.excluded.is_(False))).all()
        mean=statistics.mean(s.length_cm for s in stems) if stems else None
        target=interpolate(curve,kd)
        comparisons.append({'measurement_id':measurement.id,'kind':measurement.kind,'actual_kd_day':kd,'mean_length_cm':mean,'target_length_cm':target,'difference_cm':mean-target if mean is not None and target is not None else None,'included_stems':len(stems)})
    return {'as_of':cutoff.isoformat()+'Z','planning_version_id':plan.id if plan else None,'profile':obj(profile) if profile else None,'reason':reason,'actual_kd_day':actual_kd,'target_length_cm':interpolate(curve,actual_kd),'target_kd10_cm':interpolate(curve,10),'target_kd30_cm':interpolate(curve,30),'comparisons':comparisons,'notice':'Streeflengte is interpolatie van de opgegeven curve, zonder extrapolatie of genormaliseerde meting; onzekerheid is niet vastgesteld.'}
