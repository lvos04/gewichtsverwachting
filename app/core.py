import hashlib, json, statistics, math
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationError
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, Variety, Bay, Cultivation, CultivationRevision, PlanningVersion, Measurement, Stem, Harvest, Event, MappingVersion, AuditLog
from .auth import require_user, require_write, require_admin, audit, hash_password, public_user
router=APIRouter()
NL=ZoneInfo('Europe/Amsterdam')
def contains_nonfinite(value):
    if isinstance(value,float): return not math.isfinite(value)
    if isinstance(value,dict): return any(contains_nonfinite(v) for v in value.values())
    if isinstance(value,(list,tuple)): return any(contains_nonfinite(v) for v in value)
    return False
class CoreSchema(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)
    @model_validator(mode='before')
    @classmethod
    def reject_nonfinite(cls, value):
        if contains_nonfinite(value): raise HTTPException(422,'Getallen moeten eindig zijn')
        return value
def to_utc(d):
    return (d.replace(tzinfo=NL) if d.tzinfo is None else d).astimezone(timezone.utc).replace(tzinfo=None)
def obj(x):
    return {c.name:getattr(x,c.name) for c in x.__table__.columns if c.name not in ('password_hash','payload_hash')}
def cultivation_revision(db, c):
    db.add(CultivationRevision(cultivation_id=c.id,category=c.category,label_quality=c.label_quality,status=c.status))
def get_or_404(db, cls, id):
    value=db.get(cls,id)
    if value is None: raise HTTPException(404,'Niet gevonden')
    return value
def seed_data(db):
    for n in range(1,41):
        dept=1 if n<=9 else 2 if n<=18 else 3 if n<=30 else 4
        if not db.get(Bay,n): db.add(Bay(number=n,department=dept,beds=6))
    for name in ('antonov','pjotr','tutu'):
        if not db.scalar(select(Variety).where(Variety.name==name)): db.add(Variety(name=name,target_weight_g=105))
    db.flush()
    for n in range(1,41):
        dept=1 if n<=9 else 2 if n<=18 else 3 if n<=30 else 4
        for typ,num in (('department',dept),('valve',n),('light',n),('light',100+n)):
            if not db.scalar(select(MappingVersion.id).where(MappingVersion.bay_number==n,MappingVersion.object_type==typ,MappingVersion.object_number==num)):
                db.add(MappingVersion(bay_number=n,object_type=typ,object_number=num,valid_from=date(2026,11,16),reason='Bevestigde huidige koppeling'))
    db.commit()
class VarietyIn(CoreSchema):
    name:str=Field(min_length=1,max_length=100)
    target_weight_g:float=Field(default=105,gt=0,le=1000)
class CultivationIn(CoreSchema):
    round_name:str=Field(min_length=1,max_length=100)
    bay_number:int=Field(ge=1,le=40)
    variety_id:int
    beds:int=Field(ge=1,le=6)
    category:str='standard'
    status:str='planned'
    label_quality:str='verified'
    @model_validator(mode='after')
    def validate_values(self):
        if self.category not in ('standard','adjusted','control') or self.status not in ('planned','active','harvested','historical') or self.label_quality not in ('verified','historical_assigned','ambiguous','unknown'): raise ValueError('Ongeldige status, categorie of labelkwaliteit')
        if not self.round_name.strip(): raise ValueError('Ronde is verplicht')
        return self
class PlanningIn(CoreSchema):
    plant_date:date|None=None
    light_out_date:date|None=None
    expected_harvest_date:date|None=None
    density:float|None=Field(default=None,gt=0)
    reason:str|None=None
    @model_validator(mode='after')
    def order(self):
        if self.plant_date and self.light_out_date and self.light_out_date<self.plant_date: raise ValueError('Licht uit ligt vóór planten')
        if self.light_out_date and self.expected_harvest_date and self.expected_harvest_date<self.light_out_date: raise ValueError('Oogst ligt vóór licht uit')
        return self
