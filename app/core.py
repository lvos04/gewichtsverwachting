import hashlib, json, statistics
from datetime import date, datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, Variety, VarietyTargetVersion, Bay, Cultivation, CultivationRevision, PlanningVersion, Measurement, Stem, Harvest, Event, MappingVersion, MappingRevision, AuditLog
from .auth import require_user, require_write, require_admin, audit, hash_password, public_user
# Invoerschema's staan in core_schemas; hier opnieuw beschikbaar voor bestaande imports.
from .core_schemas import (NL, contains_nonfinite, CoreSchema, VarietyIn, CultivationIn, PlanningIn, StemIn,  # noqa: F401
                           MeasurementIn, HarvestIn, HarvestCorrection, EventIn, MappingIn, UserIn, UserUpdate, VarietyUpdate,
                           BayUpdate, CultivationUpdate)
router=APIRouter()
def to_utc(d):
    return (d.replace(tzinfo=NL) if d.tzinfo is None else d).astimezone(timezone.utc).replace(tzinfo=None)
def obj(x):
    return {c.name:getattr(x,c.name) for c in x.__table__.columns if c.name not in ('password_hash','payload_hash')}
def mapping_revision(db, mapping):
    db.add(MappingRevision(mapping_id=mapping.id,bay_number=mapping.bay_number,object_type=mapping.object_type,object_number=mapping.object_number,valid_from=mapping.valid_from,valid_until=mapping.valid_until,reason=mapping.reason))
def target_at(db, variety_id, as_of):
    if as_of.tzinfo is not None: as_of=as_of.astimezone(timezone.utc).replace(tzinfo=None)
    return db.scalar(select(VarietyTargetVersion).where(VarietyTargetVersion.variety_id==variety_id,VarietyTargetVersion.created_at<=as_of).order_by(VarietyTargetVersion.created_at.desc(),VarietyTargetVersion.id.desc()))
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
    new_varieties=[]
    for name in ('antonov','pjotr','tutu'):
        if not db.scalar(select(Variety).where(Variety.name==name)):
            v=Variety(name=name,target_weight_g=105); db.add(v); new_varieties.append(v)
    db.flush()
    for v in new_varieties: db.add(VarietyTargetVersion(variety_id=v.id,target_weight_g=v.target_weight_g,author_id=None))
    new_mappings=[]
    for n in range(1,41):
        dept=1 if n<=9 else 2 if n<=18 else 3 if n<=30 else 4
        for typ,num in (('department',dept),('valve',n),('light',n),('light',100+n)):
            if not db.scalar(select(MappingVersion.id).where(MappingVersion.bay_number==n,MappingVersion.object_type==typ,MappingVersion.object_number==num)):
                m=MappingVersion(bay_number=n,object_type=typ,object_number=num,valid_from=date(2026,11,16),reason='Bevestigde huidige koppeling'); db.add(m); new_mappings.append(m)
    db.flush()
    for m in new_mappings: mapping_revision(db,m)
    db.commit()
@router.get('/api/dashboard')
def dashboard(db:Session=Depends(get_db), user:User=Depends(require_user)):
    return dashboard_data(db)
def latest_plans(db:Session, cultivation_ids=None)->dict:
    """Nieuwste planningsversie per teelt, in SQL bepaald (created_at, id aflopend)."""
    ranked=select(PlanningVersion.id,func.row_number().over(partition_by=PlanningVersion.cultivation_id,
        order_by=(PlanningVersion.created_at.desc(),PlanningVersion.id.desc())).label('rank'))
    if cultivation_ids is not None:
        if not cultivation_ids: return {}
        ranked=ranked.where(PlanningVersion.cultivation_id.in_(cultivation_ids))
    ranked=ranked.subquery()
    return {p.cultivation_id:p for p in db.scalars(select(PlanningVersion).join(ranked,ranked.c.id==PlanningVersion.id).where(ranked.c.rank==1))}
def _local_day_start_utc(day):
    return datetime.combine(day,datetime.min.time(),NL).astimezone(timezone.utc).replace(tzinfo=None)
