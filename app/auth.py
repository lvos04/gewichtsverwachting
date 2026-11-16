import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, AuthSession, LoginAttempt, AuditLog, MfaChallenge

router = APIRouter()
COOKIE='chrysant_session'
MFA_CHALLENGE_MINUTES = 5
MFA_CHALLENGE_ATTEMPTS = 5
def _now(): return datetime.now(timezone.utc).replace(tzinfo=None)
def audit(db, user, action, entity, details=None):
    db.add(AuditLog(user_id=user.id if user else None, action=action, entity=str(entity), details=details))
def hash_password(password):
    salt=os.urandom(16)
    digest=hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'
def verify_password(password, encoded):
    try:
        algorithm,n,r,p,salt,digest=encoded.split('$')
        if algorithm != 'scrypt': return False
        return hmac.compare_digest(hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=int(n),r=int(r),p=int(p)).hex(),digest)
    except (ValueError, TypeError): return False

def _same_host(value: str, request: Request) -> bool:
    parsed=urlsplit(value)
    # Forwarded hosts are deliberately not trusted by the application.
    return parsed.scheme in ('http','https') and parsed.netloc.lower() == request.headers.get('host','').lower()

def validate_origin(request: Request, strict: bool = False):
    """Weiger cross-site schrijfverzoeken.

    `Origin` wordt altijd gecontroleerd als hij aanwezig is. Met de instelling
    "strikte oorsprongcontrole" (standaard aan) moet een schrijfverzoek zonder
    `Origin` een `Referer` van dezelfde host hebben; zonder beide wordt het
    verzoek geweigerd. Browsers sturen bij fetch/POST altijd `Origin`, en de
    app zet `Referrer-Policy: same-origin`, dus de eigen webapp en PWA-sync
    voldoen hier altijd aan.
    """
    origin=request.headers.get('origin')
    if origin:
        if not _same_host(origin, request) or urlsplit(origin).path not in ('','/'):
            raise HTTPException(403,'Ongeldige oorsprong')
        return
    if not strict:
        return
    referer=request.headers.get('referer')
    if not referer or not _same_host(referer, request):
        raise HTTPException(403,'Ongeldige of ontbrekende oorsprong')

def _strict_origin(db) -> bool:
    from .settings import effective_settings
    return effective_settings(db).strict_origin_check

def _session_user(request: Request, db: Session):
    token=request.cookies.get(COOKIE)
    session=db.get(AuthSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
    user=db.get(User,session.user_id) if session and session.expires_at > _now() else None
    return user if user and user.active else None
def require_user(request: Request, db: Session=Depends(get_db)):
    if request.method not in ('GET','HEAD','OPTIONS'): validate_origin(request, _strict_origin(db))
    user=_session_user(request, db)
    if not user: raise HTTPException(401,'Aanmelden vereist')
    return user
def require_write(user: User=Depends(require_user)):
    if user.role not in ('admin','medewerker'): raise HTTPException(403,'Geen schrijfrechten')
    return user
def mfa_setup_required(db, user) -> bool:
    """Instelling "MFA verplicht voor beheerders" (standaard uit)."""
    if user.role != 'admin' or user.mfa_enabled: return False
    from .settings import effective_settings
    return effective_settings(db).mfa_required_admins
def require_admin(user: User=Depends(require_user), db: Session=Depends(get_db)):
    if user.role != 'admin': raise HTTPException(403,'Beheerrechten vereist')
    if mfa_setup_required(db, user):
        raise HTTPException(403,'Tweestapsverificatie is verplicht voor beheerders. Stel deze eerst in bij Beheer → Tweestapsverificatie.')
    return user
class Credentials(BaseModel):
    username: str=Field(min_length=1,max_length=80)
    password: str=Field(min_length=1,max_length=256)
def public_user(u, db=None):
    data={'id':u.id,'username':u.username,'role':u.role,'mfa_enabled':bool(u.mfa_enabled)}
    if db is not None: data['mfa_setup_required']=mfa_setup_required(db,u)
    return data
def make_session(db, response, user, request):
    from .settings import effective_settings
    hours=effective_settings(db).session_hours
    token=secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,expires_at=_now()+timedelta(hours=hours)))
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=(request.url.scheme=='https' or os.environ.get('SECURE_COOKIES','').lower() in ('1','true')),max_age=hours*3600,path='/')