class StemIn(CoreSchema):
    weight_g:float=Field(gt=0,le=2000)
    length_cm:float=Field(gt=0,le=500)
    position:str=Field(min_length=1,max_length=100)
    excluded:bool=False
    exclusion_reason:str|None=None
    @model_validator(mode='after')
    def reason_required(self):
        if self.excluded and not (self.exclusion_reason or '').strip(): raise ValueError('Reden voor uitsluiting is verplicht')
        return self
class MeasurementIn(CoreSchema):
    client_id:UUID
    cultivation_id:int
    kind:str
    measured_at:datetime
    location:str|None=None
    notes:str|None=None
    stems:list[StemIn]=Field(min_length=5,max_length=5)
    @model_validator(mode='after')
    def valid(self):
        if self.kind not in ('KD10','KD30','pilot'): raise ValueError('Onbekend meettype')
        if len({s.position.strip().casefold() for s in self.stems})!=5: raise ValueError('Geef vijf verschillende meetplekken op')
        return self
class HarvestIn(CoreSchema):
    start_date:date|None=None
    end_date:date|None=None
    weight_g:float|None=Field(default=None,gt=0,le=2000)
    stem_count:int|None=Field(default=None,ge=0)
    loss_fraction:float|None=Field(default=None,ge=0,le=1)
    quality_classes:list[dict]|None=None
    notes:str|None=None
    @model_validator(mode='after')
    def valid(self):
        if self.start_date and self.end_date and self.end_date<self.start_date: raise ValueError('Einddatum ligt vóór begindatum')
        if self.quality_classes and len(self.quality_classes)>1 and self.weight_g is not None: raise ValueError('Bewaar meerdere gewichtsklassen afzonderlijk, zonder gemiddeld eindgewicht')
        return self
class EventIn(CoreSchema):
    occurred_at:datetime
    kind:str=Field(min_length=1,max_length=100)
    amount:float|None=Field(default=None,allow_inf_nan=False)
    unit:str|None=Field(default=None,max_length=50)
    scope:str|None=Field(default=None,max_length=100)
    reason:str|None=Field(default=None,max_length=2000)
    @model_validator(mode='after')
    def valid(self):
        if not self.kind.strip(): raise ValueError('Gebeurtenis is verplicht')
        if self.amount is not None and not (self.unit or '').strip(): raise ValueError('Eenheid is verplicht bij hoeveelheid')
        return self
class MappingIn(CoreSchema):
    bay_number:int=Field(ge=1,le=40)
    object_type:str
    object_number:int=Field(ge=1)
    valid_from:date
    valid_until:date|None=None
    reason:str=Field(min_length=1)
    replaces_id:int|None=None
    @model_validator(mode='after')
    def valid(self):
        if self.object_type not in ('department','valve','light') or self.valid_until and self.valid_until<self.valid_from: raise ValueError('Ongeldige koppeling of periode')
        if self.object_type=='department' and self.object_number not in (1,2,3,4): raise ValueError('Afdeling moet 1 t/m 4 zijn')
        return self
class UserIn(CoreSchema):
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=12,max_length=256)
    role:str
    @model_validator(mode='after')
    def valid(self):
        if self.role not in ('admin','medewerker','lezer') or not self.username.strip(): raise ValueError('Ongeldige rol of naam')
        return self
