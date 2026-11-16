from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text, select, func

from . import auth, core, importer, forecast, operations, settings
from .backups import create_backup
from .db import DATA_DIR, SessionLocal, engine
from .models import Cultivation, Measurement, PlanningVersion, Event

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / 'static'
log = logging.getLogger('chrysant')

def migrate():
    """Always make a consistent backup before upgrading an existing schema."""
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext
    config = Config(str(ROOT / 'alembic.ini'))
    desired = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        existing = inspect(connection).get_table_names()
    if current != desired:
        if existing:
            create_backup()
        command.upgrade(config, 'head')

def refresh_due_forecasts():
    """Refresh derived snapshots daily and when a registered fact changes."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        cultivations = db.scalars(select(Cultivation).where(Cultivation.status.in_(['active', 'planned']))).all()
        for cultivation in cultivations:
            last = db.scalar(select(func.max(forecast.ForecastSnapshot.created_at)).where(forecast.ForecastSnapshot.cultivation_id == cultivation.id))
            changed = [db.scalar(select(func.max(table.created_at)).where(table.cultivation_id == cultivation.id))
                       for table in (Measurement, PlanningVersion, Event)]
            fresh_fact = max((value for value in changed if value is not None), default=None)
            if last is None or (now - last).total_seconds() >= 86400 or (fresh_fact and fresh_fact > last):
                forecast.forecast(db, cultivation.id, now)

async def daily_backups():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(refresh_due_forecasts)
        except Exception:
            log.exception('Automatische prognoseberekening mislukt')
        with SessionLocal() as db:
            config = settings.effective_settings(db)
        latest = max((p.stat().st_mtime for p in (DATA_DIR / 'backups').glob('*.zip')), default=0)
        if datetime.now(timezone.utc).timestamp() - latest > config.backup_interval_hours * 3600:
            try:
                await asyncio.to_thread(create_backup)
                if config.backup_keep_days:
                    cutoff = datetime.now(timezone.utc).timestamp() - config.backup_keep_days * 86400
                    archives = sorted((DATA_DIR / 'backups').glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
                    for old in archives[1:]:
                        if old.stat().st_mtime < cutoff:
                            old.unlink()
            except Exception:
                log.exception('Automatische lokale back-up mislukt')

@asynccontextmanager
async def lifespan(app):
    migrate()
    with SessionLocal() as db:
        core.seed_data(db)
    importer.recover_jobs()
    task = asyncio.create_task(daily_backups())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    importer.shutdown_worker()

app = FastAPI(title='Gewichtsverwachting chrysant', version='0.1.1', lifespan=lifespan,
              docs_url=None if os.environ.get('DISABLE_API_DOCS') == '1' else '/docs')

app.add_middleware(importer.ImportUploadLimitMiddleware)

@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    # Do not echo credentials or non-JSON values (NaN/Inf) from submitted input.
    return JSONResponse(status_code=422, content={'detail': [
        {'loc': list(item['loc']), 'msg': item['msg'], 'type': item['type']}
        for item in error.errors()
    ]})

@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    if os.environ.get('SECURE_COOKIES', '').lower() in ('1', 'true'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response

app.include_router(auth.router)
app.include_router(core.router)
app.include_router(importer.router)
app.include_router(forecast.router)
app.include_router(operations.router)
app.include_router(settings.router)

@app.get('/health', include_in_schema=False)
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return {'status': 'ok', 'version': '0.1.1'}

@app.get('/sw.js', include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / 'sw.js', media_type='application/javascript', headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

app.mount('/static', StaticFiles(directory=STATIC), name='static')

@app.get('/', include_in_schema=False)
def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})
