"""Optionele TOTP-tweestapsverificatie (RFC 6238) per gebruiker.

Standaard uit. Een gebruiker zet het zelf aan via Beheer; een beheerder kan het
voor een ander account resetten, en via de CLI (`python -m app.beheer_cli mfa-uit`)
kan het bij verlies van de telefoon worden uitgezet. Met de instelling
"MFA verplicht voor beheerders" (standaard uit) hebben beheerders zonder MFA
geen toegang tot beheerfuncties totdat zij MFA hebben ingesteld.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import audit, public_user, require_user, verify_password
from .db import get_db
from .models import User

router = APIRouter()
ISSUER = "Gewichtsverwachting"
STEP = 30
DIGITS = 6
WINDOW = 1  # één stap (30 s) speling voor klokverschil


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def totp(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    code = (code or "").strip().replace(" ", "")
    if not secret or len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def verify_user_code(user: User, code: str, now: float | None = None) -> bool:
    """Controleer de code en voorkom hergebruik van dezelfde (of een oudere) tijdstap."""
    step = matching_step(user.mfa_secret or "", code, now)
    if step is None or (user.mfa_last_step is not None and step <= user.mfa_last_step):
        return False
    user.mfa_last_step = step
    return True


def otpauth_uri(user: User, secret: str) -> str:
    label = quote(f"{ISSUER}:{user.username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg_data_uri(text: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:  # pragma: no cover - qrcode is een vaste afhankelijkheid
        return None
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


class Code(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class Disable(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=10)


@router.get("/api/auth/mfa")
def mfa_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return public_user(user, db) | {"pending_setup": bool(user.mfa_secret and not user.mfa_enabled)}


@router.post("/api/auth/mfa/setup")
def mfa_setup(user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise HTTPException(409, "Tweestapsverificatie staat al aan")
    secret = new_secret()
    user.mfa_secret = secret
    user.mfa_last_step = None
    audit(db, user, "mfa_setup_started", f"user:{user.id}")
    db.commit()
    uri = otpauth_uri(user, secret)
    return {"secret": secret, "otpauth_uri": uri, "qr": qr_svg_data_uri(uri)}


@router.post("/api/auth/mfa/enable")
def mfa_enable(data: Code, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise HTTPException(409, "Tweestapsverificatie staat al aan")
    if not user.mfa_secret:
        raise HTTPException(409, "Start eerst het instellen van tweestapsverificatie")
    if not verify_user_code(user, data.code):
        raise HTTPException(422, "Code klopt niet. Controleer de tijd op je telefoon en probeer opnieuw.")
    user.mfa_enabled = True
    audit(db, user, "mfa_enabled", f"user:{user.id}")
    db.commit()
    return public_user(user, db)


@router.post("/api/auth/mfa/disable")
def mfa_disable(data: Disable, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from .settings import effective_settings

    if not user.mfa_enabled:
        raise HTTPException(409, "Tweestapsverificatie staat niet aan")
    if user.role == "admin" and effective_settings(db).mfa_required_admins:
        raise HTTPException(409, "Tweestapsverificatie is verplicht voor beheerders")
    if not verify_password(data.password, user.password_hash) or not verify_user_code(user, data.code):
        raise HTTPException(422, "Wachtwoord of code klopt niet")
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_last_step = None
    audit(db, user, "mfa_disabled", f"user:{user.id}")
    db.commit()
    return public_user(user, db)


def reset_mfa(db: Session, target: User, actor: User | None) -> None:
    """Beheerder of CLI: MFA voor een account uitzetten (bijv. telefoon kwijt)."""
    target.mfa_enabled = False
    target.mfa_secret = None
    target.mfa_last_step = None
    audit(
        db, actor, "mfa_reset", f"user:{target.id}", {"username": target.username, **({} if actor else {"via": "cli"})}
    )