def dashboard_data(db:Session):
    """Shared by the web dashboard and the read-only company link API.

    Filteren en tellen gebeurt in SQL; alleen open teelten en hun samenvattingen
    worden geladen in plaats van alle metingen en planningsversies.
    """
    from .settings import effective_settings
    settings=effective_settings(db)
    active=db.scalars(select(Cultivation).where(Cultivation.status.in_(['active','planned']))).all()
    ids=[c.id for c in active]
    latest=latest_plans(db,ids)
    measurement_count=db.scalar(select(func.count()).select_from(Measurement))
    kinds_by_cultivation={}; last_pilot={}
    if ids:
        for cid,kind,last in db.execute(select(Measurement.cultivation_id,Measurement.kind,func.max(Measurement.measured_at)).where(Measurement.cultivation_id.in_(ids)).group_by(Measurement.cultivation_id,Measurement.kind)):
            kinds_by_cultivation.setdefault(cid,set()).add(kind)
            if kind=='pilot': last_pilot[cid]=last
    varieties={v.id:v.name for v in db.scalars(select(Variety))}
    today=datetime.now(NL).date()
    week_start=today-timedelta(days=today.weekday())
    measured_species=set(db.scalars(select(Cultivation.variety_id).distinct().join(Measurement,Measurement.cultivation_id==Cultivation.id).where(
        Cultivation.status.in_(['active','planned']),Measurement.kind=='KD10',
        Measurement.measured_at>=_local_day_start_utc(week_start),Measurement.measured_at<_local_day_start_utc(today+timedelta(days=1))))) if ids else set()
    candidates=[]
    for c in active:
        plan=latest.get(c.id)
        if not plan or not plan.light_out_date: continue
        kd=(today-plan.light_out_date).days
        kinds=kinds_by_cultivation.get(c.id,set())
        def task(kind, target_day, due):
            return {'cultivation_id':c.id,'bay_number':c.bay_number,'round_name':c.round_name,'variety_id':c.variety_id,'variety':varieties.get(c.variety_id),'kind':kind,'due_date':due.isoformat(),'actual_kd_day':kd,'days_until':(due-today).days,'status':'overdue' if due<today else 'due' if due==today else 'upcoming','steering_until_kd':settings.steering_until_kd,'steering_window_open':0<=kd<=settings.steering_until_kd}
        if 'KD10' not in kinds and c.variety_id not in measured_species and kd>=3:
            candidates.append(task('KD10',10,plan.light_out_date+timedelta(days=10)))
        if 'KD10' in kinds and 'KD30' not in kinds and kd>=23:
            candidates.append(task('KD30',30,plan.light_out_date+timedelta(days=30)))
        if c.id in last_pilot and kd>=7:
            due=last_pilot[c.id].replace(tzinfo=timezone.utc).astimezone(NL).date()+timedelta(days=7)
            if due<=today+timedelta(days=7): candidates.append(task('pilot',None,due))
    candidates.sort(key=lambda x:(x['days_until'],x['bay_number']))
    # KD10 is a suggestion for one new bay per variety in the current week.
    selected=[]; species_seen=set()
    for item in candidates:
        if item['kind']=='KD10':
            if item['variety_id'] in species_seen: continue
            species_seen.add(item['variety_id'])
        selected.append(item)
    return {'active_cultivations':len(active),'rounds':sorted({c.round_name for c in active}),'bays':40,'measurements':measurement_count,'steering_until_kd':settings.steering_until_kd,'tasks':selected}
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
    v=Variety(name=name,target_weight_g=data.target_weight_g,active=data.active); db.add(v); db.flush(); db.add(VarietyTargetVersion(variety_id=v.id,target_weight_g=v.target_weight_g,author_id=user.id)); audit(db,user,'create','variety',{'id':v.id}); db.commit(); return obj(v)
