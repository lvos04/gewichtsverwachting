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
from sqlalchemy import inspect, text

from . import __version__, auth, core, koppeling, mfa, onderhoud, importer, forecast, operations, settings, crop_profiles, environment, history_import, insights, legacy_lighting, bridge
from .backups import create_backup
from .db import DATA_DIR, SessionLocal, engine

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

# Herberekening en opschoning staan in app/onderhoud.py; naam blijft hier beschikbaar.
refresh_due_forecasts = onderhoud.refresh_due_forecasts

async def daily_backups():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(refresh_due_forecasts)
        except Exception:
            log.exception('Automatische prognoseberekening mislukt')
        try:
            await asyncio.to_thread(onderhoud.daily_cleanup)
        except Exception:
            log.exception('Dagelijkse opschoning mislukt')
        try:
            # Doet niets zolang "Klimaat- en lichtdata uit Priva Bridge" uit staat.
            await asyncio.to_thread(bridge.maybe_daily_sync)
        except Exception:
            log.exception('Dagelijkse Priva Bridge-synchronisatie mislukt')
        with SessionLocal() as db:
            config = settings.effective_settings(db)
        latest = max((p.stat().st_mtime for p in (DATA_DIR / 'backups').glob('*.zip')), default=0)
        if datetime.now(timezone.utc).timestamp() - latest > config.backup_interval_hours * 3600:
            try:
                await asyncio.to_thread(create_backup)
                onderhoud.prune_backups(config.backup_keep_days)
            except Exception:
                log.exception('Automatische lokale back-up mislukt')

@asynccontextmanager
async def lifespan(app):
    migrate()
    with SessionLocal() as db:
        core.seed_data(db)
    importer.recover_jobs()
    legacy_lighting.recover_jobs()
    task = asyncio.create_task(daily_backups())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    legacy_lighting.shutdown_worker()
    importer.shutdown_worker()

app = FastAPI(title='Gewichtsverwachting chrysant', version=__version__, lifespan=lifespan,
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
app.include_router(mfa.router)
app.include_router(onderhoud.router)
app.include_router(core.router)
app.include_router(importer.router)
app.include_router(forecast.router)
app.include_router(operations.router)
app.include_router(settings.router)
app.include_router(crop_profiles.router)
app.include_router(environment.router)
app.include_router(history_import.router)
app.include_router(insights.router)
app.include_router(legacy_lighting.router)
app.include_router(bridge.router)
koppeling.install(app)

@app.get('/health', include_in_schema=False)
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return {'status': 'ok', 'version': __version__}

@app.get('/sw.js', include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / 'sw.js', media_type='application/javascript', headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

app.mount('/static', StaticFiles(directory=STATIC), name='static')

@app.get('/', include_in_schema=False)
def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})
