"""Real browser acceptance checks against an isolated temporary server/database.
Run: python tests/browser_smoke.py (install Playwright Chromium first).
Only synthetic fixtures are used. No shared developer database is modified.
"""
from __future__ import annotations
import os
import re
import io
from PIL import Image
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory(prefix='chrysant-browser-') as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'DATA_DIR': tmp, 'SECURE_COOKIES': '0'}
        logfile = open(Path(tmp) / 'server.log', 'w+')
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=logfile, stderr=subprocess.STDOUT)
        base = f'http://127.0.0.1:{port}'
        try:
            for _ in range(120):
                if server.poll() is not None:
                    logfile.seek(0)
                    raise RuntimeError(logfile.read())
                try:
                    urllib.request.urlopen(base + '/health', timeout=1)
                    break
                except Exception:
                    time.sleep(.15)
            else:
                raise RuntimeError('Testserver start niet')
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
                ctx = browser.new_context(viewport={'width': 390, 'height': 844}, timezone_id='Europe/Amsterdam', locale='nl-NL')
                # Simulate LAN HTTP: randomUUID is unavailable; cryptographic UUID fallback must work.
                ctx.add_init_script("Object.defineProperty(window.crypto, 'randomUUID', {value: undefined, configurable: true})")
                page = ctx.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(e.stack or str(e)))
                page.on('dialog', lambda d: d.accept())
                page.goto(base)
                page.get_by_label('Gebruikersnaam').fill('browser-owner')
                page.get_by_label('Wachtwoord', exact=True).fill('synthetic password 2026')
                page.get_by_role('button', name='Account aanmaken').click()
                expect(page.get_by_role('heading', name='Goed zicht op je teelt')).to_be_visible()
                page.locator('#new-cultivation').click()
                modal = page.locator('#modal')
                modal.get_by_label('Ronde', exact=True).fill('Synthetische winterronde')
                modal.get_by_label('Kap', exact=True).select_option('1')
                modal.get_by_label('Plantdatum', exact=True).fill('2026-10-01')
                modal.get_by_label('Licht uit', exact=True).fill('2026-10-20')
                modal.get_by_label('Verwachte oogst', exact=True).fill('2027-01-10')
                modal.get_by_label('Plantdichtheid (takken/m²)').fill('54')
                modal.get_by_role('button', name='Kapteelt opslaan').click()
                expect(page.get_by_role('heading', name='Kap 1 · antonov')).to_be_visible()
                expect(page.get_by_text(re.compile(r'De dagen worden (langer|korter)|Rond het keerpunt van de daglengte'), exact=False)).to_be_visible()
                expect(page.get_by_text('Nog geen betrouwbare gewichtsverwachting beschikbaar.', exact=False)).to_be_visible()
                assert page.request.get(base + '/api/cultivations/1').json()['planning'][0]['density'] == 54
                if os.environ.get('CAPTURE_SCREENSHOTS'):
                    page.set_viewport_size({'width':1440,'height':1100})
                    page.goto(base + '/#dashboard')
                    expect(page.get_by_role('heading', name='Goed zicht op je teelt')).to_be_visible()
                    page.screenshot(path='/workspace/scratch/chrysant-dashboard.png', full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                page.goto(base + '/#bays')
                expect(page.locator('[data-bay]')).to_have_count(40)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).slice(0,30)")
                page.goto(base + '/#measure/1')
                expect(page.locator('#measure-form')).to_be_visible()
                page.get_by_label('Meetplek / protocolafwijking').fill('Vijf vaste plekken, ochtend')
                for i in range(5):
                    page.get_by_label(f'Tak {i+1} gewicht in gram', exact=True).fill(str(25+i))
                    page.get_by_label(f'Tak {i+1} lengte in centimeter', exact=True).fill(str(40+i))
                if os.environ.get('CAPTURE_SCREENSHOTS'):
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path='/workspace/scratch/chrysant-mobile.png', full_page=True)
                # Local draft must survive a reload too, before committing to queue.
                expect(page.locator('#draft-status')).to_contain_text('Concept opgeslagen')
                page.reload()
                expect(page.get_by_label('Tak 1 gewicht in gram', exact=True)).to_have_value('25')
                # Wait until the service worker controls the page before offline reload.
                page.evaluate('navigator.serviceWorker.ready')
                page.reload()
                page.wait_for_function('navigator.serviceWorker.controller !== null')
                ctx.set_offline(True)
                page.get_by_role('button', name='Meting veilig bewaren').click()
                expect(page.locator('#queue-count')).to_have_text('1')
                expect(page.locator('#queue')).to_contain_text('Lokaal bewaard')
                page.reload()
                expect(page.locator('#queue-count')).to_have_text('1')
                expect(page.locator('#queue')).to_contain_text('Lokaal bewaard')
                ctx.set_offline(False)
                page.locator('#sync-now').click()
                expect(page.locator('#queue')).to_contain_text('Gesynchroniseerd', timeout=15000)
                page.reload()
                expect(page.locator('#queue-count')).to_have_text('0')
                page.locator('#sync-now').click()
                detail = page.request.get(base + '/api/cultivations/1').json()
                assert len(detail['measurements']) == 1, detail
                assert len(detail['measurements'][0]['stems']) == 5
                # Real file upload, server preview, explicit confirm, durable status/history.
                page.goto(base + '/#imports')
                csv = ('label;pcu;type_1;idx_1;type_2;idx_2;startdate;enddate;value\n'
                       'Ave_24h_CompTemp;SYNTHETIC;Afdeling;1;;;16-11-26;16-11-26;20,5\n')
                page.locator('#files').set_input_files({'name': 'synthetic.csv', 'mimeType': 'text/csv', 'buffer': csv.encode()})
                page.get_by_role('button', name='Uploaden & voorbeeld maken').click()
                expect(page.locator('#import-detail')).to_be_visible()
                for _ in range(40):
                    if page.locator('#confirm-import').count():
                        break
                    page.locator('#refresh-job').click()
                    page.wait_for_timeout(100)
                expect(page.locator('#confirm-import')).to_be_visible()
                page.locator('#confirm-import').click()
                for _ in range(40):
                    data = page.request.get(base + '/api/imports/1').json()
                    if data['status'] == 'completed':
                        break
                    page.wait_for_timeout(100)
                assert data['status'] == 'completed', data
                page.locator('#close-modal').click()
                page.reload()
                expect(page.locator('#import-list')).to_contain_text('Voltooid')
                # Climate, forecasting, administration, cards all render without errors.
                for route, heading in [('climate','Klimaat & licht'), ('models','Gewichtsverwachting'), ('manage','Bedrijfsinstellingen'), ('cards','Teeltkaarten & scans')]:
                    page.goto(base + '/#' + route)
                    expect(page.get_by_role('heading', name=heading, exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), route
                # Settings and calibration are practical forms, with real audited storage.
                page.goto(base + '/#manage')
                expect(page.locator('#settings-form')).to_be_visible()
                page.get_by_label('Tolerantie meetmoment (dagen)').fill('2')
                page.get_by_label('Reden voor wijziging', exact=True).fill('Synthetische instellingentest')
                page.get_by_role('button', name='Instellingen als nieuwe versie bewaren').click()
                expect(page.locator('#general-settings')).to_contain_text('Versiehistorie (1)')
                assert page.request.get(base + '/api/settings').json()['settings']['measurement_tolerance_days'] == 2
                page.reload()
                expect(page.get_by_label('Tolerantie meetmoment (dagen)')).to_have_value('2')
                assert page.locator('#settings-form input[type=number]').count() == 16
                page.locator('#new-calibration').click()
                page.locator('#modal').get_by_label('Geldig vanaf', exact=True).fill('2026-01-01')
                page.locator('#modal').get_by_label('Exact Priva-label', exact=True).fill('Ave_24h_CompTemp')
                page.locator('#modal').get_by_label('Bevestigde eenheid', exact=True).fill('°C')
                page.locator('#modal').get_by_label('Hoe is dit gecontroleerd?', exact=True).fill('Synthetische broncontrole')
                page.locator('#modal').locator('[name=confirmed]').check()
                page.get_by_role('button', name='Validatie bewaren').click()
                expect(page.locator('#modal')).not_to_be_visible()
                assert len(page.request.get(base + '/api/calibrations').json()) == 1
                # A save starts a management refresh; navigate while its settings panel response is held.
                expect(page.locator('#settings-form')).to_be_visible()
                settings_requests = 0
                def navigate_before_settings_response(route):
                    nonlocal settings_requests
                    settings_requests += 1
                    response = route.fetch()
                    # First call refreshes base data; second is manage() awaiting settingsPanel().
                    if settings_requests == 2:
                        page.evaluate("location.hash='cards'")
                    route.fulfill(response=response)
                page.route('**/api/settings', navigate_before_settings_response)
                page.locator('#new-variety').click()
                page.locator('#modal').get_by_label('Soortnaam',exact=True).fill('synthetische-racesoort')
                page.get_by_role('button',name='Soort opslaan',exact=True).click()
                expect(page.get_by_role('heading',name='Teeltkaarten & scans',exact=True)).to_be_visible()
                page.wait_for_timeout(100)
                page.unroute('**/api/settings', navigate_before_settings_response)
                assert not errors, errors
                # A photograph is only a draft until a human reviews and confirms it.
                page.goto(base + '/#cards')
                image = io.BytesIO()
                Image.new('RGB',(100,100),'white').save(image,format='PNG')
                page.locator('#scan-form').get_by_label('Kapteelt', exact=True).select_option('1')
                page.locator('#scan-file').set_input_files({'name':'synthetic-card.png','mimeType':'image/png','buffer':image.getvalue()})
                page.get_by_role('button',name='Foto uploaden & controleren').click()
                expect(page.locator('#review-form')).to_be_visible(timeout=20000)
                page.locator('#add-scan-event').click()
                page.locator('#modal').get_by_label('Datum / tijd',exact=True).fill('2026-11-16T10:00')
                page.locator('#modal').get_by_label('Gebeurtenis / middel',exact=True).fill('Handmatige teeltkaartcontrole')
                page.locator('#modal').get_by_label('Reden / opmerking',exact=True).fill('Overgenomen uit synthetische kaart')
                page.get_by_role('button',name='Conceptreview opslaan').click()
                expect(page.locator('#toast')).to_contain_text('Reviewconcept opgeslagen')
                assert page.request.get(base+'/api/cultivations/1').json()['events'] == []
                page.locator('#confirm-scan').click()
                expect(page.locator('#modal')).not_to_be_visible()
                assert len(page.request.get(base+'/api/cultivations/1').json()['events']) == 1
                # Store distinct harvest classes through actual input fields, never average them.
                page.goto(base + '/#cultivation/1')
                page.locator('#harvest-btn').click()
                page.locator('#modal').get_by_label('Aantal geoogste takken',exact=True).fill('150')
                page.locator('#add-harvest-class').click()
                page.locator('#class_name_0').fill('Klasse I')
                page.locator('#class_weight_0').fill('95')
                page.locator('#class_count_0').fill('100')
                page.locator('#add-harvest-class').click()
                page.locator('#class_name_1').fill('Klasse II')
                page.locator('#class_weight_1').fill('65')
                page.locator('#class_count_1').fill('50')
                page.get_by_role('button',name='Oogst opslaan',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                harvest=page.request.get(base+'/api/cultivations/1').json()['harvests'][0]
                assert harvest['weight_g'] is None and len(harvest['quality_classes']) == 2
                # Verify setup has closed, and local data cannot be sent as another account.
                res = page.request.post(base + '/api/users', data={'username':'reader','password':'synthetic reader password','role':'lezer'})
                assert res.ok, res.text()
                page.locator('#logout').click()
                expect(page.get_by_role('heading', name='Aanmelden')).to_be_visible()
                page.get_by_label('Gebruikersnaam').fill('reader')
                page.get_by_label('Wachtwoord', exact=True).fill('synthetic reader password')
                page.get_by_role('button', name='Inloggen').click()
                page.goto(base + '/#measure/1')
                expect(page.get_by_text('Je account heeft alleen leesrechten.', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='Meting veilig bewaren')).to_be_disabled()
                expect(page.locator('#queue-count')).to_have_text('0')
                assert page.request.post(base + '/api/varieties', data={'name':'forbidden'}).status == 403
                assert not errors, errors
                browser.close()
            print('Browser checks passed: mobile setup/planning, five stems, offline draft/reload/idempotent sync, CSV preview/confirm/history, honest forecast/season, settings reload, calibration, scan review/confirm, separate harvest classes and reader isolation.')
        except Exception:
            logfile.flush(); logfile.seek(0)
            print(logfile.read()[-7000:], file=sys.stderr)
            if 'errors' in locals():
                print('Browser page errors:', errors, file=sys.stderr)
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
            logfile.close()


if __name__ == '__main__':
    run()