@router.get('/api/dashboard')
def dashboard(db:Session=Depends(get_db), user:User=Depends(require_user)):
    from .settings import effective_settings
    settings=effective_settings(db)
    cs=db.scalars(select(Cultivation)).all()
    active=[c for c in cs if c.status in ('active','planned')]
    plans=db.scalars(select(PlanningVersion).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc())).all()
    latest={}
    for p in plans: latest.setdefault(p.cultivation_id,p)
    measurements=db.scalars(select(Measurement).order_by(Measurement.measured_at.desc())).all()
    by_cultivation={}
    for m in measurements: by_cultivation.setdefault(m.cultivation_id,[]).append(m)
    varieties={v.id:v.name for v in db.scalars(select(Variety))}
    today=datetime.now(NL).date()
    week_start=today-timedelta(days=today.weekday())
    measured_species=set()
    for c in active:
        if any(m.kind=='KD10' and week_start<=m.measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date()<=today for m in by_cultivation.get(c.id,[])):
            measured_species.add(c.variety_id)
    candidates=[]
    for c in active:
        plan=latest.get(c.id)
        if not plan or not plan.light_out_date: continue
        kd=(today-plan.light_out_date).days
        items=by_cultivation.get(c.id,[])
        kinds={m.kind for m in items}
        def task(kind, target_day, due):
            return {'cultivation_id':c.id,'bay_number':c.bay_number,'round_name':c.round_name,'variety_id':c.variety_id,'variety':varieties.get(c.variety_id),'kind':kind,'due_date':due.isoformat(),'actual_kd_day':kd,'days_until':(due-today).days,'status':'overdue' if due<today else 'due' if due==today else 'upcoming','steering_until_kd':settings.steering_until_kd,'steering_window_open':0<=kd<=settings.steering_until_kd}
        if 'KD10' not in kinds and c.variety_id not in measured_species and kd>=3:
            candidates.append(task('KD10',10,plan.light_out_date+timedelta(days=10)))
        if 'KD10' in kinds and 'KD30' not in kinds and kd>=23:
            candidates.append(task('KD30',30,plan.light_out_date+timedelta(days=30)))
        pilots=[m for m in items if m.kind=='pilot']
        if pilots and kd>=7:
            due=pilots[0].measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date()+timedelta(days=7)
            if due<=today+timedelta(days=7): candidates.append(task('pilot',None,due))
    candidates.sort(key=lambda x:(x['days_until'],x['bay_number']))
    # KD10 is a suggestion for one new bay per variety in the current week.
    selected=[]; species_seen=set()
    for item in candidates:
        if item['kind']=='KD10':
            if item['variety_id'] in species_seen: continue
            species_seen.add(item['variety_id'])
        selected.append(item)
    return {'active_cultivations':len(active),'rounds':sorted({c.round_name for c in active}),'bays':40,'measurements':len(measurements),'steering_until_kd':settings.steering_until_kd,'tasks':selected}
@router.get('/api/bays')
def bays(db:Session=Depends(get_db),user:User=Depends(require_user)):
    cs=db.scalars(select(Cultivation)).all()
    today=datetime.now(NL).date()
    current=db.scalars(select(MappingVersion).where(MappingVersion.object_type=='department',MappingVersion.valid_from<=today).order_by(MappingVersion.valid_from.desc(),MappingVersion.id.desc())).all()
    departments={}
    for m in current:
        if (m.valid_until is None or m.valid_until>=today) and m.bay_number not in departments: departments[m.bay_number]=m.object_number
    return [{**obj(b),'department':departments.get(b.number),'cultivations':[obj(c) for c in cs if c.bay_number==b.number and c.status in ('planned','active')]} for b in db.scalars(select(Bay).order_by(Bay.number)).all()]