# --- Echt IP-adres achter een vertrouwde proxy -------------------------------------------
def parse_proxies(value: str) -> list:
    networks=[]
    for part in (value or '').replace(';',',').split(','):
        part=part.strip()
        if not part: continue
        try: networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError: continue
    return networks
def trusted_proxies(settings) -> list:
    """Instelling `trusted_proxies`; leeg = omgevingsvariabele TRUSTED_PROXIES; standaard geen."""
    return parse_proxies(settings.trusted_proxies or os.environ.get('TRUSTED_PROXIES',''))
def _in(networks, value) -> bool:
    try: address=ipaddress.ip_address(value)
    except ValueError: return False
    return any(address in network for network in networks)
def client_ip(request: Request, networks) -> str:
    """Directe verbinding is leidend; `X-Forwarded-For` alleen van vertrouwde proxy's.

    De keten wordt van rechts gelezen: het eerste adres dat geen vertrouwde
    proxy is, is de echte client. Zo kan een client zelf geen adres vervalsen.
    """
    peer=request.client.host if request.client else 'unknown'
    if not networks or not _in(networks, peer): return peer
    hops=[h.strip() for h in request.headers.get('x-forwarded-for','').split(',') if h.strip()]
    for hop in reversed(hops):
        try: ipaddress.ip_address(hop)
        except ValueError: return peer
        if not _in(networks, hop): return hop
    return hops[0] if hops else peer

@router.get('/api/auth/status')
def status(request: Request, db: Session=Depends(get_db)):
    user=_session_user(request, db)
    return {'setup_required': db.scalar(select(func.count()).select_from(User))==0, 'user':public_user(user,db) if user else None}
@router.post('/api/auth/setup')
def setup(data: Credentials, request: Request, response: Response, db: Session=Depends(get_db)):
    validate_origin(request, _strict_origin(db))
    if len(data.password)<12 or not data.username.strip(): raise HTTPException(422,'Kies een naam en wachtwoord van minimaal 12 tekens')
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if db.scalar(select(func.count()).select_from(User)): raise HTTPException(409,'Installatie is al voltooid')
    user=User(username=data.username.strip(),password_hash=hash_password(data.password),role='admin')
    db.add(user); db.flush(); make_session(db,response,user,request); audit(db,user,'setup','user',{'id':user.id}); db.commit()
    return {'user':public_user(user,db)}

def _limit_keys(request, db, username):
    from .settings import effective_settings
    settings=effective_settings(db)
    ip=client_ip(request, trusted_proxies(settings))
    return settings, 'ip:'+ip, 'user:'+username.strip().casefold()
def _check_limits(db, settings, ip_key, user_key):
    """Per account (login_max_attempts) en per echt IP (login_ip_max_attempts).

    Het IP-limiet is ruimer, zodat één persoon met een fout wachtwoord achter een
    gedeeld adres niet iedereen blokkeert. 0 = zelfde limiet als per account (oud gedrag).
    """
    cutoff=_now()-timedelta(minutes=settings.login_window_minutes)
    counts=dict(db.execute(select(LoginAttempt.key,func.count()).where(LoginAttempt.key.in_([ip_key,user_key]),LoginAttempt.attempted_at>=cutoff).group_by(LoginAttempt.key)).all())
    ip_limit=settings.login_ip_max_attempts or settings.login_max_attempts
    if counts.get(user_key,0)>=settings.login_max_attempts or counts.get(ip_key,0)>=ip_limit:
        raise HTTPException(429,'Te veel aanmeldpogingen; probeer later opnieuw')
    return counts.get(user_key,0)