@router.get('/api/cultivations')
def cultivations(db:Session=Depends(get_db),user:User=Depends(require_user)): return [obj(c) for c in db.scalars(select(Cultivation).order_by(Cultivation.id.desc()))]
@router.post('/api/cultivations')
def add_cultivation(data:CultivationIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    get_or_404(db,Bay,data.bay_number); variety=get_or_404(db,Variety,data.variety_id)
    if data.status in ('planned','active') and not variety.active: raise HTTPException(422,'Niet-actieve soort kan niet voor een nieuwe lopende of geplande teelt worden gebruikt')
    if data.status in ('planned','active'):
        used=db.scalar(select(func.coalesce(func.sum(Cultivation.beds),0)).where(Cultivation.bay_number==data.bay_number,Cultivation.round_name==data.round_name.strip(),Cultivation.status.in_(['planned','active'])))
        if used+data.beds>6: raise HTTPException(409,'Meer dan zes bedden in deze kap en ronde')
    c=Cultivation(**{**data.model_dump(),'round_name':data.round_name.strip()}); db.add(c); db.flush(); cultivation_revision(db,c); audit(db,user,'create','cultivation',{'id':c.id}); db.commit(); return obj(c)
def measurement_objs(db,measurements):
    """Meetrijen met takken en KD-dag in een vast aantal queries (geen N+1)."""
    if not measurements: return []
    from .settings import effective_settings
    tolerance=effective_settings(db).measurement_tolerance_days
    stems={}
    for s in db.scalars(select(Stem).where(Stem.measurement_id.in_([m.id for m in measurements])).order_by(Stem.id)):
        stems.setdefault(s.measurement_id,[]).append(s)
    plans=latest_plans(db,list({m.cultivation_id for m in measurements}))
    out=[]
    for m in measurements:
        rows=stems.get(m.id,[]); included=[s for s in rows if not s.excluded]
        plan=plans.get(m.cultivation_id)
        kd=(m.measured_at.replace(tzinfo=timezone.utc).astimezone(NL).date()-plan.light_out_date).days if plan and plan.light_out_date else None
        out.append({**obj(m),'actual_kd_day':kd,'stems':[obj(s) for s in rows],'mean_weight_g':statistics.mean(s.weight_g for s in included) if included else None,'mean_length_cm':statistics.mean(s.length_cm for s in included) if included else None,'weight_sd_g':statistics.stdev(s.weight_g for s in included) if len(included)>1 else None,'timing_warning':kd is not None and m.kind in ('KD10','KD30') and abs(kd-int(m.kind[2:]))>tolerance,'timing_tolerance_days':tolerance})
    return out
def measurement_obj(db,m):
    return measurement_objs(db,[m])[0]
@router.get('/api/cultivations/{id}')
def cultivation_detail(id:int,db:Session=Depends(get_db),user:User=Depends(require_user)):
    c=get_or_404(db,Cultivation,id)
    def rows(cls): return db.scalars(select(cls).where(cls.cultivation_id==id).order_by(cls.id)).all()
    return {**obj(c),'planning':[obj(x) for x in rows(PlanningVersion)],'revisions':[obj(x) for x in rows(CultivationRevision)],'measurements':measurement_objs(db,rows(Measurement)),'harvests':[obj(x) for x in rows(Harvest)],'events':[obj(x) for x in rows(Event)]}
@router.post('/api/cultivations/{id}/planning')
def add_planning(id:int,data:PlanningIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    get_or_404(db,Cultivation,id)
    previous=db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id==id).order_by(PlanningVersion.created_at.desc(),PlanningVersion.id.desc()))
    if previous and not (data.reason or '').strip(): raise HTTPException(422,'Reden voor wijziging is verplicht')
    values=data.model_dump()
    if previous:
        for name in ('plant_date','light_out_date','expected_harvest_date','density','planned_stems','interlight_description','lighting_schedule','planned_lamp_hours'):
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
HARVEST_FIELDS=('start_date','end_date','weight_g','stem_count','loss_fraction','quality_classes','notes')
@router.post('/api/cultivations/{id}/harvest')
def add_harvest(id:int,data:HarvestIn,db:Session=Depends(get_db),user:User=Depends(require_write)):
    c=get_or_404(db,Cultivation,id); h=Harvest(cultivation_id=id,author_id=user.id,**data.model_dump()); db.add(h); c.status='harvested'; db.flush(); cultivation_revision(db,c); audit(db,user,'create','harvest',{'id':h.id,'cultivation_id':id}); db.commit(); return obj(h)
@router.post('/api/cultivations/{id}/harvest/{harvest_id}/correction')
def correct_harvest(id:int,harvest_id:int,data:HarvestCorrection,db:Session=Depends(get_db),user:User=Depends(require_write)):
    """Correctie als nieuwe, gekoppelde rij met verplichte reden (zoals bij planning).

    De oorspronkelijke registratie blijft bewaard; prognoses en baselines gebruiken
    vanaf het correctiemoment de nieuwste rij per teelt.
    """
    get_or_404(db,Cultivation,id)
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    previous=db.get(Harvest,harvest_id)
    if previous is None or previous.cultivation_id!=id: raise HTTPException(404,'Oogstregistratie niet gevonden')
    latest=db.scalar(select(Harvest).where(Harvest.cultivation_id==id).order_by(Harvest.created_at.desc(),Harvest.id.desc()))
    if latest.id!=previous.id: raise HTTPException(409,'Corrigeer de meest recente oogstregistratie van deze teelt')
    values={name:getattr(previous,name) for name in HARVEST_FIELDS}
    for name in HARVEST_FIELDS:
        if name in data.model_fields_set: values[name]=getattr(data,name)
    try: values=HarvestIn.model_validate(values).model_dump()
    except (ValidationError,HTTPException): raise HTTPException(422,'Ongeldige gecorrigeerde oogstgegevens')
    before={name:getattr(previous,name) for name in HARVEST_FIELDS}
    if values==before: raise HTTPException(422,'Er is niets gewijzigd')
    h=Harvest(cultivation_id=id,supersedes_id=previous.id,reason=data.reason.strip(),author_id=user.id,**values); db.add(h); db.flush()
    changed={k:{'voor':before[k],'na':values[k]} for k in HARVEST_FIELDS if before[k]!=values[k]}
    audit(db,user,'correct','harvest',{'id':h.id,'cultivation_id':id,'supersedes_id':previous.id,'reason':h.reason,'changes':json.loads(json.dumps(changed,default=str))})
    db.commit(); return obj(h)
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
        db.flush(); mapping_revision(db,previous)
    for other in db.scalars(select(MappingVersion).where(MappingVersion.object_type==data.object_type)):
        overlap=(other.valid_until is None or other.valid_until>=data.valid_from) and (data.valid_until is None or other.valid_from<=data.valid_until)
        if overlap and ((other.bay_number==data.bay_number and (data.object_type!='light' or other.object_number==data.object_number)) or (data.object_type in ('valve','light') and other.object_number==data.object_number)):
            raise HTTPException(409,'Koppeling overlapt een bestaande periode; geef replaces_id op')
    values=data.model_dump(exclude={'replaces_id'})
    m=MappingVersion(**values); db.add(m); db.flush(); mapping_revision(db,m); audit(db,user,'create','mapping',{'id':m.id,'replaces_id':data.replaces_id}); db.commit(); return obj(m)