@router.get('/api/varieties')
def varieties(db:Session=Depends(get_db),user:User=Depends(require_user)): return [obj(v) for v in db.scalars(select(Variety).order_by(Variety.name))]
@router.post('/api/varieties')
def add_variety(data:VarietyIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    name=data.name.strip().lower()
    if not name or db.scalar(select(Variety).where(func.lower(Variety.name)==name)): raise HTTPException(409,'Soort bestaat al of naam is leeg')
    v=Variety(name=name,target_weight_g=data.target_weight_g); db.add(v); db.flush(); audit(db,user,'create','variety',{'id':v.id}); db.commit(); return obj(v)
@router.get('/api/cultivations')
def cultivations(db:Session=Depends(get_db),user:User=Depends(require_user)): return [obj(c) for c in db.scalars(select(Cultivation).order_by(Cultivation.id.desc()))]
@router.post('/api/cultivations')
def add_cultivation(data:CultivationIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    get_or_404(db,Bay,data.bay_number); get_or_404(db,Variety,data.variety_id)
    if data.status in ('planned','active'):
        used=db.scalar(select(func.coalesce(func.sum(Cultivation.beds),0)).where(Cultivation.bay_number==data.bay_number,Cultivation.round_name==data.round_name.strip(),Cultivation.status.in_(['planned','active'])))
        if used+data.beds>6: raise HTTPException(409,'Meer dan zes bedden in deze kap en ronde')
    c=Cultivation(**{**data.model_dump(),'round_name':data.round_name.strip()}); db.add(c); db.flush(); cultivation_revision(db,c); audit(db,user,'create','cultivation',{'id':c.id}); db.commit(); return obj(c)
def measurement_obj(db,m):
    stems=db.scalars(select(Stem).where(Stem.measurement_id==m.id).order_by(Stem.id)).all()
    included=[s for s in stems if not s.excluded]
    plan=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==m.cultivation_id).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    kd=(m.measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date()-plan.light_out_date).days if plan and plan.light_out_date else None
    from .settings import effective_settings
    tolerance=effective_settings(db).measurement_tolerance_days
    return {**obj(m),'actual_kd_day':kd,'stems':[obj(s) for s in stems],'mean_weight_g':statistics.mean(s.weight_g for s in included) if included else None,'mean_length_cm':statistics.mean(s.length_cm for s in included) if included else None,'weight_sd_g':statistics.stdev(s.weight_g for s in included) if len(included)>1 else None,'timing_warning':kd is not None and m.kind in ('KD10','KD30') and abs(kd-int(m.kind[2:]))>tolerance,'timing_tolerance_days':tolerance}
@router.get('/api/cultivations/{id}')
def cultivation_detail(id:int,db:Session=Depends(get_db),user:User=Depends(require_user)):
    c=get_or_404(db,Cultivation,id)
    def rows(cls): return db.scalars(select(cls).where(cls.cultivation_id==id).order_by(cls.id)).all()
    return {**obj(c),'planning':[obj(x) for x in rows(PlanningVersion)],'revisions':[obj(x) for x in rows(CultivationRevision)],'measurements':[measurement_obj(db,x) for x in rows(Measurement)],'harvests':[obj(x) for x in rows(Harvest)],'events':[obj(x) for x in rows(Event)]}
@router.post('/api/cultivations/{id}/planning')
def add_planning(id:int,data:PlanningIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    get_or_404(db,Cultivation,id)
    previous=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==id).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    if previous and not (data.reason or '').strip(): raise HTTPException(422,'Reden voor wijziging is verplicht')
    values=data.model_dump()
    if previous:
        for name in ('plant_date','light_out_date','expected_harvest_date','density'):
            if name not in data.model_fields_set: values[name]=getattr(previous,name)
    # Revalidate after filling omitted fields; explicit null still clears a value.
    try: values=PlanningIn.model_validate(values).model_dump()
    except ValidationError: raise HTTPException(422,'Ongeldige volgorde van planningsdatums')
    p=PlanningVersion(cultivation_id=id,author_id=user.id,**values); db.add(p); db.flush(); audit(db,user,'create','planning',{'id':p.id,'cultivation_id':id}); db.commit(); return obj(p)
@router.post('/api/measurements')
def add_measurement(data:MeasurementIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    canonical=data.model_dump(mode='json'); canonical['measured_at']=to_utc(data.measured_at).isoformat(); digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    existing=db.scalar(select(Measurement).where(Measurement.client_id==str(data.client_id)))
    if existing:
        if existing.payload_hash!=digest: raise HTTPException(409,'Meet-ID bestaat met andere gegevens')
        return measurement_obj(db,existing)
    get_or_404(db,Cultivation,data.cultivation_id)
    p=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==data.cultivation_id).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    kd=(data.measured_at.astimezone(NL).date() if data.measured_at.tzinfo else data.measured_at.date())-p.light_out_date if p and p.light_out_date else None
    m=Measurement(client_id=str(data.client_id),cultivation_id=data.cultivation_id,kind=data.kind,measured_at=to_utc(data.measured_at),actual_kd_day=kd.days if kd is not None else None,location=data.location,notes=data.notes,user_id=user.id,payload_hash=digest)
    db.add(m)
    try:
        db.flush(); db.add_all([Stem(measurement_id=m.id,**s.model_dump()) for s in data.stems]); audit(db,user,'create','measurement',{'id':m.id,'cultivation_id':m.cultivation_id}); db.commit()
    except IntegrityError:
        db.rollback(); existing=db.scalar(select(Measurement).where(Measurement.client_id==str(data.client_id)))
        if existing and existing.payload_hash==digest: return measurement_obj(db,existing)
        raise HTTPException(409,'Meet-ID bestaat met andere gegevens')
    return measurement_obj(db,m)
@router.post('/api/cultivations/{id}/harvest')
def add_harvest(id:int,data:HarvestIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    c=get_or_404(db,Cultivation,id); h=Harvest(cultivation_id=id,**data.model_dump()); db.add(h); c.status='harvested'; db.flush(); cultivation_revision(db,c); audit(db,user,'create','harvest',{'id':h.id,'cultivation_id':id}); db.commit(); return obj(h)
@router.post('/api/cultivations/{id}/events')
def add_event(id:int,data:EventIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    get_or_404(db,Cultivation,id); e=Event(cultivation_id=id,**{**data.model_dump(),'occurred_at':to_utc(data.occurred_at)}); db.add(e); db.flush(); audit(db,user,'create','event',{'id':e.id,'cultivation_id':id}); db.commit(); return obj(e)
@router.get('/api/mappings')
def mappings(db:Session=Depends(get_db),user:User=Depends(require_user)): return [obj(x) for x in db.scalars(select(MappingVersion).order_by(MappingVersion.bay_number,MappingVersion.id))]
@router.post('/api/mappings')
def add_mapping(data:MappingIn,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    get_or_404(db,Bay,data.bay_number)
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if data.replaces_id:
        previous=get_or_404(db,MappingVersion,data.replaces_id)
        if previous.bay_number!=data.bay_number or previous.object_type!=data.object_type or previous.valid_from>=data.valid_from or (previous.valid_until and previous.valid_until<data.valid_from): raise HTTPException(422,'Ongeldige eerdere koppeling')
        previous.valid_until=data.valid_from-timedelta(days=1)
        db.flush()
    for other in db.scalars(select(MappingVersion).where(MappingVersion.object_type==data.object_type)):
        overlap=(other.valid_until is None or other.valid_until>=data.valid_from) and (data.valid_until is None or other.valid_from<=data.valid_until)
        if overlap and ((other.bay_number==data.bay_number and (data.object_type!='light' or other.object_number==data.object_number)) or (data.object_type in ('valve','light') and other.object_number==data.object_number)):
            raise HTTPException(409,'Koppeling overlapt een bestaande periode; geef replaces_id op')
    values=data.model_dump(exclude={'replaces_id'})
    m=MappingVersion(**values); db.add(m); db.flush(); audit(db,user,'create','mapping',{'id':m.id,'replaces_id':data.replaces_id}); db.commit(); return obj(m)
@router.get('/api/users')
def users(db:Session=Depends(get_db),user:User=Depends(require_admin)): return [public_user(x)|{'active':x.active} for x in db.scalars(select(User).order_by(User.id))]
@router.post('/api/users')
def add_user(data:UserIn,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    if db.scalar(select(User).where(func.lower(User.username)==data.username.strip().lower())): raise HTTPException(409,'Gebruiker bestaat al')
    u=User(username=data.username.strip(),password_hash=hash_password(data.password),role=data.role); db.add(u); db.flush(); audit(db,user,'create','user',{'id':u.id,'role':u.role}); db.commit(); return public_user(u)
@router.get('/api/audit')
def audits(db:Session=Depends(get_db),user:User=Depends(require_admin)): return [obj(x) for x in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1000))]
class UserUpdate(CoreSchema):
    role:str|None=None
    active:bool|None=None
    password:str|None=Field(default=None,min_length=12,max_length=256)
    @model_validator(mode='after')
    def valid(self):
        if self.role is not None and self.role not in ('admin','medewerker','lezer'): raise ValueError('Ongeldige rol')
        return self
@router.patch('/api/users/{id}')
def update_user(id:int,data:UserUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    target=get_or_404(db,User,id)
    if target.role=='admin' and target.active and (data.role not in (None,'admin') or data.active is False):
        admins=db.scalar(select(func.count()).select_from(User).where(User.role=='admin',User.active.is_(True)))
        if admins<=1: raise HTTPException(409,'De laatste actieve beheerder kan niet worden verwijderd')
    if data.role is not None: target.role=data.role
    if data.active is not None: target.active=data.active
    if data.password is not None: target.password_hash=hash_password(data.password)
    if data.active is False or data.password is not None:
        from sqlalchemy import delete
        from .models import AuthSession
        db.execute(delete(AuthSession).where(AuthSession.user_id==id))
    audit(db,user,'update','user',{'id':id,'role':target.role,'active':target.active,'password_changed':data.password is not None}); db.commit()
    return public_user(target)|{'active':target.active}
class VarietyUpdate(CoreSchema):
    target_weight_g:float=Field(gt=0,le=1000)
@router.patch('/api/varieties/{id}')
def update_variety(id:int,data:VarietyUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    v=get_or_404(db,Variety,id); old=v.target_weight_g; v.target_weight_g=data.target_weight_g
    audit(db,user,'update','variety',{'id':id,'old_target_weight_g':old,'target_weight_g':v.target_weight_g}); db.commit(); return obj(v)
class BayUpdate(CoreSchema):
    area_m2:float|None=Field(default=None,gt=0)
@router.patch('/api/bays/{number}')
def update_bay(number:int,data:BayUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    bay=get_or_404(db,Bay,number); old=bay.area_m2; bay.area_m2=data.area_m2
    audit(db,user,'update','bay',{'number':number,'old_area_m2':old,'area_m2':bay.area_m2}); db.commit(); return obj(bay)
class CultivationUpdate(CoreSchema):
    category:str|None=None
    status:str|None=None
    label_quality:str|None=None
    @model_validator(mode='after')
    def valid(self):
        if self.category is not None and self.category not in ('standard','adjusted','control'): raise ValueError('Ongeldige categorie')
        if self.status is not None and self.status not in ('planned','active','harvested','historical'): raise ValueError('Ongeldige status')
        if self.label_quality is not None and self.label_quality not in ('verified','historical_assigned','ambiguous','unknown'): raise ValueError('Ongeldige labelkwaliteit')
        return self
@router.patch('/api/cultivations/{id}')
def update_cultivation(id:int,data:CultivationUpdate,db:Session=Depends(get_db),user:User=Depends(require_write)):
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    c=get_or_404(db,Cultivation,id)
    if data.status in ('planned','active') and c.status not in ('planned','active'):
        used=db.scalar(select(func.coalesce(func.sum(Cultivation.beds),0)).where(Cultivation.bay_number==c.bay_number,Cultivation.round_name==c.round_name,Cultivation.status.in_(['planned','active']),Cultivation.id!=id))
        if used+c.beds>6: raise HTTPException(409,'Meer dan zes bedden in deze kap en ronde')
    if data.status=='harvested' and not db.scalar(select(Harvest.id).where(Harvest.cultivation_id==id)):
        raise HTTPException(422,'Registreer eerst de oogst')
    before={'category':c.category,'status':c.status,'label_quality':c.label_quality}
    for field in before:
        value=getattr(data,field)
        if value is not None: setattr(c,field,value)
    cultivation_revision(db,c)
    audit(db,user,'update','cultivation',{'id':id,'before':before,'after':{'category':c.category,'status':c.status,'label_quality':c.label_quality}})
    db.commit(); return obj(c)
