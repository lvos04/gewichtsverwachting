import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, AuthSession, LoginAttempt, AuditLog

router = APIRouter()
COOKIE='chrysant_session'
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
def validate_origin(request: Request):
    origin=request.headers.get('origin')
    if origin:
        parsed=urlsplit(origin)
        # Forwarded hosts are deliberately not trusted by the application.
        if parsed.scheme not in ('http','https') or parsed.netloc.lower() != request.headers.get('host','').lower() or parsed.path not in ('','/'):
            raise HTTPException(403,'Ongeldige oorsprong')
def require_user(request: Request, db: Session=Depends(get_db)):
    if request.method not in ('GET','HEAD','OPTIONS'): validate_origin(request)
    token=request.cookies.get(COOKIE)
    session=db.get(AuthSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
    user=db.get(User,session.user_id) if session and session.expires_at > datetime.now(timezone.utc).replace(tzinfo=None) else None
    if not user or not user.active: raise HTTPException(401,'Aanmelden vereist')
    return user
def require_write(user: User=Depends(require_user)):
    if user.role not in ('admin','medewerker'): raise HTTPException(403,'Geen schrijfrechten')
    return user
def require_admin(user: User=Depends(require_user)):
    if user.role != 'admin': raise HTTPException(403,'Beheerrechten vereist')
    return user
class Credentials(BaseModel):
    username: str=Field(min_length=1,max_length=80)
    password: str=Field(min_length=1,max_length=256)
def public_user(u): return {'id':u.id,'username':u.username,'role':u.role}
def make_session(db, response, user, request):
    from .settings import effective_settings
    hours=effective_settings(db).session_hours
    token=secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,expires_at=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(hours=hours)))
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=(request.url.scheme=='https' or os.environ.get('SECURE_COOKIES','').lower() in ('1','true')),max_age=hours*3600,path='/')
@router.get('/api/auth/status')
def status(request: Request, db: Session=Depends(get_db)):
    token=request.cookies.get(COOKIE)
    s=db.get(AuthSession,hashlib.sha256(token.encode()).hexdigest()) if token else None
    user=db.get(User,s.user_id) if s and s.expires_at>datetime.now(timezone.utc).replace(tzinfo=None) else None
    return {'setup_required': db.scalar(select(func.count()).select_from(User))==0, 'user':public_user(user) if user and user.active else None}
@router.post('/api/auth/setup')
def setup(data: Credentials, request: Request, response: Response, db: Session=Depends(get_db)):
    validate_origin(request)
    if len(data.password)<12 or not data.username.strip(): raise HTTPException(422,'Kies een naam en wachtwoord van minimaal 12 tekens')
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if db.scalar(select(func.count()).select_from(User)): raise HTTPException(409,'Installatie is al voltooid')
    user=User(username=data.username.strip(),password_hash=hash_password(data.password),role='admin')
    db.add(user); db.flush(); make_session(db,response,user,request); audit(db,user,'setup','user',{'id':user.id}); db.commit()
    return {'user':public_user(user)}
@router.post('/api/auth/login')
def login(data: Credentials, request: Request, response: Response, db: Session=Depends(get_db)):
    validate_origin(request)
    # Per account and per source, persisted so restarting cannot reset limits.
    ip=request.client.host if request.client else 'unknown'
    keys=['ip:'+ip,'user:'+data.username.strip().casefold()]
    if db.bind.dialect.name=='sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    from .settings import effective_settings
    settings=effective_settings(db)
    cutoff=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=settings.login_window_minutes)
    counts=db.execute(select(LoginAttempt.key,func.count()).where(LoginAttempt.key.in_(keys),LoginAttempt.attempted_at>=cutoff).group_by(LoginAttempt.key)).all()
    if any(count>=settings.login_max_attempts for _,count in counts):
        raise HTTPException(429,'Te veel aanmeldpogingen; probeer later opnieuw')
    user=db.scalar(select(User).where(func.lower(User.username)==data.username.strip().lower()))
    if not user or not user.active or not verify_password(data.password,user.password_hash):
        db.add_all([LoginAttempt(key=k) for k in keys]); audit(db,None,'login_failed','authentication',{'username':data.username.strip()}); db.commit()
        raise HTTPException(401,'Ongeldige gebruikersnaam of wachtwoord')
    db.execute(delete(LoginAttempt).where(LoginAttempt.key.in_(keys)))
    make_session(db,response,user,request); audit(db,user,'login','authentication'); db.commit()
    return {'user':public_user(user)}
@router.post('/api/auth/logout')
def logout(request: Request, response: Response, user: User=Depends(require_user), db: Session=Depends(get_db)):
    token=request.cookies.get(COOKIE)
    if token: db.execute(delete(AuthSession).where(AuthSession.token_hash==hashlib.sha256(token.encode()).hexdigest()))
    audit(db,user,'logout','authentication'); db.commit(); response.delete_cookie(COOKIE,path='/')
    return {'ok':True}

# Register the settings table before standalone applications create metadata.
from . import settings as _settings_registration