@router.get('/api/users')
def users(db:Session=Depends(get_db),user:User=Depends(require_admin)): return [public_user(x)|{'active':x.active} for x in db.scalars(select(User).order_by(User.id))]
@router.post('/api/users')
def add_user(data:UserIn,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    if db.scalar(select(User).where(func.lower(User.username)==data.username.strip().lower())): raise HTTPException(409,'Gebruiker bestaat al')
    u=User(username=data.username.strip(),password_hash=hash_password(data.password),role=data.role); db.add(u); db.flush(); audit(db,user,'create','user',{'id':u.id,'role':u.role}); db.commit(); return public_user(u)
@router.get('/api/audit')
def audits(before_id:int|None=Query(None,ge=1),limit:int=Query(1000,ge=1,le=1000),db:Session=Depends(get_db),user:User=Depends(require_admin)):
    # Paginering: standaard de nieuwste 1000 (ongewijzigd); daarna met before_id verder terug.
    query=select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if before_id is not None: query=query.where(AuditLog.id<before_id)
    return [obj(x) for x in db.scalars(query)]
@router.patch('/api/users/{id}')
def update_user(id:int,data:UserUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    target=get_or_404(db,User,id)
    if target.role=='admin' and target.active and (data.role not in (None,'admin') or data.active is False):
        admins=db.scalar(select(func.count()).select_from(User).where(User.role=='admin',User.active.is_(True)))
        if admins<=1: raise HTTPException(409,'De laatste actieve beheerder kan niet worden verwijderd')
    if data.role is not None: target.role=data.role
    if data.active is not None: target.active=data.active
    if data.password is not None: target.password_hash=hash_password(data.password)
    if data.mfa_reset and target.mfa_enabled:
        from .mfa import reset_mfa
        reset_mfa(db,target,user)
    if data.active is False or data.password is not None:
        from sqlalchemy import delete
        from .models import AuthSession
        db.execute(delete(AuthSession).where(AuthSession.user_id==id))
    audit(db,user,'update','user',{'id':id,'role':target.role,'active':target.active,'password_changed':data.password is not None,'mfa_reset':data.mfa_reset}); db.commit()
    return public_user(target)|{'active':target.active}
@router.patch('/api/varieties/{id}')
def update_variety(id:int,data:VarietyUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    v=get_or_404(db,Variety,id); old={'target_weight_g':v.target_weight_g,'active':v.active}
    if data.target_weight_g is not None:
        v.target_weight_g=data.target_weight_g
        db.add(VarietyTargetVersion(variety_id=v.id,target_weight_g=v.target_weight_g,author_id=user.id))
    if data.active is not None: v.active=data.active
    audit(db,user,'update','variety',{'id':id,'before':old,'after':{'target_weight_g':v.target_weight_g,'active':v.active}}); db.commit(); return obj(v)
@router.patch('/api/bays/{number}')
def update_bay(number:int,data:BayUpdate,db:Session=Depends(get_db),user:User=Depends(require_admin)):
    bay=get_or_404(db,Bay,number); old=bay.area_m2; bay.area_m2=data.area_m2
    audit(db,user,'update','bay',{'number':number,'old_area_m2':old,'area_m2':bay.area_m2}); db.commit(); return obj(bay)
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

@router.get('/api/varieties/{id}/targets')
def variety_target_history(id:int,db:Session=Depends(get_db),user:User=Depends(require_user)):
    get_or_404(db,Variety,id)
    return [obj(v) for v in db.scalars(select(VarietyTargetVersion).where(VarietyTargetVersion.variety_id==id).order_by(VarietyTargetVersion.created_at.desc(),VarietyTargetVersion.id.desc()))]