def _fail(db, settings, keys, previous_failures, username, reason='login_failed'):
    db.add_all([LoginAttempt(key=k) for k in keys]); audit(db,None,reason,'authentication',{'username':username.strip()}); db.commit()
    # Oplopende vertraging na mislukte pogingen, pas na commit (geen schrijfslot vasthouden).
    delay=min(settings.login_delay_max_seconds, 0.25*previous_failures)
    if delay>0: time.sleep(delay)

@router.post('/api/auth/login')
def login(data: Credentials, request: Request, response: Response, db: Session=Depends(get_db)):
    validate_origin(request, _strict_origin(db))
    # Per account and per real source, persisted so restarting cannot reset limits.
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    settings, ip_key, user_key=_limit_keys(request, db, data.username)
    failures=_check_limits(db, settings, ip_key, user_key)
    user=db.scalar(select(User).where(func.lower(User.username)==data.username.strip().lower()))
    if not user or not user.active or not verify_password(data.password,user.password_hash):
        _fail(db, settings, [ip_key,user_key], failures, data.username)
        raise HTTPException(401,'Ongeldige gebruikersnaam of wachtwoord')
    if user.mfa_enabled:
        token=secrets.token_urlsafe(32)
        db.add(MfaChallenge(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,expires_at=_now()+timedelta(minutes=MFA_CHALLENGE_MINUTES)))
        audit(db,user,'login_password_ok_mfa_pending','authentication'); db.commit()
        return {'mfa_required':True,'challenge':token}
    db.execute(delete(LoginAttempt).where(LoginAttempt.key.in_([ip_key,user_key])))
    make_session(db,response,user,request); audit(db,user,'login','authentication'); db.commit()
    return {'user':public_user(user,db)}

class MfaLogin(BaseModel):
    challenge: str=Field(min_length=10,max_length=200)
    code: str=Field(min_length=6,max_length=10)
@router.post('/api/auth/login/mfa')
def login_mfa(data: MfaLogin, request: Request, response: Response, db: Session=Depends(get_db)):
    from .mfa import verify_user_code
    validate_origin(request, _strict_origin(db))
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    challenge=db.get(MfaChallenge, hashlib.sha256(data.challenge.encode()).hexdigest())
    if not challenge or challenge.expires_at<=_now() or challenge.attempts>=MFA_CHALLENGE_ATTEMPTS:
        raise HTTPException(401,'Aanmeldcode verlopen; log opnieuw in')
    user=db.get(User, challenge.user_id)
    settings, ip_key, user_key=_limit_keys(request, db, user.username if user else '')
    failures=_check_limits(db, settings, ip_key, user_key)
    if not user or not user.active or not user.mfa_enabled or not verify_user_code(user, data.code):
        challenge.attempts+=1
        _fail(db, settings, [ip_key,user_key], failures, user.username if user else '', 'login_mfa_failed')
        raise HTTPException(401,'Ongeldige verificatiecode')
    db.delete(challenge)
    db.execute(delete(LoginAttempt).where(LoginAttempt.key.in_([ip_key,user_key])))
    make_session(db,response,user,request); audit(db,user,'login','authentication',{'mfa':True}); db.commit()
    return {'user':public_user(user,db)}

@router.post('/api/auth/logout')
def logout(request: Request, response: Response, user: User=Depends(require_user), db: Session=Depends(get_db)):
    token=request.cookies.get(COOKIE)
    if token: db.execute(delete(AuthSession).where(AuthSession.token_hash==hashlib.sha256(token.encode()).hexdigest()))
    audit(db,user,'logout','authentication'); db.commit(); response.delete_cookie(COOKIE,path='/')
    return {'ok':True}

# Register the settings table before standalone applications create metadata.
from . import settings as _settings_registration  # noqa: E402,F401
