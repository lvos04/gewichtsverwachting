"""Reviewable cultivation cards, local photo OCR, exports and backups."""
from __future__ import annotations
import csv
from datetime import datetime, date
import hashlib
import html
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from PIL import Image, ImageOps
import qrcode
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import audit, require_admin, require_user, require_write
from .backups import create_backup
from .db import Base, DATA_DIR, get_db
from .models import Cultivation, PlanningVersion, Variety, User, Event, utcnow

router = APIRouter()
AMSTERDAM = ZoneInfo('Europe/Amsterdam')

class CardScan(Base):
    __tablename__ = 'card_scans'
    id: Mapped[int] = mapped_column(primary_key=True)
    cultivation_id: Mapped[int] = mapped_column(ForeignKey('cultivations.id'))
    filename: Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(100))
    checksum: Mapped[str] = mapped_column(String(64))
    ocr_text: Mapped[str] = mapped_column(Text, default='')
    ocr_status: Mapped[str] = mapped_column(String(100))
    qr_content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='review')
    event_ids: Mapped[list] = mapped_column(JSON, default=list)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class ScanRevision(Base):
    __tablename__ = 'scan_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey('card_scans.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    review: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class ScanEvent(BaseModel):
    occurred_at: datetime
    kind: str = Field(min_length=1, max_length=100)
    amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=50)
    scope: str | None = Field(default=None, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('occurred_at')
    @classmethod
    def as_utc(cls, value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=AMSTERDAM)
        return value.astimezone(ZoneInfo('UTC'))

    @model_validator(mode='after')
    def validate_unit(self):
        if self.amount is not None and not (self.unit or '').strip():
            raise ValueError('Eenheid is verplicht bij hoeveelheid')
        return self

class ScanReview(BaseModel):
    notes: str = Field(default='', max_length=10000)
    events: list[ScanEvent] = Field(default_factory=list, max_length=100)

class Confirmation(BaseModel):
    confirmed: Literal[True]

def scan_json(scan):
    return {'id': scan.id, 'cultivation_id': scan.cultivation_id, 'filename': scan.filename,
            'ocr_text': scan.ocr_text, 'ocr_status': scan.ocr_status, 'qr_content': scan.qr_content,
            'review': scan.review, 'status': scan.status, 'event_ids': scan.event_ids,
            'created_at': scan.created_at, 'image_url': f'/api/scans/{scan.id}/image',
            'notice': 'OCR is een concept. Controleer foto en elke invoer; pas na bevestiging worden gebeurtenissen opgeslagen.'}

@router.get('/api/cards', response_class=HTMLResponse)
def cards(ids: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        selected = list(dict.fromkeys(int(x) for x in ids.split(',')))
    except ValueError:
        raise HTTPException(422, 'Geef kapteelt-ID’s, gescheiden door komma’s.')
    if not 1 <= len(selected) <= 80:
        raise HTTPException(422, 'Kies 1 tot 80 teeltkaarten.')
    cards_html = []
    for cid in selected:
        cultivation = db.get(Cultivation, cid)
        if not cultivation:
            raise HTTPException(404, 'Kapteelt niet gevonden')
        variety = db.get(Variety, cultivation.variety_id)
        plan = db.scalar(select(PlanningVersion).where(PlanningVersion.cultivation_id == cid).order_by(PlanningVersion.created_at.desc(), PlanningVersion.id.desc()))
        from .crop_profiles import select_profile, interpolate
        now = datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
        profile, _ = select_profile(db, cultivation.variety_id, plan.plant_date if plan else None, now)
        payload = {'app': 'chrysant', 'version': 1, 'cultivation_id': cid,
                   'planning_version': plan.id if plan else None, 'profile_version': profile.id if profile else None,
                   'round': cultivation.round_name, 'bay': cultivation.bay_number}
        qr = qrcode.make(json.dumps(payload, separators=(',', ':')))
        buffer = io.BytesIO()
        qr.save(buffer, format='PNG')
        import base64
        qr_uri = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
        esc = lambda x: html.escape(str(x if x is not None else '—'))
        light_out = plan.light_out_date if plan else None
        ld = (light_out - plan.plant_date).days if plan and light_out and plan.plant_date else None
        target10 = interpolate(profile.length_curve, 10) if profile else None
        target30 = interpolate(profile.length_curve, 30) if profile else None
        interlight = (plan.interlight_description if plan and plan.interlight_description else profile.interlight_description if profile else None)
        def short(value):
            text = str(value) if value is not None else '—'
            return esc(text[:160] + ('…' if len(text) > 160 else ''))
        rows = ''.join('<tr><td>□ □ / □ □</td><td>□ □ □</td><td>________________</td><td>□ □ □ □</td><td>□ volvelds □ selectief</td><td>________________</td></tr>' for _ in range(5))
        cards_html.append(f'''<article><span class="corner tl">┏</span><span class="corner tr">┓</span><span class="corner bl">┗</span><span class="corner br">┛</span><img class="qr" src="{qr_uri}" alt="QR kaartversie"/><h1>Kap {cultivation.bay_number} · {esc(variety.name)}</h1><p>Ronde <b>{esc(cultivation.round_name)}</b> · {cultivation.beds} bedden · kaartversie {esc(plan.id if plan else None)}</p><div class="facts">Planten: {esc(plan.plant_date if plan else None)} · Licht uit: {esc(light_out)} · LD: {esc(ld)}<br>Geplande oogst: {esc(plan.expected_harvest_date if plan else None)} · Dichtheid: {esc(plan.density if plan else None)}<br>KD10: {esc(light_out + __import__('datetime').timedelta(days=10) if light_out else None)} · KD30: {esc(light_out + __import__('datetime').timedelta(days=30) if light_out else None)}<br>Streeflengte KD10: {esc(round(target10, 1) if target10 is not None else None)} cm · KD30: {esc(round(target30, 1) if target30 is not None else None)} cm · Profiel: {esc(profile.id if profile else None)}<br>Tussenlicht: {short(interlight)} · Lampschema: {short(plan.lighting_schedule if plan else None)}<br>Geplande takken: {esc(plan.planned_stems if plan else None)} · Lampuren/dag: {esc(plan.planned_lamp_hours if plan else None)}</div><table><thead><tr><th>Datum</th><th>Lengte cm</th><th>Middel</th><th>Dosering + eenheid</th><th>Reikwijdte</th><th>Opmerking</th></tr></thead><tbody>{rows}</tbody></table><p>Volvelds: g/1000 m² · Selectief: g/10 liter — afzonderlijk registreren</p><p>Opmerkingen: _____________________________________________________________________</p></article>''')
    return '<!doctype html><html lang="nl"><meta charset="utf-8"><title>Teeltkaarten</title><style>@page{size:A4;margin:8mm}body{font:11px system-ui;margin:0;color:#111}article{box-sizing:border-box;height:138mm;padding:8mm 5mm;position:relative;break-inside:avoid}article:nth-child(2n){break-after:page}h1{font-size:22px;margin:0 0 6px}.qr{width:27mm;height:27mm;float:right}.facts{line-height:1.35;margin:7px 0;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:9px}th,td{border:1px solid #555;padding:7px 3px;text-align:left}.corner{position:absolute;font-size:20px}.tl{left:0;top:0}.tr{right:0;top:0}.bl{left:0;bottom:0}.br{right:0;bottom:0}@media screen{body{max-width:194mm;margin:auto;background:#eee}article{background:white;margin:12px 0}}</style>' + ''.join(cards_html) + '</html>'

@router.post('/api/scans')
def upload_scan(file: UploadFile = File(...), cultivation_id: int = Form(...), user: User = Depends(require_write), db: Session = Depends(get_db)):
    if not db.get(Cultivation, cultivation_id):
        raise HTTPException(404, 'Kapteelt niet gevonden')
    blob = file.file.read(15 * 1024 * 1024 + 1)
    if len(blob) > 15 * 1024 * 1024:
        raise HTTPException(413, 'Foto mag maximaal 15 MB zijn')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(blob))
            if image.width * image.height > 30_000_000:
                raise ValueError('Te veel pixels')
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Gebruik JPEG, PNG of WebP')
            suffix = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[image.format]
            image.load()
            image = ImageOps.exif_transpose(image).convert('RGB')
    except Exception:
        raise HTTPException(422, 'Geen ondersteunde of veilige foto')
    directory = DATA_DIR / 'scans'
    directory.mkdir(mode=0o700, exist_ok=True)
    checksum = hashlib.sha256(blob).hexdigest()
    name = checksum + suffix
    destination = directory / name
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
            tmp.write(blob)
            Path(tmp.name).replace(destination)
    qr_content = None
    try:
        from pyzbar.pyzbar import decode
        decoded = decode(image)
        for code in decoded:
            candidate = json.loads(code.data.decode('utf-8'))
            if isinstance(candidate, dict) and candidate.get('app') == 'chrysant':
                qr_content = candidate
                if candidate.get('cultivation_id') != cultivation_id:
                    raise HTTPException(409, 'QR-code hoort bij een andere kapteelt; kies de juiste teelt.')
                plan_id = candidate.get('planning_version')
                if plan_id:
                    plan = db.get(PlanningVersion, plan_id)
                    if not plan or plan.cultivation_id != cultivation_id:
                        raise HTTPException(409, 'QR-kaartversie bestaat niet bij deze teelt.')
                break
    except HTTPException:
        raise
    except (ImportError, OSError, ValueError, UnicodeError):
        pass
    text = ''
    ocr_status = 'handmatig: lokale OCR niet beschikbaar'
    if shutil.which('tesseract'):
        with tempfile.TemporaryDirectory() as tmp:
            ocr_path = Path(tmp) / 'photo.png'
            image.thumbnail((3000, 3000))
            image.save(ocr_path)
            try:
                result = subprocess.run(['tesseract', str(ocr_path), 'stdout', '-l', 'nld+eng', '--psm', '6'], capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    text = result.stdout[:30000]
                    ocr_status = 'concept: lokale OCR; handschrift altijd controleren'
                else:
                    ocr_status = 'handmatig: OCR kon foto niet uitlezen'
            except (subprocess.TimeoutExpired, OSError):
                ocr_status = 'handmatig: OCR niet voltooid'
    scan = CardScan(cultivation_id=cultivation_id, filename=Path(file.filename or 'foto').name[:200], stored_name=name,
                    checksum=checksum, ocr_text=text, ocr_status=ocr_status, qr_content=qr_content,
                    review={'notes': '', 'events': []}, user_id=user.id, status='review', event_ids=[])
    db.add(scan)
    db.flush()
    audit(db, user, 'scan_uploaded', f'scan:{scan.id}', {'checksum': checksum, 'qr_found': bool(qr_content)})
    db.commit()
    return scan_json(scan)

@router.get('/api/scans')
def scans(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return [scan_json(s) for s in db.scalars(select(CardScan).order_by(CardScan.id.desc()).limit(200))]

@router.get('/api/scans/{scan_id}/image')
def scan_image(scan_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    scan = db.get(CardScan, scan_id)
    if not scan:
        raise HTTPException(404, 'Scan niet gevonden')
    return FileResponse(DATA_DIR / 'scans' / scan.stored_name, headers={'Cache-Control': 'no-store'})

@router.patch('/api/scans/{scan_id}')
def review_scan(scan_id: int, data: ScanReview, user: User = Depends(require_write), db: Session = Depends(get_db)):
    scan = db.get(CardScan, scan_id)
    if not scan:
        raise HTTPException(404, 'Scan niet gevonden')
    if scan.status == 'confirmed':
        raise HTTPException(409, 'Deze scan is al bevestigd; correcties als nieuwe gebeurtenis vastleggen.')
    review = data.model_dump(mode='json')
    scan.review = review
    db.add(ScanRevision(scan_id=scan.id, user_id=user.id, review=review))
    audit(db, user, 'scan_reviewed', f'scan:{scan.id}', {'events': len(data.events)})
    db.commit()
    return scan_json(scan)

@router.post('/api/scans/{scan_id}/confirm')
def confirm_scan(scan_id: int, data: Confirmation, user: User = Depends(require_write), db: Session = Depends(get_db)):
    # Serialize confirms so simultaneous retries never duplicate agronomic events.
    if db.bind.dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    scan = db.get(CardScan, scan_id)
    if not scan:
        raise HTTPException(404, 'Scan niet gevonden')
    if scan.status == 'confirmed':
        return scan_json(scan)
    review = ScanReview.model_validate(scan.review)
    ids = []
    for row in review.events:
        values = row.model_dump()
        values['occurred_at'] = row.occurred_at.replace(tzinfo=None)
        event = Event(cultivation_id=scan.cultivation_id, **values)
        db.add(event)
        db.flush()
        ids.append(event.id)
    scan.status = 'confirmed'
    scan.event_ids = ids
    audit(db, user, 'scan_confirmed', f'scan:{scan.id}', {'event_ids': ids})
    db.commit()
    return scan_json(scan)

@router.get('/api/backups')
def backups(user: User = Depends(require_admin)):
    return [{'filename': p.name, 'size': p.stat().st_size, 'created_at': datetime.fromtimestamp(p.stat().st_mtime, ZoneInfo('UTC')).isoformat()} for p in sorted((DATA_DIR / 'backups').glob('*.zip'), reverse=True)]

@router.post('/api/backups')
def backup(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    audit(db, user, 'backup_requested', 'backup')
    db.commit()
    target = create_backup()
    return {'filename': target.name, 'download_url': '/api/backups/' + target.name, 'size': target.stat().st_size}

@router.get('/api/backups/{filename}')
def backup_download(filename: str, user: User = Depends(require_admin)):
    if Path(filename).name != filename or not filename.startswith('chrysant-') or not filename.endswith('.zip'):
        raise HTTPException(404)
    path = DATA_DIR / 'backups' / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=filename, media_type='application/zip', headers={'Cache-Control': 'no-store'})

@router.get('/api/export')
def export_data(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Excel-readable UTF-8 CSVs; no auth secrets or password hashes."""
    excluded = {'users', 'auth_sessions', 'login_attempts', 'alembic_version'}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for table in Base.metadata.sorted_tables:
            if table.name in excluded:
                continue
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            writer.writerow([c.name for c in table.columns])
            for row in db.execute(select(table)).mappings():
                values = []
                for value in row.values():
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    elif isinstance(value, (datetime, date)):
                        value = value.isoformat()
                    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
                        value = "'" + value
                    values.append(value)
                writer.writerow(values)
            archive.writestr(table.name + '.csv', '\ufeff' + output.getvalue())
    audit(db, user, 'export', 'all_business_tables')
    db.commit()
    buffer.seek(0)
    return StreamingResponse(buffer, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="chrysant-export.zip"', 'Cache-Control': 'no-store'})
