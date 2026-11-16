"""Real browser acceptance checks against an isolated temporary server/database.
Run: python tests/browser_smoke.py (install Playwright Chromium first).
Only synthetic fixtures are used. No shared developer database is modified.
"""
from __future__ import annotations
import os
import re
import io
from PIL import Image
from openpyxl import Workbook
from datetime import datetime, timedelta
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
                page.get_by_role('link',name='Uitleg voorspelling',exact=True).click()
                expect(page.get_by_role('heading',name='Zo werkt de voorspelling',exact=True)).to_be_visible()
                expect(page.locator('.model-guide')).to_contain_text('Priva gaat altijd voor.')
                expect(page.locator('.guide-example')).to_contain_text('geen echte voorspelling')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('link',name='Naar de prognoses →',exact=True).click()
                expect(page.get_by_role('heading',name='Gewichtsverwachting',exact=True)).to_be_visible()
                page.get_by_role('link',name='Hoe werkt de voorspelling?',exact=True).click()
                expect(page.get_by_role('heading',name='Zo werkt de voorspelling',exact=True)).to_be_visible()
                page.goto(base+'/#dashboard')
                expect(page.get_by_role('heading',name='Goed zicht op je teelt')).to_be_visible()
                page.locator('#new-cultivation').click()
                modal = page.locator('#modal')
                modal.get_by_label('Ronde', exact=True).fill('Synthetische winterronde')
                modal.get_by_label('Kap', exact=True).select_option('1')
                modal.get_by_label('Plantdatum', exact=True).fill('2026-10-01')
                modal.get_by_label('Licht uit', exact=True).fill('2026-10-20')
                modal.get_by_label('Verwachte oogst', exact=True).fill('2027-01-10')
                modal.get_by_label('Plantdichtheid (takken/m²)').fill('54')
                modal.get_by_label('Gepland aantal takken (optioneel)',exact=True).fill('1000')
                modal.get_by_label('Geplande branduren per dag (optioneel)',exact=True).fill('8')
                modal.get_by_label('Tussenlicht (optioneel)',exact=True).fill('Synthetisch tussenlichtschema')
                modal.get_by_label('Belichtingsschema / strategie (optioneel)',exact=True).fill('08:00–16:00')
                modal.get_by_role('button', name='Kapteelt opslaan').click()
                expect(page.get_by_role('heading', name='Kap 1 · antonov')).to_be_visible()
                expect(page.get_by_text(re.compile(r'De dagen worden (langer|korter)|Rond het keerpunt van de daglengte'), exact=False)).to_be_visible()
                expect(page.get_by_text('Nog geen betrouwbare gewichtsverwachting beschikbaar.', exact=False)).to_be_visible()
                plan=page.request.get(base + '/api/cultivations/1').json()['planning'][0]
                assert plan['density'] == 54 and plan['planned_stems'] == 1000 and plan['planned_lamp_hours'] == 8
                assert 'Synthetisch tussenlichtschema' in page.request.get(base+'/api/cards?ids=1').text()
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
                page.locator('#settings-form').get_by_text('Model & vrijgave',exact=True).click()
                page.get_by_label('Welke invoer mag het model gebruiken?',exact=True).select_option('climate')
                page.get_by_label('Minimale dekking klimaat en licht (fractie)',exact=True).fill('0.95')
                page.get_by_label('Reden voor wijziging', exact=True).fill('Synthetische instellingentest')
                page.get_by_role('button', name='Instellingen als nieuwe versie bewaren').click()
                expect(page.locator('#general-settings')).to_contain_text('Versiehistorie (1)')
                assert page.request.get(base + '/api/settings').json()['settings']['measurement_tolerance_days'] == 2
                page.reload()
                expect(page.get_by_label('Tolerantie meetmoment (dagen)')).to_have_value('2')
                settings=page.request.get(base+'/api/settings').json()['settings']
                assert settings['model_feature_profile']=='climate' and settings['environment_min_coverage']==.95
                expect(page.locator('#model_feature_profile')).to_have_value('climate')
                expect(page.locator('#environment_min_coverage')).to_have_value('0.95')
                page.locator('#new-crop-profile').click()
                page.locator('#profile_variety_id').select_option('1')
                page.locator('#season_start_month').select_option('10')
                page.locator('#season_start_day').fill('1')
                page.locator('#season_end_month').select_option('02')
                page.locator('#season_end_day').fill('28')
                page.locator('#profile_valid_from').fill('2026-01-01')
                page.locator('#curve_kd_0').fill('10')
                page.locator('#curve_length_0').fill('35')
                page.locator('#curve_kd_1').fill('30')
                page.locator('#curve_length_1').fill('45')
                page.locator('#profile_source').fill('Synthetische vastgelegde streeflengten')
                page.get_by_role('button',name='Profielversie opslaan',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                assert len(page.request.get(base+'/api/crop-profiles').json()) == 1
                page.goto(base+'/#cultivation/1')
                expect(page.locator('#growth-target')).to_contain_text('KD10-doel 35 cm')
                growth=page.request.get(base+'/api/cultivations/1/growth-target').json()
                assert growth['comparisons'][0]['mean_length_cm']==42
                expect(page.locator('#environment-panel')).to_contain_text('Onbekend')
                expect(page.locator('#environment-panel')).to_contain_text('actuele waarnemingen')
                expect(page.locator('#forecast-history')).to_contain_text('invoerpeildatum')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).slice(0,35)")
                page.goto(base+'/#manage')
                expect(page.locator('#new-calibration')).to_be_visible()
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
                # XLSX upload only previews: mapping + explicit row correction precede writes.
                page.goto(base+'/#imports/history')
                expect(page.get_by_role('heading',name='Historische teelten uit Excel',exact=True)).to_be_visible()
                workbook=Workbook(); sheet=workbook.active; sheet.title='Historie'
                sheet.append(['Synthetische broninformatie'])
                sheet.append(['Ronde','Kap','Soort','Plantdatum','Licht uit','Begin oogst','Leegdatum','Gewicht','Dichtheid',None])
                sheet.append(['Historie 2025',2,'a','2025-10-01','2025-10-20','2026-01-08','2026-01-10',105,54,6])
                sheet.append(['Historie 2025',3,'t','01-10-25','2025-10-20','2026-01-08','2026-01-10',106,54,6])
                contents=io.BytesIO();workbook.save(contents)
                count_before=len(page.request.get(base+'/api/cultivations').json())
                page.locator('#history-file').set_input_files({'name':'synthetic-history.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':contents.getvalue()})
                page.get_by_role('button',name='Bestanden uploaden & bekijken',exact=True).click()
                expect(page.locator('#history-mapping-form')).to_be_visible()
                page.locator('#history-header-row').fill('2')
                page.locator('#history-header-row').press('Tab')
                expect(page.locator('#history-source-preview')).to_contain_text('A · Ronde')
                for key,column in [('round_name','A'),('bay_number','B'),('variety','C'),('plant_date','D'),('light_out_date','E'),('harvest_start_date','F'),('harvest_end_date','G'),('weight_g','H'),('density','I'),('beds','J')]:
                    page.locator('#map-'+key).select_option(column)
                page.get_by_role('button',name='Kolomkoppeling controleren',exact=True).click()
                expect(page.locator('#confirm-history')).to_be_disabled()
                assert len(page.request.get(base+'/api/cultivations').json())==count_before
                page.locator('#history-correct-row').select_option('4')
                page.locator('#correct_plant_date').fill('2025-10-01')
                page.locator('#correction-reason').fill('Volledig jaartal gecontroleerd in bronregistratie')
                page.get_by_role('button',name='Correctie toepassen & opnieuw controleren',exact=True).click()
                expect(page.locator('#confirm-history')).to_be_enabled()
                assert len(page.request.get(base+'/api/cultivations').json())==count_before
                page.locator('#confirm-history').click()
                expect(page.locator('#history-row-preview')).to_contain_text('Bevestigd · teelten geregistreerd')
                assert len(page.request.get(base+'/api/cultivations').json())==count_before+2
                page.locator('#close-modal').click()
                page.reload()
                expect(page.locator('#main')).to_contain_text('synthetic-history.xlsx')
                expect(page.locator('#main')).to_contain_text('Bevestigd')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Descriptive comparisons keep unsupported control differences unknown.
                page.goto(base+'/#models')
                expect(page.locator('#comparison-form')).to_be_visible()
                page.locator('#comparison-round').select_option('Historie 2025')
                page.get_by_role('button',name='Groepen bekijken',exact=True).click()
                expect(page.locator('#comparison-result')).to_contain_text('Onvoldoende onafhankelijke controlekappen')
                expect(page.locator('#comparison-result')).to_contain_text('geen middelenadvies')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # A goal-only seasonal version requires no invented length curve.
                page.goto(base+'/#manage')
                page.locator('#new-crop-profile').click()
                page.locator('#profile_variety_id').select_option('3')
                page.locator('#season_start_month').select_option('10'); page.locator('#season_start_day').fill('1')
                page.locator('#season_end_month').select_option('02'); page.locator('#season_end_day').fill('28')
                page.locator('#profile_valid_from').fill('2026-01-01')
                page.locator('#clear-curve-points').click()
                page.locator('#profile_target_weight').fill('110')
                page.locator('#profile_source').fill('Synthetisch seizoensdoel zonder lengtecurve')
                page.get_by_role('button',name='Profielversie opslaan',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                profiles=page.request.get(base+'/api/crop-profiles').json()
                assert any(profile['variety_id']==3 and profile['length_curve']==[] and profile['target_weight_g']==110 for profile in profiles)
                # Dated default goals are reviewable after a real edit.
                page.locator('[data-variety="1"]').click()
                expect(page.locator('#variety-target-history')).to_contain_text('Doelgewichthistorie (')
                page.locator('#modal').get_by_label('Doelgewicht per tak (g)',exact=True).fill('106')
                page.get_by_role('button',name='Bewaren',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                targets=page.request.get(base+'/api/varieties/1/targets').json()
                assert any(target['target_weight_g']==106 for target in targets) and len(targets)>=2
                # Real-format fixtures: recognize two company books, never trust filename years.
                weight_book=Workbook(); schema=weight_book.active; schema.title='teeltschema'
                for column,value in {1:'kap',2:'planten',7:'licht uit',16:'leeg',19:'gewicht'}.items(): schema.cell(1,column,value)
                for row,bay,weight in [(2,1,105),(3,2,'95/65'),(4,3,110),(5,4,104)]:
                    for column,value in {1:bay,2:10,3:1,7:12,8:1,16:20,17:1,19:weight}.items(): schema.cell(row,column,value)
                light_sheet=weight_book.create_sheet('data')
                light_sheet['B1']='Gem. licht%'; light_sheet['B2']='SYNTHETIC:MOD. LICHT 101'; light_sheet['B3']='%'
                for index in range(576):
                    light_sheet.cell(index+4,1,datetime(2025,1,1)+timedelta(minutes=5*index)); light_sheet.cell(index+4,2,50)
                weight_bytes=io.BytesIO();weight_book.save(weight_bytes)
                program_book=Workbook();program_book.remove(program_book.active)
                for number in range(1,6):
                    program=program_book.create_sheet('Ronde '+str(number))
                    for cell,value in {'C8':'Ronde '+str(number),'D10':'schema','E10':'pl datum','H10':'licht uit','N10':'bloei','C11':'vak','E11':'w/d'}.items(): program[cell]=value
                    if number==1:
                        for row,bay,code in [(12,1,'6a'),(13,2,'6p'),(14,3,'3a3t'),(15,4,'6a')]:
                            for column,value in {3:bay,4:code,5:11 if bay==4 else 10,6:1,7:14,8:12,9:1,13:54,14:20,15:1,18:'<img src=x onerror=alert(1)>'}.items(): program.cell(row,column,value)
                program_bytes=io.BytesIO();program_book.save(program_bytes)
                page.goto(base+'/#imports/history')
                page.locator('#history-file').set_input_files([
                    {'name':'synthetic-weight-2022.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':weight_bytes.getvalue()},
                    {'name':'synthetic-program-2022.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':program_bytes.getvalue()}])
                page.get_by_role('button',name='Bestanden uploaden & bekijken',exact=True).click()
                expect(page.locator('#company-import-form')).to_be_visible(timeout=20000)
                sources=page.request.get(base+'/api/history-imports').json()['imports']
                weights=next(source for source in sources if source['filename']=='synthetic-weight-2022.xlsx')
                program=next(source for source in sources if source['filename']=='synthetic-program-2022.xlsx')
                assert weights['profile']=='legacy_weight_workbook' and program['profile']=='legacy_program_workbook'
                page.locator('#company-weight').select_option(str(weights['id'])); page.locator('#company-program').select_option(str(program['id']))
                before_company=len(page.request.get(base+'/api/cultivations').json())
                page.get_by_role('button',name='Bedrijfsvoorbeeld voorbereiden',exact=True).click()
                expect(page.locator('#legacy-config-form')).to_be_visible(timeout=20000)
                expect(page.locator('[data-year-round="Ronde 1"]')).to_have_value('')
                expect(page.locator('#legacy-confirm')).to_be_disabled()
                assert len(page.request.get(base+'/api/cultivations').json())==before_company
                page.locator('#legacy-bulk-year').fill('2025')
                page.locator('#legacy-bulk-year-rounds').select_option(['Ronde 1'])
                page.locator('#legacy-year-fill').click()
                expect(page.locator('[data-year-round="Ronde 1"]')).to_have_value('2025')
                page.locator('#legacy-weekday').select_option('iso_monday_1')
                page.locator('#legacy-config-form [name=years_confirmed]').check()
                page.get_by_role('button',name='Jaarankers en koppelingen opnieuw controleren',exact=True).click()
                expect(page.locator('#legacy-company-rows')).to_contain_text('95 g / 65 g')
                page.locator('#legacy-review-row').select_option('4')
                page.locator('#legacy-row-form [name=exclude]').check()
                page.locator('#legacy-row-reason').fill('Gemengde antonov/tutu bron niet zuiver toe te wijzen')
                with page.expect_response(lambda response: response.url.endswith('/legacy-preview') and response.request.method=='POST'):
                    page.get_by_role('button',name='Rijcorrectie opnieuw controleren',exact=True).click()
                expect(page.locator('#legacy-company-rows').get_by_text('Uitgesloten',exact=True)).to_be_visible()
                expect(page.locator('#legacy-confirm')).to_be_disabled()
                page.locator('#legacy-review-row').select_option('5')
                page.locator('#legacy-row-form [name=accept_anchor_mismatch]').check()
                page.locator('#legacy-row-reason').fill('Tegenstrijdige plantweek gecontroleerd aan aanvullende synthetische bron')
                page.get_by_role('button',name='Rijcorrectie opnieuw controleren',exact=True).click()
                try:
                    expect(page.locator('#legacy-confirm')).to_be_enabled()
                except Exception:
                    diagnostic=page.request.get(base+f"/api/history-imports/{weights['id']}").json()
                    print('Company preview diagnostics:',[(row['row_number'],row['status'],row['reasons']) for row in diagnostic['preview']['rows']],file=sys.stderr)
                    print('Saved company overrides:',diagnostic.get('mapping',{}).get('overrides'),file=sys.stderr)
                    raise
                assert page.locator('#legacy-company-detail img').count()==0
                assert len(page.request.get(base+'/api/cultivations').json())==before_company
                page.locator('#legacy-confirm').click()
                expect(page.locator('#legacy-company-rows')).to_contain_text('Bevestigd · bronkoppelingen bewaard')
                company_crops=[crop for crop in page.request.get(base+'/api/cultivations').json() if crop['round_name']=='Ronde 1']
                assert len(company_crops)==3
                class_crop=next(crop for crop in company_crops if crop['bay_number']==2)
                class_harvest=page.request.get(base+f"/api/cultivations/{class_crop['id']}").json()['harvests'][0]
                assert class_harvest['weight_g'] is None and sorted(item['weight_g'] for item in class_harvest['quality_classes'])==[65,95]
                # Reopen confirmed evidence without duplicating its previously imported crops.
                with page.expect_response(lambda response:'/new-review' in response.url and response.request.method=='POST') as reopened:
                    page.locator('[data-history-new-review]').click()
                reopened_id=reopened.value.json()['id']
                assert reopened_id!=weights['id']
                expect(page.locator('#legacy-company-rows')).to_contain_text('Al geïmporteerd: 3')
                assert len([crop for crop in page.request.get(base+'/api/cultivations').json() if crop['round_name']=='Ronde 1'])==3
                page.locator('#close-modal').click()
                # The big data sheet has its own durable supplemental import and explicit interpretation.
                page.goto(base+'/#imports/lighting')
                page.locator('#lighting-source-book').select_option(str(weights['id']))
                page.get_by_role('button',name='Aanvullende lichtbron verkennen',exact=True).click()
                expect(page.locator('#lighting-interpretation-form')).to_be_visible(timeout=20000)
                page.locator('#lighting-timezone').select_option('Europe/Amsterdam')
                page.locator('#lighting-date-system').select_option('1900')
                page.locator('#lighting-sample-seconds').fill('300')
                page.locator('#lighting-unit').select_option('%')
                page.locator('#lighting-timestamp-role').select_option('interval_start')
                page.locator('#lighting-value-kind').select_option('linear_dim_percent')
                page.locator('#lighting-reason').fill('Synthetische vijfminutenbron met lineair 50 procent expliciet geverifieerd')
                page.locator('#lighting-interpretation-form [name=confirmed]').check()
                page.get_by_role('button',name='Broninterpretatie bevestigen & verwerken',exact=True).click()
                expect(page.locator('#lighting-calibrate')).to_be_visible(timeout=20000)
                page.locator('#lighting-calibrate').click()
                expect(page.locator('#lamp-source')).to_have_value('legacy_excel')
                expect(page.locator('#dim_factor')).to_have_value('1')
                expect(page.locator('#channel')).to_have_value('')
                page.locator('#valid_from').fill('2024-01-01');page.locator('#object_number').fill('101');page.locator('#ppfd').fill('120')
                page.locator('#calibration-form [name=independent_contribution]').check()
                page.locator('#reason').fill('Synthetisch werkelijk zelfstandig lampvermogen zonder dubbel dimmen')
                page.locator('#calibration-form [name=confirmed]').check()
                page.get_by_role('button',name='Validatie bewaren',exact=True).click()
                expect(page.locator('#modal')).not_to_be_visible()
                # Even an empty Priva hours value has absolute priority over the Excel day/module.
                page.goto(base+'/#imports')
                overlap=('label;pcu;type_1;idx_1;type_2;idx_2;startdate;enddate;value\n'
                         'Sum_24h_ModGrowlightSysHours;SYNTHETIC;modgroeilichtsturing;101;kanaal;1;01-01-25;01-01-25;\n')
                page.locator('#files').set_input_files({'name':'synthetic-primary-hours.csv','mimeType':'text/csv','buffer':overlap.encode()})
                page.get_by_role('button',name='Uploaden & voorbeeld maken').click()
                expect(page.locator('#import-detail')).to_be_visible()
                for _ in range(50):
                    if page.locator('#confirm-import').count(): break
                    page.locator('#refresh-job').click();page.wait_for_timeout(100)
                page.locator('#confirm-import').click()
                for _ in range(50):
                    imports=page.request.get(base+'/api/imports').json()
                    if all(job['status']=='completed' for job in imports): break
                    page.wait_for_timeout(100)
                page.locator('#close-modal').click()
                report=page.request.get(base+'/api/legacy-lighting/daily?start=2025-01-01&end=2025-01-02&modules=101').json()
                assert report['days']['2025-01-01']['101']['quality']=='blocked_by_priva',report
                assert report['days']['2025-01-01']['101']['mol_m2_day'] is None
                assert abs(report['days']['2025-01-02']['101']['mol_m2_day']-5.184)<.001,report
                page.goto(base+'/#imports/lighting')
                page.locator('[data-lighting-job]').first.click()
                expect(page.locator('#lighting-daily-form')).to_be_visible()
                page.get_by_role('button',name='Dagrapport bekijken',exact=True).click()
                expect(page.locator('#lighting-daily-result')).to_contain_text('Priva gaat voor; Excel niet gebruikt')
                expect(page.locator('#lighting-daily-result')).to_contain_text('Geverifieerde aanvullende Excelbron')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#lighting-revoke').click()
                expect(page.locator('#lighting-job-detail')).to_contain_text('Herroepen')
                page.locator('#close-modal').click()
                # Full-size synthetic layout: all paired and extra planning rows remain visible.
                bulk_weights=Workbook(); ws=bulk_weights.active; ws.title='teeltschema'
                bulk_weights.create_sheet('data')
                for column,value in {1:'kap',2:'planten',7:'licht uit',16:'leeg',19:'gewicht'}.items(): ws.cell(1,column,value)
                n=2
                for number in range(7,28):
                    for bay in range(1,41):
                        for column,value in {1:bay,2:number+5,3:1,7:number+7,8:1,16:number+14,17:1}.items(): ws.cell(n,column,value)
                        if bay%4: ws.cell(n,19,105)
                        n+=1
                bulk_program=Workbook();bulk_program.remove(bulk_program.active)
                for number in range(1,35):
                    ws=bulk_program.create_sheet('Ronde '+str(number))
                    for cell,value in {'C8':'Ronde '+str(number),'D10':'schema','E10':'pl datum','H10':'licht uit','N10':'bloei','C11':'vak','E11':'w/d'}.items(): ws[cell]=value
                    for bay in range(1,41):
                        for column,value in {3:bay,4:'a',5:number+5,6:1,7:14,8:number+7,9:1,13:54,14:number+14,15:1}.items(): ws.cell(bay+11,column,value)
                bulk_books=[]
                for book,name in [(bulk_weights,'synthetic-bulk-weight.xlsx'),(bulk_program,'synthetic-bulk-program.xlsx')]:
                    buffer=io.BytesIO();book.save(buffer)
                    response=page.request.post(base+'/api/history-imports',multipart={'file':{'name':name,'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':buffer.getvalue()}})
                    assert response.ok,response.text();bulk_books.append(response.json())
                bulk_weight,bulk_program_source=bulk_books
                response=page.request.post(base+f"/api/history-imports/{bulk_weight['id']}/legacy-preview",data={'program_import_id':bulk_program_source['id'],'weekday_convention':'iso_monday_1','round_years':{'Ronde '+str(i):2024 for i in range(1,35)}})
                assert response.ok,response.text()
                assert response.json()['summary']['total_rows']==1360
                assert response.json()['summary']['ambiguous']==630
                before_bulk=len(page.request.get(base+'/api/cultivations').json())
                page.goto(base+'/#imports/history')
                page.locator('#company-weight').select_option(str(bulk_weight['id']));page.locator('#company-program').select_option(str(bulk_program_source['id']))
                page.get_by_role('button',name='Bedrijfsvoorbeeld voorbereiden',exact=True).click()
                expect(page.locator('#legacy-row-accounting')).to_contain_text('1.360')
                expect(page.locator('.history-row-tools')).to_contain_text('1–200 van 1.360')
                host=page.locator('.history-bulk-host')
                expect(host.locator('[data-state="status"]')).to_be_visible()
                host.locator('[data-state="status"]').select_option('ambiguous')
                host.locator('[data-state="field-beds"]').check()
                host.locator('[data-state="value-beds"]').fill('6')
                host.locator('[data-state="reason"]').fill('Zes bedden voor alle gelijke enkelsoortige bronregels gecontroleerd')
                with page.expect_response(lambda res:'/bulk-review' in res.url and res.request.method=='POST') as dry_response:
                    host.locator('[data-inspect]').click()
                assert dry_response.value.json()['bulk_review']['matched_rows']==630
                assert dry_response.value.json()['bulk_review']['changed_rows']==630
                expect(host.locator('[data-apply]')).to_be_enabled()
                with page.expect_response(lambda res:'/bulk-review' in res.url and res.request.method=='POST'):
                    host.locator('[data-apply]').click()
                expect(page.locator('#legacy-row-accounting')).to_contain_text('630 geldig')
                assert len(page.request.get(base+'/api/cultivations').json())==before_bulk
                # Checkboxes persist across page boundaries and the final page is reachable.
                page.locator('[data-history-select]').first.check()
                page.locator('[data-page="last"]').click()
                expect(page.locator('.history-row-tools')).to_contain_text('1.201–1.360 van 1.360')
                page.locator('[data-history-select]').first.check()
                expect(page.locator('.history-selection-count')).to_contain_text('2 regels geselecteerd')
                expect(page.locator('#legacy-company-rows')).to_contain_text('Ronde 34')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#close-modal').click()
                # Verify setup has closed, and local data cannot be sent as another account.
                res = page.request.post(base + '/api/users', data={'username':'reader','password':'synthetic reader password','role':'lezer'})
                assert res.ok, res.text()
                page.locator('#logout').click()
                expect(page.get_by_role('heading', name='Aanmelden')).to_be_visible()
                expect(page.locator('#modal > *')).to_have_count(0)
                page.get_by_label('Gebruikersnaam').fill('reader')
                page.get_by_label('Wachtwoord', exact=True).fill('synthetic reader password')
                page.get_by_role('button', name='Inloggen').click()
                page.goto(base+'/#model-uitleg')
                expect(page.get_by_role('heading',name='Zo werkt de voorspelling',exact=True)).to_be_visible()
                page.set_viewport_size({'width':1280,'height':800})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.goto(base + '/#measure/1')
                expect(page.get_by_text('Je account heeft alleen leesrechten.', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='Meting veilig bewaren')).to_be_disabled()
                expect(page.locator('#queue-count')).to_have_text('0')
                assert page.request.post(base + '/api/varieties', data={'name':'forbidden'}).status == 403
                assert not errors, errors
                browser.close()
            print('Browser checks passed: 1360-row reconciliation, cross-page bulk630, year batch, confirmed-review recovery, planning/cards, offline five-stem idempotency, Priva-first supplemental lighting, paired company years/classes, generic Excel review/confirm, climate-profile settings reload, seasonal curve/growth/environment, forecast history, scan confirmation, harvest classes, mobile overflow and reader/navigation isolation.')
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
