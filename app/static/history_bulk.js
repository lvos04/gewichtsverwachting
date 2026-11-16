/** Shared, revision-checked bulk staging for generic and paired workbook reviews. */
let instanceNumber = 0;
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const FIELD_DEFINITIONS = [
  {key:'beds',label:'Aantal bedden',type:'number',min:1,max:6,step:1,broad:true},
  {key:'density',label:'Plantdichtheid (takken/m²)',type:'number',min:0.1,max:1000,step:0.1,broad:true},
  {key:'planned_stems',label:'Gepland aantal takken',type:'number',min:0,step:1,broad:true},
  {key:'variety',label:'Soort / soortbedden',type:'text',hint:'Bijvoorbeeld een gecontroleerde soortnaam of 3a3p.'},
  {key:'plant_date',label:'Plantdatum',type:'date'},
  {key:'light_out_date',label:'Licht uit',type:'date'},
  {key:'harvest_start_date',label:'Begin oogst',type:'date'},
  {key:'harvest_end_date',label:'Leegdatum / einde oogst',type:'date'},
  {key:'weight_g',label:'Eindgewicht / gewichtsklassen (g)',type:'text',hint:'95/65 blijft twee klassen. Vul geen berekend gemiddelde in.'},
  {key:'stem_count',label:'Aantal geoogste takken',type:'number',min:0,step:1},
  {key:'loss_fraction',label:'Uitval (fractie 0–1)',type:'number',min:0,max:1,step:0.001},
  {key:'historical_alar_total',label:'Historisch alartotaal',type:'number',min:0,step:0.01},
  {key:'historical_alar_unit',label:'Eenheid historisch alartotaal',type:'text'},
];
const STATUS_LABELS = {valid:'Geldig',ambiguous:'Te beoordelen',excluded:'Uitgesloten',duplicate:'Al geïmporteerd'};
const SKIP_LABELS = {already_confirmed:'Al bevestigde bronrijen overgeslagen',existing_value_preserved:'Bestaande waarden behouden',no_anchor_mismatch:'Geen tegenstrijdig plantanker',not_planning_only:'Geen uitgesloten planningsregel',no_program_round_title_conflict:'Geen afwijkende programmarondetitel',no_change:'Bronrijen zonder wijziging'};

async function defaultRequest(url, options = {}) {
  const response = await fetch(url, {credentials:'same-origin', ...options,
    headers:{'Content-Type':'application/json',...options.headers},
    body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail = data?.detail;
    const error = new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg).join(' · ') : `Verzoek mislukt (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

/** Returns synchronously. destroy() invalidates all pending UI continuations. */
export function installHistoryBulk({container, importId, mode = 'generic', getSelectedRows = () => [], onStaged = () => {}, isActive = () => true, request = defaultRequest, canWrite = true} = {}) {
  if (!container || typeof container.querySelector !== 'function') throw new TypeError('Een container voor bulkbeoordeling is vereist.');
  if (!Number.isInteger(Number(importId)) || Number(importId) < 1) throw new TypeError('Een geldig importnummer is vereist.');
  const prefix = `history-bulk-${++instanceNumber}`;
  const endpoint = `/api/history-imports/${Number(importId)}`;
  const company = ['company','legacy','legacy_paired'].includes(mode);
  const find = selector => container.querySelector(selector);
  const findAll = selector => [...container.querySelectorAll(selector)];
  let destroyed = false, generation = 0, busy = false, metadata = null, inspected = null, savedState = {};
  const active = () => !destroyed && container.isConnected && isActive();
  const selectedRows = () => [...new Set((getSelectedRows() || []).map(Number).filter(value => Number.isInteger(value) && value > 0))].sort((a,b) => a-b);
  const id = suffix => `${prefix}-${suffix}`;
  const read = key => find(`[data-state="${key}"]`);
  const message = (text, error = false) => {
    if (!active()) return;
    const element = find('[data-message]');
    if (element) {element.textContent = text; element.className = error ? 'error-text' : 'hint';}
  };
  function saveView() {
    for (const input of findAll('[data-state]')) savedState[input.dataset.state] = input.type === 'checkbox' ? input.checked : input.value;
  }
  function restoreView() {
    for (const input of findAll('[data-state]')) {
      if (!(input.dataset.state in savedState)) continue;
      if (input.type === 'checkbox') input.checked = !!savedState[input.dataset.state];
      else input.value = savedState[input.dataset.state];
    }
  }
  function invalidate() {
    inspected = null;
    const preview = find('[data-preview]');
    if (preview) preview.replaceChildren();
    const apply = find('[data-apply]');
    if (apply) apply.disabled = true;
  }
  function isConcreteScope() {
    if (read('scope')?.value === 'selected') return true;
    return !!(read('group_index')?.value || read('round_name')?.value || read('variety')?.value || /^Ronde\s+\d+$/i.test(read('source_sheet')?.value || ''));
  }
  function updateScope() {
    const selected = read('scope')?.value === 'selected';
    const matching = find('[data-matching]');
    if (matching) matching.hidden = selected;
    const selection = find('[data-selected-summary]');
    if (selection) selection.textContent = `${selectedRows().length.toLocaleString('nl-NL')} aangevinkte bronrijen over alle pagina's.`;
    const concrete = isConcreteScope();
    for (const definition of FIELD_DEFINITIONS) {
      const check = read(`field-${definition.key}`), value = read(`value-${definition.key}`);
      if (!check || !value) continue;
      const allowed = definition.broad || concrete;
      if (!allowed) check.checked = false;
      check.disabled = !canWrite || busy || !allowed;
      check.title = allowed ? '' : 'Kies aangevinkte bronrijen, één ronde, blok of soort om dit gegeven gezamenlijk te beoordelen.';
      value.disabled = !canWrite || busy || !allowed || !check.checked;
      value.required = check.checked && allowed;
    }
    const scopeNote = find('[data-sensitive-note]');
    if (scopeNote) scopeNote.textContent = concrete ? 'Je selectie is concreet genoeg voor gedeelde soorten, datums en gewichten. Kies elk te wijzigen gegeven expliciet.' : 'Voor soorten, datums en gewichten kies je aangevinkte bronrijen of één ronde, blok of soort. Algemene reden-/statusgroepen ondersteunen bedden, dichtheid en geplande takken.';
    const anchorAck = find('[data-anchor-ack]');
    if (anchorAck) anchorAck.hidden = read('accept_anchor_mismatch')?.value !== 'true';
    const titleAck=find('[data-title-ack]');
    if(titleAck)titleAck.hidden=read('confirm_program_round_title')?.value!=='true';
    const overwriteAck = find('[data-overwrite-ack]');
    if (overwriteAck) overwriteAck.hidden = !read('overwrite_existing')?.checked;
    if (!canWrite) findAll('input,select,textarea,button').forEach(element => {if (!element.hasAttribute('data-refresh')) element.disabled = true;});
  }
  function setBusy(value) {
    busy = value;
    container.setAttribute('aria-busy', String(value));
    findAll('input,select,textarea,button').forEach(element => element.disabled = value || !canWrite);
    if (!value) {
      const refresh = find('[data-refresh]'); if (refresh) refresh.disabled = false;
      updateScope();
      const apply = find('[data-apply]'); if (apply) apply.disabled = !canWrite || !inspected || inspected.result.changed_rows < 1;
    }
  }
  function filterOptions(dictionary, label) {
    return `<option value="">Alle ${label}</option>${Object.entries(dictionary || {}).map(([value,count]) => `<option value="${escapeHtml(value)}">${escapeHtml(value)} (${Number(count).toLocaleString('nl-NL')})</option>`).join('')}`;
  }
  function selectField(key, label, options) {
    return `<div class="field"><label for="${id(key)}">${label}</label><select id="${id(key)}" data-state="${key}">${options}</select></div>`;
  }
  function actionOptions() {return '<option value="">Niet wijzigen</option><option value="true">Ja</option><option value="false">Nee</option>';}
  function fieldEditor(definition) {
    return `<div class="field"><label class="inline" for="${id(`field-${definition.key}`)}"><input id="${id(`field-${definition.key}`)}" type="checkbox" data-state="field-${definition.key}"> ${definition.label}</label><input id="${id(`value-${definition.key}`)}" aria-label="Gedeelde waarde: ${definition.label}" data-state="value-${definition.key}" type="${definition.type}" ${definition.min !== undefined ? `min="${definition.min}"` : ''} ${definition.max !== undefined ? `max="${definition.max}"` : ''} ${definition.step !== undefined ? `step="${definition.step}"` : ''} maxlength="2000" disabled>${definition.hint ? `<p class="hint">${definition.hint}</p>` : ''}</div>`;
  }
  function render() {
    if (!active()) return;
    const version = metadata?.review_version_id;
    if (!version) {
      container.innerHTML = `<h3>Gelijke antwoorden gezamenlijk beoordelen</h3><p class="hint">Maak eerst een controlevoorbeeld van dit werkboek. Daarna kun je dezelfde beoordeling toepassen op alle passende bronrijen.</p><button type="button" data-refresh class="small">Groepen opnieuw laden</button><p data-message role="status"></p>`;
      find('[data-refresh]').onclick = refresh;
      return;
    }
    container.innerHTML = `<div class="section-title"><h3>Gelijke antwoorden gezamenlijk beoordelen</h3><button type="button" class="small" data-refresh>↻ Groepen vernieuwen</button></div><p class="hint">Beoordelingsversie #${Number(version)} · ${Number(metadata.total_rows || 0).toLocaleString('nl-NL')} opgeslagen bronrijen. Een groepsselectie werkt over <strong>alle pagina's</strong>.</p>${!canWrite ? '<p class="notice">Je account heeft alleen leesrechten.</p>' : ''}<form data-bulk-form><div class="field"><label for="${id('scope')}">Welke bronrijen krijgen hetzelfde antwoord?</label><select id="${id('scope')}" data-state="scope"><option value="matching">Alle passende bronrijen, over alle pagina's</option><option value="selected">Alleen aangevinkte bronrijen, ook op andere pagina's</option></select></div><p class="hint" data-selected-summary></p><div data-matching><div class="form-grid">${selectField('status','Rijstatus','<option value="">Alle statussen</option>'+Object.entries(metadata.statuses || {}).filter(([status]) => status in STATUS_LABELS).map(([status,count]) => `<option value="${status}">${STATUS_LABELS[status]} (${Number(count).toLocaleString('nl-NL')})</option>`).join(''))}${selectField('reason_contains','Dezelfde beoordelingsreden','<option value="">Alle redenen</option>'+(metadata.reasons || []).map(item => `<option value="${escapeHtml(item.reason)}">${escapeHtml(item.reason)} (${Number(item.count).toLocaleString('nl-NL')})</option>`).join(''))}${selectField('round_name','Ronde',filterOptions(metadata.rounds,'rondes'))}${selectField('group_index','Gewichtsblok',filterOptions(metadata.groups,'blokken'))}${selectField('source_sheet','Bronwerkblad',filterOptions(metadata.source_sheets,'werkbladen'))}${selectField('variety','Bestaande soortcode / soort',filterOptions(metadata.varieties,'soorten'))}</div><p class="hint">Ingevulde filters gelden samen. Kies bijvoorbeeld één reden binnen één ronde.</p></div><h3>Welke antwoorden zijn gelijk?</h3><p class="hint">Vink elk gezamenlijk gegeven aan en vul de gecontroleerde waarde in. Standaard worden alleen lege waarden aangevuld.</p><div class="form-grid">${FIELD_DEFINITIONS.filter(item => item.broad).map(fieldEditor).join('')}</div><details><summary>Soort, datums, gewichten en overige brongegevens</summary><p class="hint" data-sensitive-note></p><div class="form-grid" style="margin-top:14px">${FIELD_DEFINITIONS.filter(item => !item.broad).map(fieldEditor).join('')}</div></details><div class="form-grid">${selectField('exclude','Deze selectie uitsluiten van definitieve import',actionOptions())}${company ? selectField('include_planning','Regels zonder eindgewicht als planning meenemen',actionOptions())+selectField('accept_anchor_mismatch','Tegenstrijdige plantweek/-dag accepteren',actionOptions())+selectField('confirm_program_round_title','Afwijkende rondetitel in het Teeltprogramma accepteren',actionOptions()) : ''}</div>${company ? '<p class="hint">Planning meenemen met een groepsselectie vereist ook een passend redenfilter. Plantjaren per ronde stel je bij de jaarankers in.</p><label class="inline field" data-anchor-ack hidden><input type="checkbox" data-state="anchor_ack"> Ik heb de tegenstrijdige bronankers voor deze volledige selectie gecontroleerd.</label><label class="inline field" data-title-ack hidden><input type="checkbox" data-state="title_ack"> Ik heb de afwijkende programmarondetitel voor deze volledige selectie gecontroleerd.</label>' : ''}<label class="inline field"><input type="checkbox" data-state="overwrite_existing"> Bestaande waarden in de gekozen velden bewust vervangen.</label><p class="hint">Zonder deze keuze blijven bestaande bronwaarden en handmatige veldcorrecties behouden. Met deze keuze kunnen ook eerdere handmatige waarden in de gekozen velden worden vervangen. Andere correctievelden blijven behouden.</p><label class="inline field" data-overwrite-ack hidden><input type="checkbox" data-state="overwrite_ack"> Ik heb gecontroleerd dat de gedeelde waarde ook de bestaande waarden in deze selectie mag vervangen.</label><div class="field"><label for="${id('reason')}">Onderbouwing voor deze gezamenlijke beoordeling</label><textarea id="${id('reason')}" data-state="reason" required minlength="3" maxlength="2000"></textarea></div><button type="submit" class="primary" data-inspect>Controlevoorbeeld van deze actie</button></form><div data-preview class="section"></div><div class="actions"><button type="button" class="primary" data-apply disabled>Op het importvoorbeeld toepassen</button></div><p class="hint">Na toepassen staat een nieuwe beoordelingsversie klaar. Controleer de herberekende regels en gebruik daarna de definitieve importknop van het werkboek.</p><p data-message role="status" aria-live="polite"></p>`;
    restoreView();
    find('[data-refresh]').onclick = refresh;
    find('[data-bulk-form]').onsubmit = inspect;
    find('[data-apply]').onclick = apply;
    const edit = event => {
      if (busy) return;
      if (event.target.matches('[data-state]')) {
        const key=event.target.dataset.state;
        if(['scope','status','reason_contains','group_index','source_sheet','round_name','variety'].includes(key)) {
          for(const ack of ['anchor_ack','title_ack','overwrite_ack'])if(read(ack))read(ack).checked=false;
        }
        if(key==='accept_anchor_mismatch'&&read('anchor_ack'))read('anchor_ack').checked=false;
        if(key==='confirm_program_round_title'&&read('title_ack'))read('title_ack').checked=false;
        if((key==='overwrite_existing'||key.startsWith('field-')||key.startsWith('value-'))&&read('overwrite_ack'))read('overwrite_ack').checked=false;
        invalidate(); updateScope(); saveView();
      }
    };
    find('[data-bulk-form]').addEventListener('input', edit);
    find('[data-bulk-form]').addEventListener('change', edit);
    updateScope();
  }
  function actionValue(key) {const value = read(key)?.value; return value === 'true' ? true : value === 'false' ? false : null;}
  function collect() {
    if (!metadata?.review_version_id) throw new Error('Laad eerst de actuele beoordelingsgroepen.');
    const body = {expected_review_version_id: metadata.review_version_id, fields:{}, overwrite_existing:!!read('overwrite_existing')?.checked, exclude:actionValue('exclude'), accept_anchor_mismatch:company ? actionValue('accept_anchor_mismatch') : null, include_planning:company ? actionValue('include_planning') : null, confirm_program_round_title:company ? actionValue('confirm_program_round_title') : null, reason:read('reason')?.value.trim() || ''};
    if (read('scope')?.value === 'selected') {
      body.row_numbers = selectedRows();
      if (!body.row_numbers.length) throw new Error('Vink eerst de gewenste bronrijen aan. Je selectie mag meerdere pagina’s beslaan.');
    } else {
      body.all_matching = {};
      for (const key of ['status','reason_contains','group_index','source_sheet','round_name','variety']) {
        const value = read(key)?.value;
        if (value) body.all_matching[key] = key === 'group_index' ? Number(value) : value;
      }
      if (!Object.keys(body.all_matching).length) throw new Error('Kies minstens één gedeeld rijkenmerk, zoals reden, ronde of status.');
    }
    for (const definition of FIELD_DEFINITIONS) {
      if (!read(`field-${definition.key}`)?.checked) continue;
      if (!definition.broad && !isConcreteScope()) throw new Error('Kies aangevinkte rijen of één concrete ronde, blok of soort voor dit gegeven.');
      const value = read(`value-${definition.key}`)?.value;
      if (!value?.trim()) throw new Error(`Vul de gedeelde waarde voor ${definition.label.toLowerCase()} in.`);
      if (definition.type === 'number') {
        const numeric = Number(value);
        if (!Number.isFinite(numeric) || definition.step === 1 && !Number.isInteger(numeric)) throw new Error(`Geef een geldig getal voor ${definition.label.toLowerCase()}.`);
        body.fields[definition.key] = numeric;
      } else body.fields[definition.key] = value.trim();
    }
    if (body.reason.length < 3) throw new Error('Onderbouw waarom deze bronrijen hetzelfde antwoord krijgen.');
    if (!Object.keys(body.fields).length && body.exclude === null && body.accept_anchor_mismatch === null && body.include_planning === null && body.confirm_program_round_title === null) throw new Error('Kies minstens één gezamenlijk antwoord of actie.');
    if (body.accept_anchor_mismatch === true && !read('anchor_ack')?.checked) throw new Error('Bevestig dat je de tegenstrijdige bronankers van de volledige selectie hebt gecontroleerd.');
    if(body.confirm_program_round_title===true&&!read('title_ack')?.checked)throw new Error('Bevestig dat je de afwijkende programmarondetitel voor de volledige selectie hebt gecontroleerd.');
    if (body.overwrite_existing && !read('overwrite_ack')?.checked) throw new Error('Bevestig expliciet dat bestaande waarden in de gekozen velden vervangen mogen worden.');
    if (body.include_planning && body.all_matching && !body.all_matching.reason_contains) throw new Error('Kies ook de beoordelingsreden van de uitgesloten planningsregels.');
    return body;
  }
  function showInspection(body, result) {
    const target = find('[data-preview]');
    if (!target) return;
    const fieldRows = Object.entries(body.fields).map(([key,value]) => `<tr><td>${FIELD_DEFINITIONS.find(item => item.key === key)?.label || escapeHtml(key)}</td><td>${escapeHtml(value)}</td></tr>`).join('');
    const actions = [[body.exclude,'Uitsluiten'],[body.include_planning,'Planning meenemen'],[body.accept_anchor_mismatch,'Tegenstrijdige plantankers accepteren'],[body.confirm_program_round_title,'Afwijkende programmarondetitel accepteren']].filter(([value]) => value !== null).map(([value,label]) => `<tr><td>${label}</td><td>${value ? 'Ja' : 'Nee'}</td></tr>`).join('');
    target.innerHTML = `<h3>Controle van deze actie</h3><div class="summary"><div><small>Passende bronrijen</small><strong>${Number(result.matched_rows || 0).toLocaleString('nl-NL')}</strong></div><div><small>Te wijzigen bronrijen</small><strong>${Number(result.changed_rows || 0).toLocaleString('nl-NL')}</strong></div></div><p class="notice">Deze aantallen gelden voor de volledige selectie, ook buiten de huidige pagina. Na toepassen worden alle gewijzigde bronregels opnieuw gevalideerd.</p><div class="table-wrap"><table><thead><tr><th>Gezamenlijk antwoord</th><th>Nieuwe waarde</th></tr></thead><tbody>${fieldRows}${actions}</tbody></table></div><p class="hint">${body.overwrite_existing ? 'Bestaande waarden in deze gekozen velden mogen worden vervangen.' : 'Alleen ontbrekende veldwaarden worden aangevuld; bestaande waarden blijven staan.'}</p><p class="hint">Onderbouwing: ${escapeHtml(body.reason)}</p>${Object.keys(result.skipped || {}).length ? `<details open><summary>Overgeslagen of behouden</summary>${Object.entries(result.skipped).map(([key,count]) => `<p class="hint">${escapeHtml(SKIP_LABELS[key] || key)}: ${Number(count).toLocaleString('nl-NL')}</p>`).join('')}<p class="hint">Een bronrij kan bij meerdere controletellingen voorkomen.</p></details>` : ''}<details><summary>Rijnummers in de gewijzigde selectie</summary><p class="hint">${(result.sample_row_numbers || result.row_numbers?.slice(0,30) || []).map(escapeHtml).join(', ') || 'Geen wijzigingen'}${result.changed_rows > 30 ? ` · eerste 30 van ${Number(result.changed_rows).toLocaleString('nl-NL')}` : ''}</p></details>`;
  }
  async function inspect(event) {
    event?.preventDefault();
    if (!active() || busy || !canWrite) return;
    let body;
    try {body = collect();} catch (error) {message(error.message, true); return;}
    if (!find('[data-bulk-form]').reportValidity()) return;
    invalidate(); message('De volledige selectie wordt gecontroleerd…'); setBusy(true);
    const operation = ++generation;
    try {
      const answer = await request(`${endpoint}/bulk-review`, {method:'POST', body:{...body,dry_run:true}});
      if (!active() || generation !== operation) return;
      const result = answer.bulk_review;
      if (!result) throw new Error('Het controleantwoord bevat geen bulkbeoordeling.');
      if (body.row_numbers && JSON.stringify(body.row_numbers) !== JSON.stringify(selectedRows())) throw new Error('De aangevinkte selectie is ondertussen gewijzigd. Maak een nieuw controlevoorbeeld.');
      inspected = {body, result};
      showInspection(body, result);
      message(result.changed_rows ? 'Controleer deze gedeelde antwoorden en aantallen. Pas ze daarna toe op het importvoorbeeld.' : 'Er zijn geen wijzigingen nodig. Bestaande waarden kunnen behouden of al gelijk zijn.');
    } catch (error) {if (active()) {inspected = null; message(error.message, true);}}
    finally {if (active()) setBusy(false);}
  }
  async function apply() {
    if (!active() || busy || !canWrite || !inspected || inspected.result.changed_rows < 1) return;
    const planned = inspected;
    if (planned.body.row_numbers && JSON.stringify(planned.body.row_numbers) !== JSON.stringify(selectedRows())) {invalidate();message('De aangevinkte selectie is gewijzigd. Maak een nieuw controlevoorbeeld.',true);return;}
    setBusy(true); message('De nieuwe beoordelingsversie wordt opgeslagen…');
    const operation = ++generation;
    try {
      const detail = await request(`${endpoint}/bulk-review`, {method:'POST', body:{...planned.body,dry_run:false}});
      if (!active() || generation !== operation) return;
      const changed = detail.bulk_review?.changed_rows;
      inspected = null;
      message(`${Number(changed ?? planned.result.changed_rows).toLocaleString('nl-NL')} bronrijen opnieuw beoordeeld. Controleer de nieuwe uitkomst vóór definitief importeren.`);
      try {await onStaged(detail);} catch (error) {if (active()) message(`De beoordelingsversie is opgeslagen. Heropen het werkboek om de actuele regels te laden: ${error.message}`,true);}
      if (active()) {setBusy(false);await refresh();}
    } catch (error) {
      if (active()) {invalidate(); message(error.status === 409 ? `${error.message}. Vernieuw de groepen en controleer opnieuw.` : error.message,true);}
    } finally {if (active()) setBusy(false);}
  }
  async function refresh() {
    if (!active() || busy) return;
    saveView(); invalidate();
    const operation = ++generation;
    if (!metadata) container.innerHTML = '<h3>Gelijke antwoorden gezamenlijk beoordelen</h3><p class="hint" role="status">Groepen over alle importpagina’s worden geladen…</p>';
    else message('Actuele beoordelingsgroepen laden…');
    try {
      const value = await request(`${endpoint}/review-groups`);
      if (!active() || generation !== operation) return;
      metadata = value; render();
    } catch (error) {
      if (!active() || generation !== operation) return;
      container.innerHTML = `<h3>Gelijke antwoorden gezamenlijk beoordelen</h3><p class="notice error" data-message role="alert">${escapeHtml(error.message)}</p><button type="button" class="small" data-refresh>Opnieuw laden</button>`;
      find('[data-refresh]').onclick = refresh;
    }
  }
  function refreshSelection() {
    if (!active()) return;
    if (read('scope')?.value === 'selected') {invalidate();for(const ack of ['anchor_ack','title_ack','overwrite_ack'])if(read(ack))read(ack).checked=false;}
    updateScope();
  }
  refresh();
  return {refresh, refreshSelection, destroy() {destroyed = true; generation++; inspected = null;}};
}
