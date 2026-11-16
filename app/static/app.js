import {
  $,
  $$,
  esc,
  fmt,
  date,
  today,
  localNow,
  json,
  newClientId,
} from "./js/util.js";
let user = null,
  cultivations = [],
  varieties = [],
  bays = [],
  appSettings = { measurement_tolerance_days: 1, steering_until_kd: 35 },
  currentPage = "",
  syncing = false,
  poll = null,
  busyNavigation = 0;
const nav = [
  ["dashboard", "◫", "Overzicht"],
  ["bays", "▦", "Kappen"],
  ["measure", "＋", "Snel meten"],
  ["imports", "↥", "Priva-import"],
  ["climate", "☀", "Klimaat"],
  ["models", "⌁", "Prognoses"],
  ["model-uitleg", "?", "Uitleg voorspelling"],
  ["cards", "▤", "Teeltkaarten"],
  ["manage", "⚙", "Beheer"],
];
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  setTimeout(() => $("#toast").classList.remove("show"), 5000);
}
async function api(path, options = {}) {
  const headers = {
    ...(options.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    ...options.headers,
  };
  const res = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
    body:
      options.body && !(options.body instanceof FormData)
        ? JSON.stringify(options.body)
        : options.body,
  });
  if (!res.ok) {
    let data;
    try {
      data = await res.json();
    } catch {}
    const detail = data?.detail;
    const error = new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((x) => `${x.loc?.slice(1).join(".")}: ${x.msg}`)
              .join(" · ")
          : `Verzoek mislukt (${res.status})`,
    );
    error.status = res.status;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}
const list = (data) =>
  Array.isArray(data)
    ? data
    : data?.items ||
      data?.cultivations ||
      data?.jobs ||
      data?.models ||
      data?.scenarios ||
      data?.calibrations ||
      [];
function options(items, key = "id", label = "name", selected = "") {
  return items
    .map(
      (x) =>
        `<option value="${esc(x[key])}" ${String(x[key]) === String(selected) ? "selected" : ""}>${esc(typeof label === "function" ? label(x) : x[label])}</option>`,
    )
    .join("");
}
const cLabel = (c) =>
  `Kap ${c.bay_number} · ${c.variety_name || varieties.find((v) => v.id === c.variety_id)?.name || "soort"} · ${c.round_name}`;
const selectCult = (name = "cultivation_id", id = "") =>
  `<div class="field"><label for="${name}">Kapteelt</label><select id="${name}" name="${name}" required><option value="">Kies een kapteelt</option>${options(cultivations, "id", cLabel, id)}</select></div>`;
function field(name, label, type = "text", value = "", extra = "") {
  return `<div class="field"><label for="${name}">${label}</label><input id="${name}" name="${name}" type="${type}" value="${esc(value)}" ${extra}></div>`;
}
const area = (name, label, value = "") =>
  `<div class="field"><label for="${name}">${label}</label><textarea id="${name}" name="${name}">${esc(value)}</textarea></div>`;
const formError = () => '<div class="error-text" role="alert"></div>';
function payload(form) {
  return Object.fromEntries(new FormData(form));
}
function bindForm(id, fn) {
  const form = $(id);
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = $("[type=submit]", form),
      error = $(".error-text", form);
    if (error) error.textContent = "";
    if (submit) submit.disabled = true;
    try {
      await fn(payload(form), form);
    } catch (e) {
      if (error) error.textContent = e.message;
      else toast(e.message);
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}
function pageHead(title, sub, actions = "") {
  return `<div class="page-head"><div><div class="eyebrow">Chrysant · Teeltregistratie</div><h1>${title}</h1><p class="sub">${sub}</p></div><div class="actions">${actions}</div></div>`;
}
function empty(title, desc = "") {
  return `<div class="empty"><strong>${title}</strong>${desc}</div>`;
}
function tag(text, type = "") {
  return `<span class="tag ${type}">${esc(text)}</span>`;
}
function statusLabel(s) {
  return (
    {
      active: "Lopend",
      planned: "Gepland",
      harvested: "Geoogst",
      standard: "Standaard",
      adjusted: "Bijgestuurd",
      control: "Controle",
      unknown: "Onbekend",
      experimental: "Experimenteel",
      operational: "Vrijgegeven",
      baseline: "Historische referentie",
      queued: "In wachtrij",
      previewing: "Voorbeeld voorbereiden",
      preview: "Klaar voor controle",
      importing: "Wordt verwerkt",
      completed: "Voltooid",
      failed: "Mislukt",
      revoked: "Herroepen",
    }[s] || s
  );
}
function modal(title, content) {
  const d = $("#modal");
  d.innerHTML = `<div class="modal-head"><h2>${esc(title)}</h2><button type="button" aria-label="Venster sluiten" id="close-modal">×</button></div>${content}`;
  $("#close-modal").onclick = () => d.close();
  d.showModal();
  applyPermissions();
  return d;
}
async function getBase() {
  const owner = user?.id;
  const out = await Promise.allSettled([
    api("/api/cultivations"),
    api("/api/varieties"),
    api("/api/bays"),
    api("/api/settings"),
  ]);
  if (!user || user.id !== owner) return;
  if (out[0].status === "fulfilled") cultivations = list(out[0].value);
  if (out[1].status === "fulfilled") varieties = list(out[1].value);
  if (out[2].status === "fulfilled") bays = list(out[2].value);
  if (out[3].status === "fulfilled") appSettings = out[3].value.settings;
  if (out.every((x) => x.status === "rejected")) {
    const cached = JSON.parse(
      sessionStorage.getItem("chrysant-base") || "null",
    );
    if (cached?.owner === user.id) {
      cultivations = cached.cultivations;
      varieties = cached.varieties;
      bays = cached.bays;
      appSettings = cached.settings || appSettings;
    } else throw out[0].reason;
  } else
    sessionStorage.setItem(
      "chrysant-base",
      JSON.stringify({
        owner: user.id,
        cultivations,
        varieties,
        bays,
        settings: appSettings,
      }),
    );
}
function shell() {
  const current = location.hash.slice(1).split("/")[0] || "dashboard";
  $("#app").innerHTML =
    `<div class="layout"><aside class="sidebar"><div class="brand"><img src="/static/icon.svg" alt=""><div>chrysant<small>Gewichtsverwachting</small></div></div><nav class="nav" aria-label="Hoofdnavigatie">${nav.map(([id, icon, label]) => `<a href="#${id}" class="${current === id ? "active" : ""}"><span class="symbol" aria-hidden="true">${icon}</span>${label}</a>`).join("")}</nav><div class="side-bottom">LOKAAL & VERTROUWELIJK<br>Geen externe weer- of AI-provider<button id="logout-side" class="small">Uitloggen</button></div></aside><div class="workspace"><header class="topbar"><div><strong>Van kas naar inzicht</strong> <small> / ${esc(new Intl.DateTimeFormat("nl-NL", { weekday: "long", day: "numeric", month: "long" }).format(new Date()))}</small></div><div class="actions"><span id="connection" class="connection"></span><button id="logout" class="small" title="Uitloggen">${esc(user.username)} ↪</button></div></header><main id="main" tabindex="-1"><p class="muted">Gegevens laden…</p></main></div></div>`;
  $("#logout").onclick = logout;
  $("#logout-side").onclick = logout;
  connection();
}
function connection() {
  const el = $("#connection");
  if (el)
    el.innerHTML = `<span class="dot ${navigator.onLine ? "" : "off"}"></span>${navigator.onLine ? "Verbonden" : "Offline · lokaal bewaren"}`;
}
async function logout() {
  if (!navigator.onLine) {
    toast("Verbind met internet om de serversessie veilig af te sluiten.");
    return;
  }
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    toast("Afmelden is niet gelukt: " + error.message);
    return;
  }
  user = null;
  cultivations = [];
  varieties = [];
  bays = [];
  sessionStorage.removeItem("chrysant-user");
  sessionStorage.removeItem("chrysant-base");
  authChannel?.postMessage("logout");
  clearInterval(poll);
  location.hash = "";
  auth(false);
}
function auth(setup = false, message = "") {
  $("#app").innerHTML =
    `<div class="auth"><div class="brand"><img src="/static/icon.svg" alt=""><div>chrysant<small>Gewichtsverwachting</small></div></div><section class="card"><div class="eyebrow">Welkom in de kas</div><h1>${setup ? "Een goede start" : "Aanmelden"}</h1><p class="sub">${setup ? "Maak het eerste beheerdersaccount aan." : "Je teelten, metingen en verwachtingen op één plek."}</p>${message ? `<p class="notice warn">${esc(message)}</p>` : ""}<form id="auth-form">${field("username", "Gebruikersnaam", "text", "", 'required autocomplete="username" maxlength="80"')}${field("password", "Wachtwoord", "password", "", 'required autocomplete="' + (setup ? "new-password" : "current-password") + '" ' + (setup ? 'minlength="12"' : ""))}${setup ? '<p class="hint">Kies een sterk wachtwoord van minimaal 12 tekens.</p>' : ""}${formError()}<button class="primary" type="submit" style="width:100%">${setup ? "Account aanmaken" : "Inloggen"} →</button></form></section><p class="hint">Bedrijfsgegevens blijven op je eigen server. Metingen kun je na aanmelden ook zonder bereik vastleggen.</p></div>`;
  bindForm("#auth-form", async (data) => {
    const result = await api(setup ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST",
      body: data,
    });
    user = result.user || result;
    sessionStorage.setItem("chrysant-user", JSON.stringify(user));
    await start();
  });
}
async function start() {
  shell();
  try {
    await getBase();
  } catch (e) {
    toast(
      navigator.onLine
        ? e.message
        : "Offline: nieuwe metingen worden lokaal bewaard.",
    );
  }
  await route();
  syncQueue();
}
function pageOwner(page) {
  const generation = busyNavigation,
    owner = user?.id;
  return () =>
    !!user &&
    user.id === owner &&
    busyNavigation === generation &&
    currentPage === page;
}
async function route() {
  if (!user) return;
  clearInterval(poll);
  const stamp = ++busyNavigation;
  const [requestedPage, id] = location.hash.slice(1).split("/");
  const page = requestedPage || "dashboard";
  currentPage = page;
  $$(".nav a").forEach((a) =>
    a.classList.toggle("active", a.hash === `#${page}`),
  );
  const main = $("#main");
  main.innerHTML = '<p class="muted">Gegevens laden…</p>';
  try {
    let render =
      {
        dashboard,
        bays: bayPage,
        measure,
        imports,
        climate,
        models,
        "model-uitleg": modelExplanation,
        cards,
        manage,
        cultivation: () => detail(id),
      }[page] || dashboard;
    await render();
    if (stamp !== busyNavigation) return;
    applyPermissions();
    main.focus({ preventScroll: true });
  } catch (e) {
    if (stamp !== busyNavigation || !user) return;
    main.innerHTML =
      pageHead(
        "Gegevens niet beschikbaar",
        "Controleer je verbinding en probeer opnieuw.",
      ) +
      `<div class="notice error">${esc(e.message)}</div><button id="retry">Opnieuw proberen</button>`;
    $("#retry").onclick = route;
  }
}
function cultivationTable(rows) {
  return rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Kap / soort</th><th>Ronde</th><th>Bedden</th><th>Status</th><th></th></tr></thead><tbody>${rows.map((c) => `<tr><td><a href="#cultivation/${c.id}">Kap ${esc(c.bay_number)} · ${esc(c.variety_name || varieties.find((v) => v.id === c.variety_id)?.name || "—")}</a></td><td>${esc(c.round_name)}</td><td>${esc(c.beds)} / 6</td><td>${tag(statusLabel(c.status))}</td><td><a href="#measure/${c.id}" aria-label="Meting voor kap ${esc(c.bay_number)}">Meten →</a></td></tr>`).join("")}</tbody></table></div>`
    : empty(
        "Nog geen kapteelten",
        "Maak een kapteelt aan om te beginnen met plannen en meten.",
      );
}
async function dashboard() {
  const alive = pageOwner("dashboard");
  if (!alive()) return;
  let data = {};
  try {
    data = await api("/api/dashboard");
  } catch (e) {
    if (navigator.onLine) throw e;
  }
  const active = cultivations.filter((c) =>
    ["planned", "active"].includes(c.status),
  );
  const queued = await queueItems();
  if (!alive()) return;
  const n = queued.filter((q) => q.status !== "synced").length;
  $("#main").innerHTML =
    pageHead(
      "Goed zicht op je teelt",
      "Van de eerste meting tot het verse eindgewicht.",
      `<button class="primary" id="new-cultivation">＋ Nieuwe kapteelt</button>`,
    ) +
    `<div class="hero-strip"><div><h2>Elke tak telt.</h2><p>Vijf takken meten. Groei volgen. Beslissen met betrouwbare gegevens.</p></div><a class="btn" href="#measure">＋ Snel een meting invoeren</a></div><div class="grid"><section class="card metric"><span class="icon" aria-hidden="true">♧</span><div class="label">Lopende kapteelten</div><div class="value">${active.length}</div><div class="hint">In ${new Set(active.map((c) => c.round_name)).size} ronde(n)</div></section><section class="card metric"><span class="icon" aria-hidden="true">▦</span><div class="label">Kappen in gebruik</div><div class="value">${new Set(active.map((c) => c.bay_number)).size}<small style="font-size:14px;color:var(--muted)"> / 40</small></div><div class="hint">Verdeeld over 4 afdelingen</div></section><section class="card metric"><span class="icon" aria-hidden="true">↥</span><div class="label">Lokaal te verzenden</div><div class="value">${n}</div><div class="hint">${n ? "Bewaard op dit apparaat" : "Alle vastgelegde metingen verzonden"}</div></section></div><div class="two section"><section class="card"><div class="section-title"><h2>Teelten in de kas</h2><a href="#bays" class="hint">Alle kappen →</a></div>${cultivationTable(active.slice(0, 8))}</section><div><section class="card"><h2>Vandaag aandacht</h2><div id="tasks">${renderTasks(data.tasks || data.due_measurements || [])}</div></section><section class="card"><div class="eyebrow">Betrouwbaar voorspellen</div><h2>Eerst meten, dan weten</h2><p class="sub" style="font-size:13px">Een doelgewicht is geen voorspelling. De prognose blijft onbekend tot er voldoende bruikbare oogstgegevens zijn.</p><a href="#models">Bekijk modelstatus →</a></section></div></div><p class="footer-note">Actuele bedrijfsregistratie · Geen gegevens worden naar externe diensten verzonden.</p>`;
  $("#new-cultivation").onclick = newCultivation;
}
function renderTasks(tasks) {
  if (!tasks?.length)
    return empty(
      "Geen open meettaken",
      "Nieuwe meetmomenten verschijnen op basis van de teeltplanning.",
    );
  return tasks
    .map(
      (t) =>
        `<article style="padding:12px 0;border-bottom:1px solid var(--line)"><strong>${esc(t.title || t.kind || t.message || "Meetmoment")}</strong> ${t.status === "overdue" ? tag("Achterstallig", "warn") : ""}<p class="hint">${esc(t.description || t.round_name || "")} ${t.bay_number ? "· Kap " + esc(t.bay_number) : ""}<br>${t.due_date ? date(t.due_date) + " · " : ""}${t.actual_kd_day != null ? "Nu KD-dag " + esc(t.actual_kd_day) : ""} ${esc(t.variety || "")}</p>${t.cultivation_id ? `<a class="small btn" href="#measure/${t.cultivation_id}">Meting vastleggen</a>` : ""}</article>`,
    )
    .join("");
}
async function bayPage() {
  $("#main").innerHTML =
    pageHead(
      "De kas in één oogopslag",
      "40 kappen · 4 afdelingen · zes bedden per kap",
      `<button class="primary" id="new-cultivation">＋ Nieuwe kapteelt</button>`,
    ) +
    `<section class="card"><div class="actions" style="margin-bottom:20px"><label for="round-filter" style="align-self:center">Ronde</label><select id="round-filter" style="max-width:250px"><option value="">Alle rondes</option>${[...new Set(cultivations.map((c) => c.round_name))].map((x) => `<option>${esc(x)}</option>`).join("")}</select><span class="tag">Groen = teelt geregistreerd</span></div><div id="bay-map"></div></section><section class="card"><h2>Kapteelten</h2><div id="bay-table"></div></section>`;
  function draw() {
    const round = $("#round-filter").value;
    const cs = cultivations.filter((c) => !round || c.round_name === round);
    $("#bay-map").innerHTML = [
      [1, 9],
      [10, 18],
      [19, 30],
      [31, 40],
    ]
      .map(
        ([s, e], i) =>
          `<section class="department"><div class="department-head"><strong>AFDELING ${i + 1}</strong><span>Kap ${s}–${e}</span></div><div class="bay-grid">${Array.from(
            { length: e - s + 1 },
            (_, j) => {
              const n = s + j,
                rows = cs.filter((c) => c.bay_number === n);
              return `<button class="bay ${rows.length ? "occupied" : ""}" data-bay="${n}" aria-label="Kap ${n}: ${rows.length ? rows.map(cLabel).join(", ") : "geen geregistreerde teelt"}"><b>${n.toString().padStart(2, "0")}</b><small>${rows.length ? rows.map((c) => varieties.find((v) => v.id === c.variety_id)?.name || "Teelt").join(" / ") : "Beschikbaar"}</small></button>`;
            },
          ).join("")}</div></section>`,
      )
      .join("");
    $("#bay-table").innerHTML = cultivationTable(cs);
    $$("[data-bay]").forEach(
      (b) =>
        (b.onclick = () => {
          const rows = cs.filter((c) => c.bay_number === Number(b.dataset.bay));
          if (rows.length === 1) location.hash = `cultivation/${rows[0].id}`;
          else if (rows.length)
            modal(`Kap ${b.dataset.bay}`, cultivationTable(rows));
          else newCultivation(Number(b.dataset.bay));
        }),
    );
  }
  $("#round-filter").onchange = draw;
  $("#new-cultivation").onclick = () => newCultivation();
  draw();
}
function newCultivation(bay = "") {
  modal(
    "Nieuwe kapteelt",
    `<p class="sub">Registreer elke soort in een gemengde kap als een eigen kapteelt.</p><form id="cult-form"><div class="form-grid">${field("round_name", "Ronde", "text", "", 'required placeholder="Bijv. Winter 2026–2027"')}<div class="field"><label for="bay_number">Kap</label><select name="bay_number" id="bay_number" required><option value="">Kies een kap</option>${Array.from({ length: 40 }, (_, i) => `<option value="${i + 1}" ${i + 1 === bay ? "selected" : ""}>Kap ${i + 1}</option>`).join("")}</select></div><div class="field"><label for="variety_id">Soort</label><select name="variety_id" id="variety_id" required>${options(varieties.filter((v) => v.active !== false))}</select></div>${field("beds", "Aantal bedden", "number", 6, 'min="1" max="6" step="1" required')}<div class="field"><label for="category">Teeltcategorie</label><select name="category" id="category"><option value="standard">Standaard</option><option value="adjusted">Bijgestuurd</option><option value="control">Controle</option></select></div><div class="field"><label for="status">Status</label><select name="status" id="status"><option value="active">Lopend</option><option value="planned">Gepland</option></select></div>${field("plant_date", "Plantdatum", "date", "", "required")}${field("light_out_date", "Licht uit", "date", "", "required")}${field("expected_harvest_date", "Verwachte oogst", "date", "", "required")}${field("density", "Plantdichtheid (takken/m²)", "number", "", 'min="0.1" max="500" step="0.1" required')}${planningExtras({})}</div>${formError()}<button class="primary" type="submit">Kapteelt opslaan</button></form>`,
  );
  bindForm("#cult-form", async (d) => {
    const c = await api("/api/cultivations", {
      method: "POST",
      body: {
        round_name: d.round_name,
        bay_number: +d.bay_number,
        variety_id: +d.variety_id,
        beds: +d.beds,
        category: d.category,
        status: d.status,
        label_quality: "verified",
      },
    });
    try {
      await api(`/api/cultivations/${c.id}/planning`, {
        method: "POST",
        body: {
          plant_date: d.plant_date,
          light_out_date: d.light_out_date,
          expected_harvest_date: d.expected_harvest_date,
          density: +d.density,
          reason: "Eerste planning",
          ...planningExtraPayload(d),
        },
      });
    } catch (e) {
      toast("Kapteelt aangemaakt; planning nog aanvullen: " + e.message);
    }
    $("#modal").close();
    await getBase();
    location.hash = `cultivation/${c.id}`;
    if (currentPage === "cultivation") route();
  });
}
async function detail(id) {
  const alive = pageOwner("cultivation");
  if (!alive()) return;
  const c = await api(`/api/cultivations/${Number(id)}`);
  if (!alive()) return;
  let f;
  try {
    f = await api(`/api/cultivations/${c.id}/forecast`);
  } catch (e) {
    f = { status: "unknown", warnings: [e.message] };
  }
  if (!alive()) return;
  const p = c.planning?.[c.planning.length - 1] || {};
  $("#main").innerHTML =
    pageHead(
      `Kap ${c.bay_number} · ${esc(c.variety_name || varieties.find((v) => v.id === c.variety_id)?.name || "")}`,
      `${esc(c.round_name)} · ${esc(c.beds)} bedden · ${statusLabel(c.category)}`,
      `<a class="btn" href="/api/cards?ids=${c.id}" target="_blank" rel="noopener">▤ Teeltkaart</a><a class="btn primary" href="#measure/${c.id}">＋ Meting</a>`,
    ) +
    `<div class="two"><div><section class="card"><div class="section-title"><h2>Verwachting & seizoen</h2>${tag(statusLabel(f.status), f.status === "operational" ? "" : "warn")}</div>${forecastView(f)}</section><section class="card"><div class="section-title"><h2>Planning</h2><button id="edit-plan" class="small">Nieuwe versie</button></div>${
      c.planning?.length
        ? `<div class="form-grid"><div><label>Geplant</label><p>${date(p.plant_date)}</p></div><div><label>Licht uit</label><p>${date(p.light_out_date)}</p></div><div><label>Verwachte oogst</label><p>${date(p.expected_harvest_date)}</p></div><div><label>Plantdichtheid</label><p>${fmt(p.density, " /m²")}</p></div><div><label>Geplande takken</label><p>${fmt(p.planned_stems)}</p></div><div><label>Geplande branduren</label><p>${fmt(p.planned_lamp_hours, " uur/dag")}</p></div><div class="full"><label>Tussenlicht & belichtingsschema</label><p class="hint">${esc(p.interlight_description || "Niet vastgelegd")}<br>${esc(p.lighting_schedule || "Niet vastgelegd")}</p></div></div><details><summary>${c.planning.length} planningversie(s)</summary>${c.planning
            .slice()
            .reverse()
            .map(
              (v) =>
                `<p class="hint">${date(v.created_at)} · ${esc(v.reason)}<br>Planten ${date(v.plant_date)} · licht uit ${date(v.light_out_date)} · oogst ${date(v.expected_harvest_date)}</p>`,
            )
            .join("")}</details>`
        : empty(
            "Planning ontbreekt",
            "Voeg de datums toe om KD-dagen en meetmomenten te berekenen.",
          )
    }</section><section class="card" id="growth-target"><h2>Groei & streeflengte</h2><p class="hint">Groei vergelijken…</p></section><section class="card" id="environment-panel"><h2>Licht, klimaat & water per fase</h2><p class="hint">Brondekking laden…</p></section><section class="card"><h2>Metingen</h2>${measurementHistory(c.measurements || [])}</section><section class="card" id="forecast-history"><h2>Eerdere verwachtingen</h2><p class="hint">Prognosehistorie laden…</p></section></div><div><section class="card"><h2>Registreren</h2><div class="actions"><button id="event-btn">＋ Gebeurtenis</button><button id="harvest-btn">＋ Oogst</button><button id="cultivation-state">Teeltstatus</button></div><p class="hint" style="margin-top:12px">Leg afwijkingen, remstof, licht en teeltinstellingen met hun werkelijke datum vast.</p></section><section class="card"><h2>Teelttijdlijn</h2><div class="timeline">${
      [
        ...(c.events || []).map((e) => ({
          date: e.occurred_at,
          title: e.kind,
          body: `${e.amount ?? ""} ${e.unit || ""} · ${e.scope || ""} ${e.reason || ""}`,
        })),
        ...(c.harvests || []).map((h) => ({
          date: h.start_date,
          title: "Oogst geregistreerd",
          body: `${fmt(h.weight_g, " g/tak")} · ${fmt(h.stem_count, " takken")} · ${h.notes || ""}`,
        })),
      ]
        .sort((a, b) => String(b.date).localeCompare(a.date))
        .map(
          (e) =>
            `<article><small>${date(e.date)}</small><h3>${esc(e.title)}</h3><p class="hint">${esc(e.body)}</p></article>`,
        )
        .join("") || '<p class="hint">Nog geen gebeurtenissen of oogsten.</p>'
    }</div></section></div></div>`;
  $("#edit-plan").onclick = () => planningForm(c.id, p);
  $("#event-btn").onclick = () => eventForm(c.id);
  $("#harvest-btn").onclick = () => harvestForm(c.id);
  $("#cultivation-state").onclick = () => cultivationState(c);
  await Promise.allSettled([
    growthPanel(c.id),
    environmentPanel(c.id),
    forecastHistoryPanel(c.id),
  ]);
}
function forecastView(f) {
  const season = f.season;
  return `<div class="summary"><div><small>Verwacht eindgewicht</small><strong>${fmt(f.point_g, " g")}</strong></div><div><small>80%-voorspelinterval</small><strong style="font-size:18px">${f.low_g != null && f.high_g != null ? `${fmt(f.low_g)} – ${fmt(f.high_g)} g` : "Onbekend"}</strong></div><div><small>Soortdoel</small><strong style="font-size:18px">${fmt(f.target_g, " g")}</strong></div></div>${f.point_g == null ? '<p class="hint">Nog geen betrouwbare gewichtsverwachting beschikbaar. Registreer metingen en complete oogsten.</p>' : ""}${season ? `<div class="notice"><strong>${esc({ longer: "De dagen worden langer", shorter: "De dagen worden korter", turning: "Rond het keerpunt van de daglengte" }[season.trend] || "Seizoensverloop")}</strong><br>Astronomische daglengte: ${fmt(season.daylength_hours, " uur")} ${season.trend_hours_per_day != null ? "· verandering " + fmt(season.trend_hours_per_day * 60, " min/dag") : ""}<p class="hint" style="margin-bottom:0">Daglengte is een seizoensindicatie. De werkelijke lichtsom in de kas hangt ook af van weer, schermen en lampen.</p>${season.expected_remainder ? `<details><summary>Resterend seizoen: donker / gemiddeld / licht</summary>${typeof season.expected_remainder === "string" ? esc(season.expected_remainder) : seasonRemainder(season.expected_remainder)}</details>` : ""}</div>` : ""}${f.actual_kd_day != null ? `<p class="notice ${f.can_steer ? "" : "warn"}">Nu KD-dag ${esc(f.actual_kd_day)} · ingesteld bijstuurvenster tot KD${esc(f.steering_until_kd ?? appSettings.steering_until_kd)}. ${f.can_steer ? "Binnen het bijstuurvenster." : "Buiten het bijstuurvenster."} Alleen menselijke beoordeling; gebeurtenissen worden handmatig vastgelegd.</p>` : ""}${forecastInputs(f)}${(f.warnings || []).map((w) => `<p class="notice warn">${esc(typeof w === "string" ? w : JSON.stringify(w))}</p>`).join("")}${f.as_of ? `<p class="hint">Invoerpeildatum: ${date(f.as_of)}</p>` : ""}`;
}
function measurementHistory(rows) {
  if (!rows.length)
    return empty(
      "Nog geen metingen",
      "Meet vijf afzonderlijke takken op KD10, KD30 of tijdens de pilot.",
    );
  return rows
    .slice()
    .reverse()
    .map(
      (m) =>
        `<details><summary>${esc(m.kind)} · ${date(m.measured_at)} · ${fmt(m.mean_weight_g, " g")} · ${fmt(m.mean_length_cm, " cm")}</summary><p class="hint">Werkelijke KD-dag ${fmt(m.actual_kd_day)} · ${esc(m.location || "")}<br>${esc(m.notes || "")}</p>${m.timing_warning ? `<p class="notice warn">Meting valt buiten de ingestelde tolerantie van ${esc(m.timing_tolerance_days ?? appSettings.measurement_tolerance_days)} dag(en).</p>` : ""}<div class="table-wrap"><table><thead><tr><th>Plek</th><th>Gewicht</th><th>Lengte</th><th>Opname</th></tr></thead><tbody>${(m.stems || []).map((s) => `<tr><td>${esc(s.position)}</td><td>${fmt(s.weight_g, " g")}</td><td>${fmt(s.length_cm, " cm")}</td><td>${s.excluded ? "Uitgesloten: " + esc(s.exclusion_reason) : "Ja"}</td></tr>`).join("")}</tbody></table></div></details>`,
    )
    .join("");
}
function planningForm(id, p) {
  modal(
    "Nieuwe planningversie",
    `<p class="hint">Eerdere versies en oorspronkelijke meetmomenten blijven behouden.</p><form id="plan-form"><div class="form-grid">${field("plant_date", "Plantdatum", "date", p.plant_date || "", "required")}${field("light_out_date", "Licht uit", "date", p.light_out_date || "", "required")}${field("expected_harvest_date", "Verwachte oogst", "date", p.expected_harvest_date || "", "required")}${field("density", "Plantdichtheid (takken/m²)", "number", p.density || "", 'required min="0.1" step="0.1"')}${planningExtras(p)}</div>${area("reason", "Reden voor wijziging")}${formError()}<button class="primary" type="submit">Nieuwe versie bewaren</button></form>`,
  );
  bindForm("#plan-form", async (d) => {
    await api(`/api/cultivations/${id}/planning`, {
      method: "POST",
      body: { ...d, density: +d.density, ...planningExtraPayload(d) },
    });
    $("#modal").close();
    toast("Planningversie opgeslagen");
    route();
  });
}
function eventForm(id) {
  modal(
    "Gebeurtenis vastleggen",
    `<form id="event-form"><datalist id="event-units"><option value="g/1000m2">Gram per 1000 m², volvelds</option><option value="g/10l">Gram per 10 liter, selectief</option><option value="h">Uren</option><option value="°C">Graden Celsius</option></datalist>${field("occurred_at", "Werkelijk tijdstip", "datetime-local", localNow(), "required")}<div class="form-grid"><div class="field"><label for="kind">Gebeurtenis</label><select name="kind" id="kind"><option value="alar">Alar / remstof</option><option value="light">Belichting</option><option value="temperature">Temperatuurinstelling</option><option value="deviation">Afwijking</option><option value="other">Overig</option></select></div>${field("amount", "Hoeveelheid", "number", "", 'step="0.01"')}${field("unit", "Eenheid", "text", "g/1000m2", 'list="event-units" placeholder="Kies de juiste eenheid"')}<div class="field"><label for="scope">Reikwijdte</label><select id="scope" name="scope"><option value="volvelds">Volvelds</option><option value="selectief">Selectief</option></select></div></div>${area("reason", "Reden en toelichting")}${formError()}<button type="submit" class="primary">Gebeurtenis opslaan</button></form>`,
  );
  function eventUnit() {
    const kind = $("#kind").value;
    $("#unit").value =
      kind === "alar"
        ? $("#scope").value === "selectief"
          ? "g/10l"
          : "g/1000m2"
        : kind === "light"
          ? "h"
          : kind === "temperature"
            ? "°C"
            : "";
  }
  $("#kind").onchange = eventUnit;
  $("#scope").onchange = eventUnit;
  bindForm("#event-form", async (d) => {
    if (
      d.kind === "alar" &&
      d.amount &&
      d.unit !== (d.scope === "selectief" ? "g/10l" : "g/1000m2")
    )
      throw new Error(
        "Gebruik g/1000m2 voor volvelds of g/10l voor selectieve alar.",
      );
    await api(`/api/cultivations/${id}/events`, {
      method: "POST",
      body: { ...d, amount: d.amount ? +d.amount : null },
    });
    $("#modal").close();
    route();
  });
}
function harvestForm(id) {
  let classCount = 0;
  modal(
    "Oogst registreren",
    `<form id="harvest-form"><div class="form-grid">${field("start_date", "Begin oogst", "date", today(), "required")}${field("end_date", "Leegdatum", "date", today(), "required")}${field("weight_g", "Vers eindgewicht per tak (g)", "number", "", 'min="0.1" step="0.1" required')}${field("stem_count", "Aantal geoogste takken", "number", "", 'min="1" step="1" required')}${field("loss_fraction", "Uitval (fractie: 0–1)", "number", "", 'min="0" max="1" step="0.001"')}</div><p class="hint">Verschillende gewichten (bijvoorbeeld 95 / 65 g) blijven aparte klassen. Voeg in dat geval de klassen toe; er wordt geen gezamenlijk gewicht ingevuld.</p><div id="harvest-classes"></div><button type="button" id="add-harvest-class" class="small">＋ Afzonderlijke kwaliteitsklasse</button>${area("notes", "Opmerkingen")}${formError()}<button type="submit" class="primary" style="margin-top:15px">Oogst opslaan</button></form>`,
  );
  function updateMode() {
    const hasClasses = $$("[data-harvest-class]").length > 0;
    $("#weight_g").required = !hasClasses;
    $("#weight_g").disabled = hasClasses;
    if (hasClasses) $("#weight_g").value = "";
  }
  $("#add-harvest-class").onclick = () => {
    const i = classCount++;
    $("#harvest-classes").insertAdjacentHTML(
      "beforeend",
      `<section data-harvest-class="${i}" style="border-top:1px solid var(--line);padding-top:15px"><div class="form-grid">${field("class_name_" + i, "Kwaliteitsklasse", "text", "", 'required placeholder="Bijv. klasse I"')}${field("class_weight_" + i, "Gewicht per tak (g)", "number", "", 'required min="0.1" step="0.1"')}${field("class_count_" + i, "Aantal takken in deze klasse", "number", "", 'required min="1" step="1"')}</div><button type="button" class="small danger" data-remove-class="${i}">Klasse verwijderen</button></section>`,
    );
    $$("[data-remove-class]").forEach(
      (button) =>
        (button.onclick = () => {
          button.closest("[data-harvest-class]").remove();
          updateMode();
        }),
    );
    updateMode();
  };
  bindForm("#harvest-form", async (d) => {
    const classes = $$("[data-harvest-class]").map((row) => {
      const i = row.dataset.harvestClass;
      return {
        name: d["class_name_" + i],
        weight_g: +d["class_weight_" + i],
        stem_count: +d["class_count_" + i],
      };
    });
    await api(`/api/cultivations/${id}/harvest`, {
      method: "POST",
      body: {
        start_date: d.start_date,
        end_date: d.end_date,
        weight_g: d.weight_g ? +d.weight_g : null,
        stem_count: +d.stem_count,
        loss_fraction: d.loss_fraction ? +d.loss_fraction : null,
        quality_classes: classes,
        notes: d.notes,
      },
    });
    $("#modal").close();
    await getBase();
    route();
  });
}
// Only measurements and drafts are stored locally, partitioned by authenticated user ID.
let dbPromise;
function localDB() {
  if (!dbPromise)
    dbPromise = new Promise((resolve, reject) => {
      const r = indexedDB.open("chrysant-offline-v1", 1);
      r.onupgradeneeded = () => {
        r.result.createObjectStore("measurements", { keyPath: "key" });
        r.result.createObjectStore("drafts", { keyPath: "key" });
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  return dbPromise;
}
async function localOp(store, mode, fn) {
  const db = await localDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    let result;
    try {
      result = fn(tx.objectStore(store));
    } catch (e) {
      reject(e);
      return;
    }
    tx.oncomplete = () => resolve(result?.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () =>
      reject(tx.error || new Error("Lokale opslag afgebroken"));
  });
}
async function queueItems() {
  if (!user) return [];
  const rows = await localOp("measurements", "readonly", (s) => s.getAll());
  return rows
    .filter((r) => r.owner === user.id)
    .sort((a, b) => b.saved_at.localeCompare(a.saved_at));
}
async function saveQueue(body) {
  const item = {
    key: `${user.id}:${body.client_id}`,
    owner: user.id,
    body,
    status: "queued",
    saved_at: new Date().toISOString(),
    label: cLabel(
      cultivations.find((c) => c.id === body.cultivation_id) || {
        bay_number: "?",
        round_name: "",
      },
    ),
  };
  await localOp("measurements", "readwrite", (s) => s.put(item));
  return item;
}
async function syncQueue() {
  if (!user || syncing || !navigator.onLine) return;
  syncing = true;
  const owner = user.id;
  try {
    const session = await api("/api/auth/status");
    if (session.user?.id !== owner) {
      toast(
        "Meld opnieuw aan met dit account om de lokale metingen te verzenden.",
      );
      return;
    }
    for (const row of await queueItems()) {
      if (row.status === "synced" || !user || user.id !== owner) continue;
      try {
        await api("/api/measurements", { method: "POST", body: row.body });
        row.status = "synced";
        row.error = null;
        row.synced_at = new Date().toISOString();
      } catch (e) {
        row.status = "queued";
        row.error = e.message;
        if (e.status === 401 || e.status === 403) {
          await localOp("measurements", "readwrite", (s) => s.put(row));
          break;
        }
      }
      await localOp("measurements", "readwrite", (s) => s.put(row));
    }
  } catch (e) {
    toast("Metingen blijven lokaal: " + e.message);
  } finally {
    syncing = false;
    if (currentPage === "measure" && $("#queue")) drawQueue();
  }
}
async function drawQueue() {
  const el = $("#queue");
  if (!el) return;
  const rows = await queueItems();
  if (!el.isConnected) return;
  el.innerHTML = rows.length
    ? rows
        .slice(0, 30)
        .map(
          (q) =>
            `<article style="padding:12px 0;border-bottom:1px solid var(--line)"><div style="display:flex;justify-content:space-between;gap:12px"><strong style="font-size:13px">${esc(q.label)}</strong>${tag(q.status === "synced" ? "Gesynchroniseerd" : "Lokaal bewaard", q.status === "synced" ? "" : "warn")}</div><p class="hint">${esc(q.body.kind)} · ${date(q.body.measured_at)} · ID ${esc(q.body.client_id.slice(0, 8))}${q.error ? `<br><span class="danger">${esc(q.error)}</span>` : ""}</p><details><summary>Ingevoerde takken bekijken</summary>${json(q.body)}</details></article>`,
        )
        .join("")
    : empty(
        "Geen lokale metingen",
        "Opgeslagen metingen en hun verzendstatus verschijnen hier.",
      );
  const pending = rows.filter((q) => q.status !== "synced");
  $("#queue-count").textContent = String(pending.length);
}
async function measure() {
  const alive = pageOwner("measure");
  if (!alive()) return;
  const id = location.hash.split("/")[1] || "";
  $("#main").innerHTML =
    pageHead(
      "Vijf takken. Eén meting.",
      "Knip bij de grond, weeg direct vers en meet elke tak afzonderlijk.",
    ) +
    `<div class="two"><section class="card"><form id="measure-form">${selectCult("cultivation_id", id)}${!cultivations.length ? '<p class="notice warn">Maak eerst een kapteelt aan via Kappen. Bestaande concepten blijven lokaal bewaard.</p>' : ""}<div class="form-grid"><div class="field"><label for="kind">Meetmoment</label><select name="kind" id="kind"><option value="KD10">KD10 · eerste meting</option><option value="KD30">KD30 · vervolgmeting</option><option value="pilot">Pilot · wekelijkse meting</option></select></div>${field("measured_at", "Werkelijk meetmoment", "datetime-local", localNow(), "required")}${field("location", "Meetplek / protocolafwijking", "text", "", 'required placeholder="Bijv. vijf vaste plekken, ochtend"')}</div><div class="notice" id="kd-info">Streefdag KD10 of KD30, met ${esc(appSettings.measurement_tolerance_days ?? 1)} dag(en) tolerantie. De werkelijke KD-dag wordt bij synchronisatie gecontroleerd.</div><div class="stems"><span></span><label>Gewicht (g)</label><label>Lengte (cm)</label><label class="position">Vaste meetplek</label>${Array.from({ length: 5 }, (_, i) => `<div class="stem-row"><span class="stem-number">${i + 1}</span><input aria-label="Tak ${i + 1} gewicht in gram" name="weight_${i}" type="number" inputmode="decimal" required min="0.1" max="1000" step="0.1"><input aria-label="Tak ${i + 1} lengte in centimeter" name="length_${i}" type="number" inputmode="decimal" required min="0.1" max="300" step="0.1"><input class="position" aria-label="Tak ${i + 1} vaste meetplek" name="position_${i}" value="Plek ${i + 1}" required><div class="exclude"><label class="inline"><input name="excluded_${i}" type="checkbox"> Uitsluiten van gemiddelde</label><input name="reason_${i}" aria-label="Reden uitsluiting tak ${i + 1}" placeholder="Reden voor uitsluiting" hidden></div></div>`).join("")}</div><div class="summary" id="measurement-summary"><div><small>Gemiddeld gewicht</small><strong>—</strong></div><div><small>Gemiddelde lengte</small><strong>—</strong></div></div>${area("notes", "Opmerkingen (bijv. kort na watergift)")}${formError()}<button class="primary" type="submit">✓ Meting veilig bewaren</button><p class="hint">Eerst op dit apparaat opgeslagen. Met internet wordt de meting automatisch één keer verzonden.</p><p class="hint" id="draft-status" aria-live="polite"></p></form></section><aside><section class="card"><div class="section-title"><h2>Verzendstatus <span id="queue-count" class="tag warn">0</span></h2><button class="small" id="sync-now">↻ Verzenden</button></div><p class="hint">Een lokale meting overleeft herladen. Alleen “Gesynchroniseerd” is ook veilig op de server opgeslagen.</p><div id="queue"></div></section><section class="notice warn">Openstaande metingen staan alleen op dit apparaat. Wis browsergegevens pas nadat alles is gesynchroniseerd.</section></aside></div>`;
  const form = $("#measure-form"),
    draftKey = `${user.id}:measurement`;
  const saved = await localOp("drafts", "readonly", (s) => s.get(draftKey));
  if (!alive() || !form.isConnected) return;
  if (saved?.data) {
    for (const [k, v] of Object.entries(saved.data)) {
      const f = form.elements.namedItem(k);
      if (f) {
        if (f.type === "checkbox") f.checked = v === "on";
        else f.value = v;
      }
    }
    if (id) form.elements.cultivation_id.value = id;
    $("#draft-status").textContent = "Lokaal concept hersteld.";
  }
  let draftTimer,
    measurePlan = saved?.planning || null;
  function showKD() {
    if (!alive() || !form.isConnected) return;
    const el = $("#kd-info");
    const measured = form.elements.measured_at.value.slice(0, 10);
    const lightOut = measurePlan?.light_out_date;
    if (!lightOut || !measured) return;
    const kd = Math.round(
      (Date.parse(measured) - Date.parse(lightOut)) / 86400000,
    );
    const kind = form.elements.kind.value;
    const late =
      kind !== "pilot" &&
      Math.abs(kd - Number(kind.slice(2))) >
        appSettings.measurement_tolerance_days;
    el.classList.toggle("warn", late);
    el.textContent = `Werkelijke KD-dag: ${kd} · licht uit ${date(lightOut)}. ${late ? "Deze meting valt buiten de ingestelde tolerantie rond de streefdag. Leg de reden vast bij opmerkingen." : kind === "pilot" ? "Pilotmeting: de werkelijke meetdag blijft bewaard." : "Binnen de ingestelde tolerantie rond het streefmeetmoment."} Alle tijden zijn Nederlandse tijd.`;
  }
  async function loadMeasurePlan() {
    const cid = form.elements.cultivation_id.value;
    if (!cid) return;
    try {
      const data = await api(`/api/cultivations/${cid}`);
      measurePlan = data.planning?.at(-1) || null;
    } catch {
      /* An offline draft retains its last known planning version. */
    }
    showKD();
  }
  form.elements.cultivation_id.addEventListener("change", async () => {
    measurePlan = null;
    await loadMeasurePlan();
    await persistDraft();
  });
  await loadMeasurePlan();
  if (!alive() || !form.isConnected) return;
  async function persistDraft() {
    try {
      await localOp("drafts", "readwrite", (s) =>
        s.put({
          key: draftKey,
          owner: user.id,
          data: payload(form),
          planning: measurePlan,
        }),
      );
      if ($("#draft-status"))
        $("#draft-status").textContent = "Concept opgeslagen op dit apparaat.";
    } catch (e) {
      if ($("#draft-status"))
        $("#draft-status").textContent =
          "Lokale opslag niet beschikbaar: " + e.message;
    }
  }
  function update() {
    if (!alive() || !form.isConnected) return;
    showKD();
    const ws = [],
      ls = [];
    for (let i = 0; i < 5; i++) {
      const excluded = form.elements[`excluded_${i}`].checked;
      const reason = form.elements[`reason_${i}`];
      reason.hidden = !excluded;
      reason.required = excluded;
      if (!excluded) {
        if (form.elements[`weight_${i}`].value)
          ws.push(+form.elements[`weight_${i}`].value);
        if (form.elements[`length_${i}`].value)
          ls.push(+form.elements[`length_${i}`].value);
      }
    }
    const avg = (a) =>
      a.length ? a.reduce((a, b) => a + b, 0) / a.length : null;
    const mean = avg(ws),
      sd =
        ws.length > 1
          ? Math.sqrt(
              ws.reduce((a, b) => a + (b - mean) ** 2, 0) / (ws.length - 1),
            )
          : null;
    $("#measurement-summary").innerHTML =
      `<div><small>Gem. gewicht (${ws.length} takken)</small><strong>${fmt(mean, " g")}</strong></div><div><small>Gem. lengte</small><strong>${fmt(avg(ls), " cm")}</strong></div><div><small>Spreiding (SD)</small><strong>${fmt(sd, " g")}</strong></div>`;
    clearTimeout(draftTimer);
    draftTimer = setTimeout(persistDraft, 250);
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
  $("#sync-now").onclick = async () => {
    await syncQueue();
    toast(
      navigator.onLine
        ? "Verzendpoging afgerond"
        : "Geen verbinding: metingen blijven lokaal",
    );
  };
  await drawQueue();
  if (!alive() || !form.isConnected) return;
  bindForm("#measure-form", async (d) => {
    const stems = Array.from({ length: 5 }, (_, i) => ({
      weight_g: +d[`weight_${i}`],
      length_cm: +d[`length_${i}`],
      position: d[`position_${i}`],
      excluded: d[`excluded_${i}`] === "on",
      exclusion_reason: d[`excluded_${i}`] === "on" ? d[`reason_${i}`] : null,
    }));
    if (
      new Set(stems.map((s) => s.position.trim().toLocaleLowerCase())).size !==
      5
    )
      throw new Error("Gebruik vijf verschillende meetplekken.");
    if (stems.every((s) => s.excluded))
      throw new Error("Minimaal één tak moet meetellen.");
    if (
      stems.some((s) => s.excluded) &&
      !confirm(
        "Uitgesloten takken blijven bewaard, maar tellen niet mee in het gemiddelde. Bevestig deze keuze.",
      )
    )
      return;
    const body = {
      client_id: newClientId(),
      cultivation_id: +d.cultivation_id,
      kind: d.kind,
      measured_at: d.measured_at,
      location: d.location,
      notes: d.notes,
      stems,
    };
    await saveQueue(body);
    clearTimeout(draftTimer);
    await localOp("drafts", "readwrite", (s) => s.delete(draftKey));
    const selected = d.cultivation_id;
    form.reset();
    form.elements.cultivation_id.value = selected;
    form.elements.measured_at.value = localNow();
    for (let i = 0; i < 5; i++) form.elements[`reason_${i}`].hidden = true;
    $("#draft-status").textContent = "Meting lokaal bewaard.";
    toast("Meting veilig bewaard op dit apparaat");
    await drawQueue();
    syncQueue();
  });
}
async function imports() {
  const alive = pageOwner("imports");
  if (!alive()) return;
  clearInterval(poll);
  if (location.hash.split("/")[1] === "history") return historicalImports();
  if (location.hash.split("/")[1] === "lighting") return legacyLighting();
  const jobs = list(await api("/api/imports"));
  if (!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Priva-gegevens importeren",
      "Van CSV of ZIP naar controleerbare historie. Bekijk eerst het voorbeeld, bevestig daarna.",
    ) +
    importTabs("priva") +
    `<section class="card"><form id="import-form"><div class="upload"><div style="font-size:27px;color:var(--green);margin-bottom:8px" aria-hidden="true">↥</div><h2>Historie toevoegen</h2><p class="sub" style="font-size:13px">Selecteer één of meer CSV-bestanden, of een ZIP met mappen.</p><label for="files" class="hint">Bestanden kiezen</label><input id="files" name="files" type="file" accept=".csv,.zip" multiple required></div><p class="hint">Werkelijke rijdatums zijn leidend. Een weeknummer of bestandsnaam bepaalt nooit het gegevensjaar.</p>${formError()}<button type="submit" class="primary">Uploaden & voorbeeld maken</button></form></section><section class="card"><div class="section-title"><h2>Importgeschiedenis</h2><button id="refresh-imports" class="small">↻ Vernieuwen</button></div><div id="import-list">${jobs.length ? jobs.map(importRow).join("") : empty("Nog geen imports", "Je oorspronkelijke bestanden en de herkomst blijven bewaard.")}</div></section>`;
  $("#refresh-imports").onclick = route;
  bindForm("#import-form", async (_, form) => {
    const fd = new FormData();
    for (const f of $("#files").files) fd.append("files", f);
    const job = await api("/api/imports", { method: "POST", body: fd });
    if (!alive()) return;
    toast("Bestanden ontvangen. Het voorbeeld wordt voorbereid.");
    await imports();
    if (alive()) await showImport(job.id);
  });
  $$("[data-import]").forEach(
    (b) => (b.onclick = () => showImport(b.dataset.import)),
  );
  if (
    jobs.some((j) => ["queued", "previewing", "importing"].includes(j.status))
  )
    poll = setInterval(async () => {
      if (!$("#modal").open) await imports();
    }, 5000);
}
function importRow(j) {
  return `<article style="padding:16px 0;border-bottom:1px solid var(--line)"><div class="section-title"><div><h3>Import #${esc(j.id)} ${tag(statusLabel(j.status), j.status === "failed" ? "warn" : "")}</h3><span class="hint">${date(j.created_at)} · ${fmt(j.progress_rows || 0)} / ${fmt(j.total_rows || 0)} rijen</span></div><button class="small" data-import="${esc(j.id)}">Controleren →</button></div>${["queued", "previewing", "importing"].includes(j.status) ? `<progress aria-label="Importvoortgang" value="${j.progress_rows || 0}" max="${j.total_rows || 1}"></progress>` : ""}${j.error ? `<p class="error-text">${esc(j.error)}</p>` : ""}</article>`;
}
async function showImport(id) {
  let j = await api(`/api/imports/${id}`);
  let jobTimer;
  const dialog = modal(
    `Import #${id} · ${statusLabel(j.status)}`,
    `<div id="import-detail"></div>`,
  );
  dialog.addEventListener("close", () => clearTimeout(jobTimer), {
    once: true,
  });
  async function draw() {
    clearTimeout(jobTimer);
    if (!$("#import-detail") || !dialog.open) return;
    $(".modal-head h2", dialog).textContent =
      `Import #${id} · ${statusLabel(j.status)}`;
    const p = j.preview || {};
    $("#import-detail").innerHTML =
      `<div class="notice">Originele bronnen blijven bewaard. Dubbele uploads veranderen bestaande meetwaarden niet.</div>${j.error ? `<p class="notice error">${esc(j.error)}</p>` : ""}<p><strong>${fmt(j.progress_rows || 0)}</strong> / ${fmt(j.total_rows || 0)} rijen verwerkt</p>${p.files?.length ? `<div class="table-wrap"><table><thead><tr><th>Bestand / profiel</th><th>Periode</th><th>Afdeling</th><th>Rijen</th></tr></thead><tbody>${p.files.map((f) => `<tr><td>${esc(f.name)}<br><span class="hint">${esc(f.profile)}</span></td><td>${date(f.start)} – ${date(f.end)}<br>${esc((f.years || []).join(", "))}</td><td>${esc((f.departments || []).join(", ")) || "—"}</td><td>${fmt(f.row_count)}</td></tr>`).join("")}</tbody></table></div>` : ""}${p.quality ? `<details open><summary>Kwaliteitscontroles</summary>${qualityView(p.quality)}</details>` : ""}${p.source_priority ? `<details><summary>Bronprioriteit</summary>${json(p.source_priority)}</details>` : ""}${
        p.files?.some((f) => f.sample?.length)
          ? `<details><summary>Controlevoorbeeld ruwe rijen</summary>${p.files
              .filter((f) => f.sample?.length)
              .map((f) => `<h3>${esc(f.name)}</h3>${json(f.sample)}`)
              .join("")}</details>`
          : ""
      }<details><summary>Volledig importverslag en herkomst</summary>${json(j)}</details><div class="actions" style="margin-top:15px">${j.status === "preview" ? '<button class="primary" id="confirm-import">Definitief importeren</button>' : ""}${j.status === "failed" ? '<button id="resume-import">Import hervatten</button>' : ""}${!["revoked", "previewing", "queued", "importing"].includes(j.status) ? '<button class="danger" id="revoke-import">Import herroepen</button>' : ""}<button id="refresh-job">↻ Status ophalen</button></div>`;
    if ($("#confirm-import"))
      $("#confirm-import").onclick = async () => {
        if (
          !confirm(
            "Het voorbeeld is gecontroleerd. Deze brongegevens definitief importeren?",
          )
        )
          return;
        await action("confirm");
      };
    if ($("#resume-import"))
      $("#resume-import").onclick = () => action("resume");
    if ($("#revoke-import"))
      $("#revoke-import").onclick = async () => {
        if (
          confirm(
            "Deze import herroepen? De bronnen en het auditspoor blijven bestaan.",
          )
        )
          await action("revoke");
      };
    $("#refresh-job").onclick = async () => {
      try {
        j = await api(`/api/imports/${id}`);
        await draw();
      } catch (error) {
        toast(error.message);
      }
    };
    applyPermissions();
    if (["queued", "previewing", "importing"].includes(j.status))
      jobTimer = setTimeout(async () => {
        try {
          j = await api(`/api/imports/${id}`);
          await draw();
        } catch (error) {
          toast("Importstatus niet bereikbaar: " + error.message);
        }
      }, 2000);
  }
  async function action(name) {
    try {
      j = await api(`/api/imports/${id}/${name}`, { method: "POST" });
      toast("Importstatus bijgewerkt");
      draw();
    } catch (e) {
      toast(e.message);
    }
  }
  await draw();
  applyPermissions();
}
function qualityView(value) {
  if (value == null)
    return '<p class="hint">Geen kwaliteitsrapport beschikbaar.</p>';
  if (Array.isArray(value))
    return value.length
      ? value
          .map(
            (x) =>
              `<p class="notice warn">${esc(typeof x === "string" ? x : JSON.stringify(x))}</p>`,
          )
          .join("")
      : '<p class="hint">Geen aanvullende meldingen.</p>';
  if (typeof value === "object")
    return Object.entries(value)
      .map(
        ([key, v]) =>
          `<p class="hint"><strong>${esc(key.replaceAll("_", " "))}:</strong> ${esc(typeof v === "object" ? JSON.stringify(v) : v)}</p>`,
      )
      .join("");
  return `<p class="hint">${esc(value)}</p>`;
}
async function climate() {
  const alive = pageOwner("climate");
  if (!alive()) return;
  const end = today(),
    start = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
  $("#main").innerHTML =
    pageHead(
      "Klimaat & licht",
      "Werkelijke Priva-metingen, bronkwaliteit en seizoenscontext.",
    ) +
    `<section class="card"><form id="climate-form"><div class="form-grid">${field("start", "Van", "date", start, "required")}${field("end", "Tot en met", "date", end, "required")}<div class="field"><label for="bay_number">Kap (optioneel)</label><select id="bay_number" name="bay_number"><option value="">Alle objecten</option>${Array.from({ length: 40 }, (_, i) => `<option value="${i + 1}">Kap ${i + 1}</option>`).join("")}</select></div></div>${formError()}<button type="submit" class="primary">Gegevens bekijken</button></form></section><div id="climate-result"></div>`;
  bindForm("#climate-form", load);
  await load(payload($("#climate-form")));
  async function load(d) {
    const params = new URLSearchParams(Object.entries(d).filter(([, v]) => v));
    const c = await api("/api/climate?" + params);
    if (!alive() || !$("#climate-result")) return;
    $("#climate-result").innerHTML =
      `<div class="grid"><div class="card metric"><div class="label">Lichtsom lampen</div><div class="value">${fmt(c.derived?.lamp_dli)}</div><div class="hint">mol/m² · geselecteerde periode</div></div><div class="card metric"><div class="label">Lichtsom daglicht</div><div class="value">${fmt(c.derived?.daylight_dli)}</div><div class="hint">mol/m² · geselecteerde periode</div></div><div class="card metric"><div class="label">Totale lichtsom</div><div class="value">${fmt(c.derived?.total_dli)}</div><div class="hint">Alleen bij geverifieerde invoer</div></div></div><section class="card section"><h2>Datakwaliteit</h2>${qualityView(c.quality)}${qualityView(c.derived?.reasons)}${c.mapping_uncertain ? '<p class="notice warn">Een of meer historische koppelingen zijn onzeker. Controleer de ingangsdatums in Beheer.</p>' : ""}${c.conflicts?.length ? `<details><summary>Bronconflicten (${c.conflicts.length})</summary>${json(c.conflicts)}</details>` : ""}</section>${c.derived?.daily?.length ? `<section class="card"><h2>Lichtsom per dag</h2><p class="hint">Alleen volledige dagen met bevestigde broninstellingen krijgen een lichtsom. Een streep betekent onbekend.</p><div class="table-wrap"><table><thead><tr><th>Dag</th><th>Lampen</th><th>Daglicht</th><th>Totale DLI</th><th>Kwaliteit</th></tr></thead><tbody>${c.derived.daily.map((day) => `<tr><td>${date(day.date)}</td><td>${fmt(day.lamp_dli)}</td><td>${fmt(day.daylight_dli)}</td><td>${fmt(day.total_dli)}</td><td>${esc(typeof day.quality === "object" ? JSON.stringify(day.quality) : day.quality)}</td></tr>`).join("")}</tbody></table></div><p class="hint">DLI in mol/m² per dag.</p></section>` : ""}<section class="card"><h2>Meetreeksen (${c.series?.length || 0})</h2>${c.series?.length ? c.series.map((s) => `<details><summary>${esc(s.label)} · ${esc(s.object_type)} ${esc(s.object_number)} ${s.channel ? "· kanaal " + esc(s.channel) : ""}</summary><p class="hint">Eenheid: ${esc(s.unit || "onbevestigd")} · ${s.dates?.length || 0} dag(en)</p><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Waarde</th><th>Aantal</th><th>Kwaliteit</th></tr></thead><tbody>${(s.dates || []).map((v) => `<tr><td>${date(v.date)}</td><td>${fmt(v.value)}</td><td>${fmt(v.count)}</td><td>${esc(typeof v.quality === "object" ? JSON.stringify(v.quality) : v.quality)}</td></tr>`).join("")}</tbody></table></div></details>`).join("") : empty("Nog geen meetreeksen in deze periode", "Importeer Priva-bestanden of selecteer een andere periode.")}</section><div class="notice">De lengte van de dag verandert met het seizoen. Dat is iets anders dan gemeten straling in de kas. Regen, bewolking, schermen en lampen bepalen de ontvangen lichtsom.</div>`;
  }
}
function modelMetrics(metrics) {
  const model = metrics.model || {},
    baseline = metrics.baseline || {};
  return `<div class="table-wrap"><table><tbody><tr><th>MAE model</th><td>${fmt(model.mae_g, " g")}</td></tr><tr><th>MAE referentie (dezelfde teelten)</th><td>${fmt(baseline.mae_g, " g")}</td></tr><tr><th>MAE basismodel (dezelfde teelten)</th><td>${fmt(metrics.basic_reference?.mae_g, " g")}</td></tr><tr><th>Bias model</th><td>${fmt(model.bias_g, " g")}</td></tr><tr><th>Dekking 80%-interval</th><td>${model.interval_80_coverage != null ? fmt(model.interval_80_coverage * 100, "%") : "Onbekend"}</td></tr><tr><th>Geëvalueerde latere rondes</th><td>${fmt(metrics.unseen_rounds)}</td></tr><tr><th>Vrijgavebesluit</th><td>${metrics.release_gate === "passed" ? "Criteria gehaald" : "Experimenteel"}</td></tr></tbody></table></div>`;
}
async function models() {
  const alive = pageOwner("models");
  if (!alive()) return;
  const data = await api("/api/models"),
    rows = list(data),
    scenarios = list(await api("/api/scenarios"));
  if (!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Gewichtsverwachting",
      "Een eerlijke prognose begint met complete gegevens en onafhankelijke toetsing.",
      '<a class="btn" href="#model-uitleg">Hoe werkt de voorspelling?</a>' + (user.role === "admin"
        ? '<button class="primary" id="train-model">Nieuw model toetsen</button>'
        : ""),
    ) +
    `<div class="notice"><strong>Vrijgave is een meetbaar besluit.</strong> Minimaal ${esc(appSettings.min_training_cultivations ?? 30)} complete standaardteelten, toetsing op ${esc(appSettings.required_unseen_rounds ?? 2)} latere volledig ongeziene rondes en verbetering tegenover dezelfde historische referentie. De genoemde 3,8 g is een doelreferentie, geen gemeten prestatie van deze app.</div><div class="two"><section class="card"><h2>Prognose per kapteelt</h2><form id="forecast-form">${selectCult()}${field("as_of", "Invoerpeildatum (optioneel)", "datetime-local")}${formError()}<button class="primary" type="submit">Verwachting bekijken</button></form><div id="forecast-result"></div></section><section class="card"><h2>Modelhistorie</h2>${rows.length ? rows.map((m) => `<article style="padding:15px 0;border-bottom:1px solid var(--line)"><h3>${esc(m.kind || m.model_kind || "Model")} #${esc(m.id)} ${tag(statusLabel(m.active ? "operational" : "experimental"), m.active ? "" : "warn")}</h3><p class="hint">${date(m.created_at)} · ${profileLabel(m.features?.spec?.profile || "basic")} · ${esc(m.data_hash || m.training_data_hash || "")}</p>${m.metrics ? modelMetrics(m.metrics) : ""}${m.release_reasons ? qualityView(m.release_reasons) : ""}<details><summary>Validatie en kenmerken</summary>${json(m)}</details>${user.role === "admin" ? `<button class="small" data-activate="${m.id}" ${m.operational_eligible !== true ? 'disabled title="Voldoet nog niet aan vrijgavecriteria"' : ""}>Operationeel activeren</button>` : ""}</article>`).join("") : empty("Nog geen getraind model", "Zonder betrouwbare oogstlabels blijft de verwachting onbekend.")}</section></div><section class="card"><div class="section-title"><h2>Handmatige toekomstscenario’s</h2><button id="new-scenario" class="small">＋ Scenario</button></div><p class="hint">Buitenweer en teeltinstellingen voor de nog onbekende periode. Handmatige invoer is geen waargenomen klimaat. Externe provider staat uit.</p>${scenarios.length ? scenarios.map((s) => `<details><summary>${esc(s.name || s.label || s.scenario || "Scenario")} · geldig ${date(s.valid_at)}</summary>${json(s)}</details>`).join("") : empty("Nog geen scenario’s", "Leg eventueel een donker, gemiddeld of licht toekomstscenario vast.")}</section><section class="card" id="control-comparison"></section>`;
  bindForm("#forecast-form", async (d) => {
    const f = await api(
      `/api/cultivations/${d.cultivation_id}/forecast${d.as_of ? "?as_of=" + encodeURIComponent(d.as_of) : ""}`,
    );
    if (!alive() || !$("#forecast-result")) return;
    $("#forecast-result").innerHTML = forecastView(f);
  });
  if ($("#train-model"))
    $("#train-model").onclick = async () => {
      if (
        !confirm(
          "Een nieuwe experimentele modelversie trainen en valideren met de huidige gegevens?",
        )
      )
        return;
      try {
        const result = await api("/api/models/train", {
          method: "POST",
          body: {},
        });
        toast("Modeltoetsing afgerond");
        modal(
          "Resultaat modeltoetsing",
          result.models
            .map((m) =>
              m.status === "insufficient_data"
                ? `<p class="notice warn"><strong>${esc(m.kind)}</strong>: ${esc(m.complete_standard_cultivations)} complete standaardteelten, minimaal ${esc(m.required)} vereist. Nog niet getraind.</p>`
                : `<h3>${esc(m.kind)}</h3>${modelMetrics(m.metrics || {})}`,
            )
            .join(""),
        );
        models();
      } catch (e) {
        toast(e.message);
      }
    };
  $$("[data-activate]").forEach(
    (b) =>
      (b.onclick = async () => {
        if (
          !confirm(
            "Dit model operationeel activeren? De server controleert eerst alle vrijgavecriteria.",
          )
        )
          return;
        try {
          await api(`/api/models/${b.dataset.activate}/activate`, {
            method: "POST",
          });
          toast("Model geactiveerd");
          models();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
  $("#new-scenario").onclick = scenarioForm;
  await comparisonPanel();
}
function modelExplanation() {
  $("#main").innerHTML = pageHead(
    "Zo werkt de voorspelling",
    "In gewone taal: van vijf gemeten takken naar een verwacht oogstgewicht.",
    '<a class="btn" href="#models">Naar de prognoses →</a>',
  ) + `<article class="model-guide" aria-label="Uitleg van het voorspelmodel">
    <section class="card guide-intro">
      <div class="eyebrow">Het idee</div>
      <h2>Wat zouden deze takken bij de oogst kunnen wegen?</h2>
      <p>Je meet hoe de takken er nu voor staan. De app zoekt in eerdere teelten naar verbanden tussen zulke metingen en het uiteindelijke oogstgewicht. Daarmee maakt hij een schatting van het <strong>gewicht per tak bij de oogst</strong>.</p>
      <p>Het is een hulpmiddel voor jouw beoordeling van de teelt. Er zit geen glazen bol in de app.</p>
    </section>

    <section class="card">
      <h2>Zo gaat het in vier stappen</h2>
      <ol class="guide-steps">
        <li><strong>Jij meet vijf takken.</strong><p>Je voert het gewicht en de lengte in. De app gebruikt het gemiddelde, zodat één lichte of zware tak minder invloed heeft. Meet op vaste plekken volgens dezelfde werkwijze.</p></li>
        <li><strong>De app kijkt naar de teelt.</strong><p>Welke soort is het? Hoe dicht staan de planten? Hoeveel dagen zitten ze in de korte dag? En in welk deel van het jaar groeien ze?</p></li>
        <li><strong>Een model gebruikt wat eerdere teelten hebben geleerd.</strong><p>Een model is hier gewoon een rekenregel die uit eerdere metingen en oogstgewichten is gemaakt. Hij berekent een verwachting voor deze teelt.</p></li>
        <li><strong>Je krijgt een schatting met een marge.</strong><p>Je ziet een verwacht gewicht, de onzekerheid en welke gegevens zijn gebruikt. Als bruikbare gegevens ontbreken, kan de uitkomst onbekend blijven.</p></li>
      </ol>
    </section>

    <div class="two">
      <section class="card">
        <h2>Waarom meten rond KD10 en KD30?</h2>
        <p><strong>KD betekent korte dag:</strong> de dagen sinds de datum ‘licht uit’ in je planning.</p>
        <p>Rond <strong>KD10</strong> maakt de app een eerste modelverwachting met de eerste meting. Rond <strong>KD30</strong> kan een apart model ook de tweede meting en de groei tussen de metingen gebruiken.</p>
        <p>De echte meetdag telt mee. Een meting op dag 11 wordt dus niet behandeld alsof die precies op dag 10 is gedaan.</p>
      </section>
      <section class="card">
        <h2>Worden de dagen langer of korter?</h2>
        <p>Een teelt in het voorjaar groeit naar langere dagen toe. In het najaar worden de dagen korter. De app neemt de tijd van het jaar, de berekende daglengte en die richting mee.</p>
        <p>Dat vertelt hoeveel daglicht er <em>mogelijk</em> is. Het vertelt niet hoeveel zon er door de bewolking en de kas heen werkelijk bij de planten komt.</p>
      </section>
    </div>

    <section class="card">
      <h2>En licht, temperatuur en water?</h2>
      <p>Dat hangt af van het gekozen model. Het <strong>basismodel</strong> gebruikt de teelt, takmetingen en het seizoen. Een <strong>lichtmodel</strong> gebruikt ook gecontroleerde lamplichtgegevens. Een <strong>klimaatmodel</strong> kan daarnaast temperatuur, luchtvochtigheid en watergift gebruiken.</p>
      <p><strong>Priva gaat altijd voor.</strong> Excel-belichting kan alleen aanvullen waar Priva-uren ontbreken. Ook een lege of onbetrouwbare Priva-urenwaarde wordt niet stilletjes door Excel vervangen. Voor een lichtsom moeten de betekenis van de Excel-percentages en de lampcapaciteit eerst zijn gecontroleerd.</p>
      <p>Een ontbrekende meting betekent ‘we weten het niet’. De app maakt daar geen nul van. Bij onvoldoende gegevens kan hij terugvallen op een beschikbaar eenvoudiger model of een historisch gemiddelde. Je ziet welke uitkomst wordt gebruikt.</p>
    </section>

    <section class="card">
      <h2>Wat betekenen de meldingen?</h2>
      <dl class="guide-statuses">
        <div><dt>Onbekend</dt><dd>Er is nog geen geschikte modelvoorspelling of betrouwbare historische referentie beschikbaar.</dd></div>
        <div><dt>Historische referentie</dt><dd>Dit is een gemiddelde van betrouwbare eerdere oogsten van die soort. Het is nog geen voorspelling op basis van jouw takmetingen.</dd></div>
        <div><dt>Experimenteel</dt><dd>Er is een model, maar het is nog niet vrijgegeven voor normaal gebruik.</dd></div>
        <div><dt>Vrijgegeven</dt><dd>Het model heeft de ingestelde controles gehaald en een beheerder heeft het aangezet. Ook dit model kan ernaast zitten.</dd></div>
      </dl>
    </section>

    <section class="card guide-example">
      <div class="eyebrow">Verzonnen voorbeeld · geen echte voorspelling</div>
      <h2>Doel 105 gram, verwachting 100 gram: hoe kan dat?</h2>
      <p><strong>105 gram is wat je wilt halen.</strong> De verwachting van 100 gram is wat de beschikbare gegevens op dat moment doen vermoeden. Het doel verandert die schatting niet automatisch.</p>
      <p>Staat er bijvoorbeeld een marge van 94 tot 106 gram bij? Dan laat die zien dat de uitkomst onzeker is. Het echte gewicht kan ook buiten die marge vallen.</p>
      <p>Een ‘80%-marge’ is bedoeld om ongeveer 8 van de 10 uitkomsten te omvatten. Of dat in jullie teelt ook lukt, moet uit de toetsing blijken. Het is geen garantie.</p>
    </section>

    <section class="card">
      <h2>Hoe leert de app, en hoe controleren we hem?</h2>
      <p>Je registreert ook het echte oogstgewicht. Een beheerder kan met voldoende complete teelten een nieuw model laten maken. Dat gebeurt niet automatisch na iedere invoer.</p>
      <p>De app oefent op eerdere rondes en wordt getest op latere rondes die hij nog niet heeft gezien. Hij mag daarbij niet alvast het latere oogstgewicht of later ingevoerde gegevens gebruiken. We vergelijken zijn fouten met een eenvoudige historische referentie. Een uitgebreider model moet ook beter presteren dan het basismodel.</p>
      <p>Een lager gemiddeld verschil tussen voorspelling en oogst is beter. Genoeg teelten hebben is alleen het begin: de verbetering moet ook op nieuwe rondes zichtbaar zijn voordat een beheerder het model kan vrijgeven.</p>
    </section>

    <section class="card">
      <h2>Wat kun jij doen voor een betere voorspelling?</h2>
      <ul class="guide-tips">
        <li>Meet steeds vijf takken op de afgesproken manier en voer de echte meetdatum in.</li>
        <li>Houd soort, plantdatum, licht-uitdatum en plantdichtheid goed bij.</li>
        <li>Controleer importmeldingen en registreer het echte oogstgewicht zorgvuldig.</li>
        <li>Bekijk bij een voorspelling ook de status, de marge en ontbrekende gegevens.</li>
      </ul>
      <p>De app kan nog niet betrouwbaar zeggen hoeveel extra grammen een extra uur licht, water of een andere ingreep oplevert. Een verband in oude teelten is nog geen bewijs van het effect van zo’n verandering.</p>
      <a href="#models">Bekijk de verwachtingen en modelstatus →</a>
    </section>
  </article>`;
}
function scenarioForm() {
  modal(
    "Handmatig toekomstscenario",
    `<p class="hint">Gebruik uitsluitend waarden die op de uitgiftedatum beschikbaar zijn. Dit vervangt geen gemeten kaslicht.</p><form id="scenario-form">${field("name", "Naam scenario", "text", "", 'required placeholder="Bijv. Donkere komende week"')}${selectCult()}<div class="form-grid">${field("issued_at", "Uitgiftetijd", "datetime-local", localNow(), "required")}${field("valid_at", "Geldig voor", "datetime-local", localNow(), "required")}${field("outside_radiation_j_cm2", "Buitenstraling (J/cm², optioneel)", "number", "", 'min="0" step="0.1"')}${field("outside_temp_c", "Buitentemperatuur (°C, optioneel)", "number", "", 'step="0.1"')}${field("planned_lamp_hours", "Geplande branduren per dag", "number", "", 'min="0" max="24" step="0.1"')}${field("cloud_fraction", "Bewolking (fractie: 0–1)", "number", "", 'min="0" max="1" step="0.01"')}</div>${formError()}<button type="submit" class="primary">Scenario bewaren</button></form>`,
  );
  bindForm("#scenario-form", async (d) => {
    const body = { ...d, cultivation_id: +d.cultivation_id };
    for (const k of [
      "outside_radiation_j_cm2",
      "outside_temp_c",
      "planned_lamp_hours",
      "cloud_fraction",
    ])
      body[k] = d[k] ? +d[k] : null;
    await api("/api/scenarios", { method: "POST", body });
    $("#modal").close();
    models();
  });
}
async function manage() {
  const alive = pageOwner("manage");
  if (!alive()) return;
  const mappings = list(await api("/api/mappings"));
  if (!alive()) return;
  let users = [],
    audits = [],
    backups = [],
    calibrations = [];
  if (user.role === "admin") {
    const r = await Promise.allSettled([
      api("/api/users"),
      api("/api/audit"),
      api("/api/backups"),
      api("/api/calibrations"),
    ]);
    [users, audits, backups, calibrations] = r.map((x) =>
      x.status === "fulfilled" ? list(x.value) : [],
    );
  }
  if (!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Bedrijfsinstellingen",
      "Soorten, koppelingen en historie. Wijzigingen blijven traceerbaar.",
    ) +
    `${user.role === "admin" ? '<section class="card" id="general-settings"><p class="hint">Algemene instellingen laden…</p></section><section class="card" id="crop-profiles"><p class="hint">Seizoensprofielen laden…</p></section>' : ""}<div class="two"><div><section class="card"><div class="section-title"><h2>Soorten & doelgewicht</h2><button id="new-variety" class="small">＋ Soort</button></div><div class="table-wrap"><table><thead><tr><th>Soort</th><th>Doelgewicht</th><th></th></tr></thead><tbody>${varieties.map((v) => `<tr><td>${esc(v.name)} ${v.active === false ? tag("Historisch / inactief", "gray") : ""}</td><td>${fmt(v.target_weight_g, " g")}</td><td>${user.role === "admin" ? `<button class="small" data-variety="${v.id}">Wijzigen</button>` : ""}</td></tr>`).join("")}</tbody></table></div></section><section class="card"><div class="section-title"><h2>Koppelingen met ingangsdatum</h2><button id="new-mapping" class="small">＋ Koppeling</button></div><p class="hint">Oude situaties blijven historisch intact. Kranen 41–45 hebben geen bevestigde kaptoewijzing.</p><div class="field"><label for="mapping-filter">Kap</label><select id="mapping-filter">${Array.from({ length: 40 }, (_, i) => `<option value="${i + 1}">Kap ${i + 1}</option>`).join("")}</select></div><div id="mapping-list"></div></section><section class="card"><h2>Oppervlakte per kap</h2><p class="hint">Een onbekende oppervlakte blijft leeg; zij wordt niet geschat.</p><form id="bay-settings"><div class="form-grid"><div class="field"><label for="bay-edit">Kap</label><select id="bay-edit" name="number">${options(bays, "number", (b) => "Kap " + b.number)}</select></div>${field("area_m2", "Oppervlakte (m²)", "number", bays[0]?.area_m2 || "", 'min="0.1" step="0.1"')}</div>${formError()}<button type="submit">Oppervlakte bewaren</button></form></section></div><div>${user.role === "admin" ? `<section class="card"><div class="section-title"><h2>Gebruikers</h2><button id="new-user" class="small">＋ Account</button></div>${users.map((u) => `<div style="display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--line)"><span><strong>${esc(u.username)}</strong><br><small class="muted">${esc(u.role)} · ${u.active === false ? "inactief" : "actief"}</small></span><button class="small" data-user="${u.id}">Beheren</button></div>`).join("")}</section><section class="card"><h2>Export & back-up</h2><p class="hint">Exporteer de bedrijfsregistratie of maak een consistente serverback-up inclusief bestanden.</p><div class="actions"><a class="btn" href="/api/export">↓ Volledige export</a><button id="backup-now">＋ Back-up maken</button></div><div id="backup-result"></div>${backups.length ? `<details><summary>Bewaarde back-ups (${backups.length})</summary>${backups.map((b) => `<p class="hint"><a href="/api/backups/${encodeURIComponent(b.filename)}">${esc(b.filename)}</a> · ${fmt(b.size / 1024, " kB")} · ${date(b.created_at)}</p>`).join("")}</details>` : ""}</section>` : '<section class="notice">Alleen beheerders kunnen gebruikers, oppervlakte, kalibraties en back-ups wijzigen.</section>'}</div></div>${
      user.role === "admin"
        ? `<section class="card" id="koppelsleutels"><p class="hint">Koppelsleutels laden…</p></section><section class="card"><div class="section-title"><h2>Gevalideerde eenheden & lichtinstellingen</h2><button id="new-calibration">＋ Validatie vastleggen</button></div><p class="hint">Leg alleen bevestigde bronbetekenissen en instellingen vast. Zonder bevestigde eenheid, lampkanaal of scherminvloed blijft de DLI ongeverifieerd.</p>${calibrations.length ? calibrations.map((c) => `<details><summary>${esc(c.kind)} · ${esc(c.label || "")} · ${esc(c.object_type || "")} ${esc(c.object_number || "")} · vanaf ${date(c.valid_from)}</summary>${json(c)}</details>`).join("") : empty("Nog geen bronvalidaties", "De actuele installatieconfiguratie wordt niet naar historische jaren teruggeprojecteerd.")}<details><summary>Laatst geregistreerde wijzigingen</summary>${
            audits.length
              ? `<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Actie</th><th>Object</th><th>Details</th></tr></thead><tbody>${audits
                  .slice(0, 100)
                  .map(
                    (a) =>
                      `<tr><td>${date(a.created_at)}</td><td>${esc(a.action)}</td><td>${esc(a.entity)}</td><td>${esc(JSON.stringify(a.details))}</td></tr>`,
                  )
                  .join("")}</tbody></table></div>`
              : empty("Geen auditregels beschikbaar")
          }</details></section>`
        : ""
    }`;
  if (user.role === "admin")
    await Promise.allSettled([settingsPanel(), cropProfilesPanel(), koppelsleutelsPanel()]);
  if (!alive()) return;
  function drawMappings() {
    const selected = +$("#mapping-filter").value;
    const rows = mappings.filter((m) => m.bay_number === selected);
    $("#mapping-list").innerHTML =
      rows
        .map(
          (m) =>
            `<article style="padding:11px 0;border-bottom:1px solid var(--line)"><strong>${{ department: "Afdeling", valve: "Kraan", light: "Lichtmodule" }[m.object_type]} ${esc(m.object_number)}</strong> ${m.valid_until ? tag("Historisch", "gray") : tag("Geldig vanaf " + date(m.valid_from))}<p class="hint">${date(m.valid_from)} → ${m.valid_until ? date(m.valid_until) : "heden"} · ${esc(m.reason)}</p>${user.role === "admin" ? `<button class="small" data-replace="${m.id}">Opvolgende versie</button>` : ""}</article>`,
        )
        .join("") || empty("Geen koppelingen");
    $$("[data-replace]").forEach(
      (b) =>
        (b.onclick = () =>
          mappingForm(mappings.find((m) => m.id === +b.dataset.replace))),
    );
  }
  $("#mapping-filter").onchange = drawMappings;
  drawMappings();
  $("#new-mapping").onclick = () => mappingForm();
  $("#new-variety").onclick = () => {
    modal(
      "Soort toevoegen",
      `<form id="variety-form">${field("name", "Soortnaam", "text", "", "required")}${field("target_weight_g", "Doelgewicht per tak (g)", "number", 105, 'required min="0.1" max="1000" step="0.1"')}${formError()}<button type="submit" class="primary">Soort opslaan</button></form>`,
    );
    bindForm("#variety-form", async (d) => {
      await api("/api/varieties", {
        method: "POST",
        body: { ...d, target_weight_g: +d.target_weight_g },
      });
      $("#modal").close();
      await getBase();
      manage();
    });
  };
  $$("[data-variety]").forEach(
    (b) =>
      (b.onclick = () => {
        const v = varieties.find((v) => v.id === +b.dataset.variety);
        modal(
          `Doelgewicht · ${v.name}`,
          `<form id="target-form">${field("target_weight_g", "Doelgewicht per tak (g)", "number", v.target_weight_g, 'required min="0.1" max="1000" step="0.1"')}<label class="inline field"><input name="active" type="checkbox" ${v.active !== false ? "checked" : ""}> Beschikbaar voor nieuwe teelten</label><p class="hint">Een inactieve soort blijft in de historische registratie bewaard.</p>${formError()}<button class="primary" type="submit">Bewaren</button></form><details id="variety-target-history"><summary>Doelgewichthistorie laden…</summary></details>`,
        );
        varietyTargetHistory(v.id);
        bindForm("#target-form", async (d) => {
          await api(`/api/varieties/${v.id}`, {
            method: "PATCH",
            body: {
              target_weight_g: +d.target_weight_g,
              active: d.active === "on",
            },
          });
          $("#modal").close();
          await getBase();
          manage();
        });
      }),
  );
  $("#bay-edit").onchange = () => {
    $("#area_m2").value =
      bays.find((b) => b.number === +$("#bay-edit").value)?.area_m2 || "";
  };
  bindForm("#bay-settings", async (d) => {
    await api(`/api/bays/${d.number}`, {
      method: "PATCH",
      body: { area_m2: d.area_m2 ? +d.area_m2 : null },
    });
    await getBase();
    toast("Oppervlakte opgeslagen");
  });
  if (user.role === "admin") {
    $("#new-user").onclick = () => userForm();
    $$("[data-user]").forEach(
      (b) =>
        (b.onclick = () =>
          userForm(users.find((u) => u.id === +b.dataset.user))),
    );
    $("#new-calibration").onclick = calibrationForm;
    $("#backup-now").onclick = async () => {
      try {
        const b = await api("/api/backups", { method: "POST", body: {} });
        $("#backup-result").innerHTML =
          `<p class="notice" style="margin-top:15px">Back-up gemaakt: <a href="${esc(b.download_url)}">${esc(b.filename)}</a> (${fmt(b.size / 1024, " kB")})</p>`;
      } catch (e) {
        toast(e.message);
      }
    };
  }
}
const SETTINGS_GROUPS = [
  [
    "Teelt & seizoen",
    [
      ["latitude_deg", "Breedtegraad kas (°)", "-66", "66", "0.0001"],
      [
        "measurement_tolerance_days",
        "Tolerantie meetmoment (dagen)",
        "0",
        "7",
        "1",
      ],
      ["steering_until_kd", "Bijsturen tot en met KD-dag", "10", "100", "1"],
    ],
  ],
  [
    "Toegang & beveiliging",
    [
      ["session_hours", "Sessieduur (uur)", "1", "168", "1"],
      ["login_max_attempts", "Maximale inlogpogingen", "3", "50", "1"],
      [
        "login_window_minutes",
        "Venster inlogpogingen (minuten)",
        "1",
        "1440",
        "1",
      ],
    ],
  ],
  [
    "Automatische back-ups",
    [
      [
        "backup_interval_hours",
        "Interval tussen back-ups (uur)",
        "1",
        "168",
        "1",
      ],
      [
        "backup_keep_days",
        "Bewaartermijn (dagen; 0 = onbeperkt)",
        "0",
        "3650",
        "1",
      ],
    ],
  ],
  [
    "Importbronprioriteiten",
    [
      [
        "priority_algemeen_bedrijf",
        "Algemeen Bedrijf: prioriteit",
        "0",
        "100",
        "1",
      ],
      ["priority_klimaat", "Klimaat: prioriteit", "0", "100", "1"],
      ["priority_belichting", "Belichting: prioriteit", "0", "100", "1"],
      ["priority_kranen", "Kranen: prioriteit", "0", "100", "1"],
      [
        "priority_algemeen_afdeling",
        "Algemeen Afdeling: prioriteit",
        "0",
        "100",
        "1",
      ],
    ],
  ],
  [
    "Model & vrijgave",
    [
      [
        "ridge_alpha",
        "Ridge-regularisatie (alpha)",
        "0.0001",
        "10000",
        "0.0001",
      ],
      [
        "min_training_cultivations",
        "Minimum complete standaardteelten",
        "30",
        "10000",
        "1",
      ],
      [
        "required_unseen_rounds",
        "Vereiste latere ongeziene rondes",
        "2",
        "20",
        "1",
      ],
      [
        "environment_min_coverage",
        "Minimale dekking klimaat en licht (fractie)",
        "0.9",
        "1",
        "0.01",
      ],
      [
        "environment_min_training_cultivations",
        "Minimum complete teelten voor licht-/klimaatmodel",
        "30",
        "10000",
        "1",
      ],
    ],
  ],
];
async function settingsPanel() {
  const alive = pageOwner("manage");
  const panel = $("#general-settings");
  if (!panel) return;
  try {
    const result = await api("/api/settings");
    if (!alive() || !panel.isConnected) return;
    appSettings = result.settings;
    panel.innerHTML = `<h2>Algemene instellingen</h2><p class="hint">Elke wijziging krijgt een ingangsdatum en blijft in de historie bewaard. De ondergrenzen voor modelvrijgave kunnen niet worden verlaagd.</p><form id="settings-form">${SETTINGS_GROUPS.map(([title, fields]) => `<details ${title === "Teelt & seizoen" ? "open" : ""}><summary>${title}</summary><div class="form-grid" style="margin-top:16px">${fields.map(([key, label, min, max, step]) => field(key, label, "number", appSettings[key], `required min="${min}" max="${max}" step="${step}"`)).join("")}</div>${title === "Model & vrijgave" ? modelProfileFields(appSettings) : ""}${title === "Importbronprioriteiten" ? '<p class="hint">Bij conflicten wint de hogere prioriteit van een geldige bron. Bij gelijke prioriteit geldt de meest recent bevestigde bron. Ongeldige of ontbrekende waarden winnen niet van een geldige waarde. Deze instellingen gelden tussen Priva-profielen; aanvullende Excelbelichting blijft altijd ondergeschikt aan Priva.</p>' : ""}${title === "Automatische back-ups" ? '<p class="hint">0 bewaart back-ups onbeperkt. Bij een bewaartermijn worden oudere exemplaren pas na een geslaagde automatische back-up opgeruimd; de laatste blijft behouden.</p>' : ""}</details>`).join("")}${field("settings_effective_from", "Ingangsmoment (leeg = direct; Nederlandse tijd)", "datetime-local")}${area("settings_reason", "Reden voor wijziging")}${formError()}<button type="submit" class="primary">Instellingen als nieuwe versie bewaren</button></form><details><summary>Versiehistorie (${result.history?.length || 0})</summary>${result.history?.length ? result.history.map((v) => `<details><summary>Versie ${esc(v.id)} · vanaf ${date(v.effective_from)}</summary><p class="hint">${esc(v.reason)}</p><div class="table-wrap"><table><tbody>${SETTINGS_GROUPS.flatMap(([, fields]) => fields.map(([key, label]) => `<tr><td>${label}</td><td>${fmt(v.settings[key])}</td></tr>`)).join("")}<tr><td>Modelinvoerprofiel</td><td>${profileLabel(v.settings.model_feature_profile || "basic")}</td></tr><tr><td>Daglichtbenadering toegestaan</td><td>${v.settings.environment_include_daylight_estimate ? "Ja" : "Nee"}</td></tr></tbody></table></div></details>`).join("") : '<p class="hint">De zichtbare beginwaarden zijn nog niet als nieuwe versie gewijzigd.</p>'}</details><p class="hint">${esc(result.infrastructure)}</p>`;
    $("#settings_reason").required = true;
    $("#settings_reason").minLength = 3;
    bindForm("#settings-form", async (d) => {
      const settings = Object.fromEntries(
        SETTINGS_GROUPS.flatMap(([, fields]) =>
          fields.map(([key]) => [key, +d[key]]),
        ),
      );
      await api("/api/settings", {
        method: "POST",
        body: {
          settings: {
            ...settings,
            model_feature_profile: d.model_feature_profile,
            environment_include_daylight_estimate:
              d.environment_include_daylight_estimate === "on",
            environment_include_co2: d.environment_include_co2 === "on",
          },
          effective_from: d.settings_effective_from || null,
          reason: d.settings_reason,
        },
      });
      toast("Instellingen als nieuwe versie opgeslagen");
      await settingsPanel();
    });
  } catch (e) {
    if (!alive() || !panel.isConnected) return;
    panel.innerHTML = `<h2>Algemene instellingen</h2><p class="notice error">${esc(e.message)}</p>`;
  }
}
function cultivationState(c) {
  const choices = (values, selected) =>
    values
      .map(
        ([value, label]) =>
          `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`,
      )
      .join("");
  modal(
    "Teeltstatus & registratiekwaliteit",
    `<form id="state-form"><div class="field"><label for="status">Status</label><select id="status" name="status">${choices(
      [
        ["planned", "Gepland"],
        ["active", "Lopend"],
        ["harvested", "Geoogst"],
        ["historical", "Historisch"],
      ],
      c.status,
    )}</select></div><div class="field"><label for="category">Categorie</label><select id="category" name="category">${choices(
      [
        ["standard", "Standaard"],
        ["adjusted", "Bijgestuurd"],
        ["control", "Controle"],
      ],
      c.category,
    )}</select></div><div class="field"><label for="label_quality">Kwaliteit teeltlabel</label><select id="label_quality" name="label_quality">${choices(
      [
        ["verified", "Gecontroleerd"],
        ["historical_assigned", "Historisch / toegewezen"],
        ["ambiguous", "Gemengd / ambigue"],
        ["unknown", "Onbekend"],
      ],
      c.label_quality,
    )}</select></div><p class="hint">De status geoogst vereist een geregistreerde oogst. Ambigue of toegewezen labels worden afzonderlijk behandeld bij modeltraining.</p>${formError()}<button type="submit" class="primary">Teeltgegevens bewaren</button></form>`,
  );
  bindForm("#state-form", async (d) => {
    await api(`/api/cultivations/${c.id}`, { method: "PATCH", body: d });
    $("#modal").close();
    await getBase();
    route();
  });
}
function mappingForm(m) {
  modal(
    m ? "Opvolgende koppeling" : "Nieuwe koppeling",
    `<form id="mapping-form"><div class="form-grid">${field("bay_number", "Kap", "number", m?.bay_number || 1, 'required min="1" max="40"')}<div class="field"><label for="object_type">Type object</label><select id="object_type" name="object_type">${[
      ["department", "Afdeling"],
      ["valve", "Kraan"],
      ["light", "Lichtmodule"],
    ]
      .map(
        ([v, l]) =>
          `<option value="${v}" ${m?.object_type === v ? "selected" : ""}>${l}</option>`,
      )
      .join(
        "",
      )}</select></div>${field("object_number", "Objectnummer", "number", m?.object_number || "", 'required min="1"')}${field("valid_from", "Geldig vanaf", "date", today(), "required")}${field("valid_until", "Geldig tot (optioneel)", "date")}</div>${area("reason", "Onderbouwing van deze koppeling")}${formError()}<button class="primary" type="submit">Versie bewaren</button></form>`,
  );
  $("#reason").required = true;
  bindForm("#mapping-form", async (d) => {
    await api("/api/mappings", {
      method: "POST",
      body: {
        ...d,
        bay_number: +d.bay_number,
        object_number: +d.object_number,
        valid_until: d.valid_until || null,
        replaces_id: m?.id || null,
      },
    });
    $("#modal").close();
    manage();
  });
}
function userForm(u) {
  modal(
    u ? `Account · ${u.username}` : "Gebruiker toevoegen",
    `<form id="user-form">${!u ? field("username", "Gebruikersnaam", "text", "", 'required autocomplete="off"') : ""}${field("password", u ? "Nieuw wachtwoord (leeg = behouden)" : "Wachtwoord", "password", "", `${!u ? "required " : ""}minlength="12" autocomplete="new-password"`)}<div class="field"><label for="role">Rol</label><select id="role" name="role">${[
      ["admin", "Beheerder"],
      ["medewerker", "Medewerker"],
      ["lezer", "Alleen lezen"],
    ]
      .map(
        ([v, l]) =>
          `<option value="${v}" ${u?.role === v ? "selected" : ""}>${l}</option>`,
      )
      .join(
        "",
      )}</select></div>${u ? `<label class="inline field"><input type="checkbox" name="active" ${u.active !== false ? "checked" : ""}> Account actief</label>` : ""}${formError()}<button type="submit" class="primary">Account opslaan</button></form>`,
  );
  bindForm("#user-form", async (d) => {
    const body = { ...d };
    if (u) {
      body.active = d.active === "on";
      if (!body.password) delete body.password;
    }
    await api(u ? `/api/users/${u.id}` : "/api/users", {
      method: u ? "PATCH" : "POST",
      body,
    });
    $("#modal").close();
    manage();
  });
}
function calibrationForm(initialSource = "priva") {
  initialSource = typeof initialSource === "string" ? initialSource : "priva";
  modal(
    "Bronvalidatie vastleggen",
    `<form id="calibration-form"><div class="field"><label for="cal-kind">Wat is gecontroleerd?</label><select id="cal-kind" name="kind"><option value="unit">Eenheid van een meetreeks</option><option value="lamp">Lampcapaciteit en kanaal</option><option value="transmission">Kastransmissie en scherminvloed</option></select></div><div class="form-grid">${field("valid_from", "Geldig vanaf", "date", "", "required")}${field("valid_until", "Geldig tot (optioneel)", "date")}<div class="field"><label for="object_type">Objecttype</label><select name="object_type" id="object_type"><option value="">Alleen label</option><option value="light">Lichtmodule</option><option value="bay">Kap</option><option value="department">Afdeling</option><option value="valve">Kraan</option></select></div>${field("object_number", "Objectnummer", "number", "", 'min="1"')}${field("label", "Exact Priva-label", "text")}${field("channel", "Kanaal (optioneel)", "text")}</div><div id="cal-fields"></div>${area("reason", "Hoe is dit gecontroleerd?")}<label class="inline field"><input type="checkbox" name="confirmed" required> Ik heb deze instelling en ingangsdatum daadwerkelijk gecontroleerd.</label>${formError()}<button class="primary" type="submit">Validatie bewaren</button></form>`,
  );
  function calFields() {
    const kind = $("#cal-kind").value;
    $("#cal-fields").innerHTML =
      kind === "unit"
        ? field(
            "unit",
            "Bevestigde eenheid",
            "text",
            "",
            'required placeholder="Bijv. J/cm2, s of h"',
          )
        : kind === "lamp"
          ? `<div class="field"><label for="lamp-source">Lampbron</label><select id="lamp-source" name="lamp_source"><option value="priva">Priva-rapport</option><option value="legacy_excel">Aanvullende Excelbelichting</option></select></div><div class="form-grid">${field("ppfd", "Werkelijk PPFD (µmol/m²/s)", "number", "", 'required min="0.1" step="0.1"')}${field("dim_factor", "Dimfactor (0–1)", "number", "", 'required min="0" max="1" step="0.01"')}${field("hours_label", "Exact label brandduur", "text", "", "required")}${field("time_unit", "Eenheid brandduur", "text", "", 'required placeholder="s of h"')}</div><label class="inline field"><input type="checkbox" name="independent_contribution" required> Deze module en dit kanaal zijn een zelfstandige bijdrage en worden niet dubbel geteld.</label>`
          : `${field("factor", "Transmissiefactor (0–1)", "number", "", 'required min="0" max="1" step="0.01"')}<label class="inline field"><input type="checkbox" name="no_screen_effect"> Gecontroleerd dat er in deze periode geen schermverlies was.</label><p class="hint">Zonder deze bevestiging blijft daglicht-DLI geblokkeerd zolang een onderbouwde tijdreeks van schermstanden ontbreekt.</p>`;
    for(const key of ['label','channel'])$('#'+key).readOnly=false;
    if(kind==='lamp') {
      $('#lamp-source').value=initialSource==='legacy_excel'?'legacy_excel':'priva';
      function lampSource(){
        const legacy=$('#lamp-source').value==='legacy_excel';
        $('#object_type').value='light';
        for(const key of ['label','channel','hours_label','time_unit','dim_factor'])$('#'+key).readOnly=legacy;
        $('#label').value=legacy?'legacy_excel_full_power_hours':'';
        $('#channel').value='';$('#hours_label').value=legacy?'legacy_excel_full_power_hours':'';
        $('#time_unit').value=legacy?'h':'';$('#dim_factor').value=legacy?'1':'';
        $('#label').setAttribute('aria-label',legacy?'Vast label voor aanvullende Excelbranduren':'Exact Priva-label');
      }
      $('#lamp-source').onchange=lampSource;lampSource();
    }
  }
  $("#cal-kind").onchange = calFields;
  $("#reason").required = true;
  if(initialSource==='legacy_excel')$('#cal-kind').value='lamp';
  calFields();
  bindForm("#calibration-form", async (d) => {
    const value =
      d.kind === "unit"
        ? { unit: d.unit }
        : d.kind === "lamp"
          ? {
              ppfd: +d.ppfd,
              dim_factor: +d.dim_factor,
              hours_label: d.hours_label,
              time_unit: d.time_unit,
              independent_contribution: d.independent_contribution === "on",
            }
          : {
              factor: +d.factor,
              no_screen_effect: d.no_screen_effect === "on",
            };
    await api("/api/calibrations", {
      method: "POST",
      body: {
        kind: d.kind,
        valid_from: d.valid_from,
        valid_until: d.valid_until || null,
        object_type: d.lamp_source==='legacy_excel'?'light':d.object_type || null,
        object_number: d.object_number ? +d.object_number : null,
        label: d.label || null,
        channel: d.lamp_source==='legacy_excel'?'':d.channel || null,
        value,
        reason: d.reason,
        confirmed: true,
      },
    });
    $("#modal").close();
    toast('Bronvalidatie opgeslagen');
    manage();
  });
}
async function cards() {
  const alive = pageOwner("cards");
  if (!alive()) return;
  const scans = list(await api("/api/scans"));
  if (!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Teeltkaarten & scans",
      "Twee kaarten per A4. Foto’s lokaal uitlezen en altijd zelf controleren.",
    ) +
    `<div class="two"><section class="card"><h2>Kaarten afdrukken</h2><p class="hint">Selecteer de kapteelten. Elke kaart bevat een QR-code en planningversie.</p><div class="check-list" id="card-list">${cultivations.length ? cultivations.map((c) => `<label><input type="checkbox" value="${c.id}" name="card"><span>${esc(cLabel(c))}</span></label>`).join("") : empty("Nog geen kapteelten")}</div><div class="actions" style="margin-top:18px"><button id="print-cards" class="primary">▤ Print geselecteerde kaarten</button></div><p class="hint">Opent een printweergave in een nieuw venster.</p></section><section class="card"><h2>Foto van een teeltkaart</h2><form id="scan-form">${selectCult()}<div class="field"><label for="scan-file">Kaartfoto</label><input id="scan-file" name="file" type="file" accept="image/jpeg,image/png,image/webp" capture="environment" required></div><p class="hint">De foto blijft op de eigen server. Lokale OCR levert hoogstens concepttekst. Cijfers worden pas na jouw controle als gebeurtenissen opgeslagen.</p>${formError()}<button class="primary" type="submit">Foto uploaden & controleren</button></form></section></div><section class="card"><h2>Scangeschiedenis</h2>${scans.length ? scans.map((s) => `<article style="display:flex;justify-content:space-between;gap:12px;padding:14px 0;border-bottom:1px solid var(--line)"><div><strong>${esc(s.filename)}</strong> ${tag(s.status === "confirmed" ? "Bevestigd" : "Controle nodig", s.status === "confirmed" ? "" : "warn")}<p class="hint">${date(s.created_at)} · ${esc(cLabel(cultivations.find((c) => c.id === s.cultivation_id) || { bay_number: "?", round_name: "" }))}</p></div><button class="small" data-scan="${s.id}">Bekijken →</button></article>`).join("") : empty("Nog geen scans", "Je kunt gebeurtenissen ook rechtstreeks bij een kapteelt invoeren.")}</section>`;
  $("#print-cards").onclick = () => {
    const ids = $$("[name=card]:checked").map((x) => x.value);
    if (!ids.length) {
      toast("Selecteer eerst minimaal één kapteelt.");
      return;
    }
    window.open("/api/cards?ids=" + ids.join(","), "_blank", "noopener");
  };
  bindForm("#scan-form", async (d) => {
    const form = new FormData();
    form.append("cultivation_id", d.cultivation_id);
    form.append("file", $("#scan-file").files[0]);
    const s = await api("/api/scans", { method: "POST", body: form });
    await cards();
    reviewScan(s);
  });
  $$("[data-scan]").forEach(
    (b) =>
      (b.onclick = () =>
        reviewScan(scans.find((s) => s.id === +b.dataset.scan))),
  );
}
function reviewScan(s) {
  let events = (s.review?.events || []).map((e) => ({ ...e }));
  const readonly = s.status === "confirmed";
  modal(
    `Kaartscan #${s.id}`,
    `<div class="notice warn">${esc(s.notice || "Controleer de foto en elke waarde. Pas na bevestiging worden gebeurtenissen definitief opgeslagen.")}</div><img class="scan-image" src="${esc(s.image_url)}" alt="Originele foto van de teeltkaart"><details><summary>Lokale OCR-concepttekst · ${esc(s.ocr_status)}</summary><pre>${esc(s.ocr_text || "Geen leesbare concepttekst beschikbaar. Voer de waarden hieronder handmatig over.")}</pre>${s.qr_content ? `<p class="hint">QR: ${esc(s.qr_content)}</p>` : ""}</details><form id="review-form">${area("notes", "Reviewnotities en correcties", s.review?.notes || "")}<div id="scan-events"></div>${!readonly ? '<button type="button" id="add-scan-event">＋ Gecontroleerde regel toevoegen</button>' : ""}${formError()}<div class="actions" style="margin-top:18px">${!readonly ? '<button type="submit" class="primary">Conceptreview opslaan</button><button type="button" id="confirm-scan">Definitief bevestigen</button>' : '<span class="tag">Bevestigd · gebeurtenissen opgeslagen</span>'}</div></form>`,
  );
  function gather() {
    return events.map((_, i) => ({
      occurred_at: $(`[name="event_date_${i}"]`).value,
      kind: $(`[name="event_kind_${i}"]`).value,
      amount: $(`[name="event_amount_${i}"]`).value
        ? +$(`[name="event_amount_${i}"]`).value
        : null,
      unit: $(`[name="event_unit_${i}"]`).value || null,
      scope: $(`[name="event_scope_${i}"]`).value || null,
      reason: $(`[name="event_reason_${i}"]`).value,
    }));
  }
  function draw() {
    const d = $("#scan-events");
    d.innerHTML = events
      .map(
        (e, i) =>
          `<section style="border-top:1px solid var(--line);padding:18px 0"><div class="section-title"><h3>Regel ${i + 1}</h3>${!readonly ? `<button class="small danger" type="button" data-remove-event="${i}">Regel verwijderen</button>` : ""}</div><div class="form-grid">${field(`event_date_${i}`, "Datum / tijd", "datetime-local", e.occurred_at ? toLocalInput(e.occurred_at) : "", "required")}${field(`event_kind_${i}`, "Gebeurtenis / middel", "text", e.kind || "", "required")}${field(`event_amount_${i}`, "Hoeveelheid", "number", e.amount ?? "", 'min="0" step="0.01"')}${field(`event_unit_${i}`, "Eenheid", "text", e.unit || "")}${field(`event_scope_${i}`, "Reikwijdte (selectief / volvelds)", "text", e.scope || "")}</div>${area(`event_reason_${i}`, "Reden / opmerking", e.reason || "")}</section>`,
      )
      .join("");
    events.forEach((_, i) => {
      $(`[name="event_reason_${i}"]`).required = true;
    });
    $$("[data-remove-event]").forEach(
      (b) =>
        (b.onclick = () => {
          events = gather();
          events.splice(+b.dataset.removeEvent, 1);
          draw();
        }),
    );
    if (readonly)
      $$("input,textarea", $("#review-form")).forEach(
        (x) => (x.disabled = true),
      );
  }
  draw();
  applyPermissions();
  if (!readonly) {
    $("#add-scan-event").onclick = () => {
      events = gather();
      events.push({});
      draw();
    };
    async function save() {
      if (!$("#review-form").reportValidity())
        throw new Error("Vul de verplichte velden van elke regel in.");
      events = gather();
      s = await api(`/api/scans/${s.id}`, {
        method: "PATCH",
        body: { notes: $("#notes").value, events },
      });
      return s;
    }
    bindForm("#review-form", async () => {
      await save();
      toast("Reviewconcept opgeslagen; nog niet definitief.");
    });
    $("#confirm-scan").onclick = async () => {
      try {
        await save();
        if (
          !confirm(
            "Foto en alle ingevoerde regels gecontroleerd? Deze gebeurtenissen nu definitief aan de kapteelt toevoegen?",
          )
        )
          return;
        await api(`/api/scans/${s.id}/confirm`, {
          method: "POST",
          body: { confirmed: true },
        });
        $("#modal").close();
        toast("Scan bevestigd en gebeurtenissen opgeslagen");
        cards();
      } catch (e) {
        $(".error-text", $("#review-form")).textContent = e.message;
      }
    };
  }
}
function toLocalInput(value) {
  if (!value) return "";
  if (!/Z$|[+-]\d\d:\d\d$/.test(value)) return value.slice(0, 16);
  return new Date(value)
    .toLocaleString("sv-SE", { timeZone: "Europe/Amsterdam" })
    .replace(" ", "T")
    .slice(0, 16);
}
function seasonRemainder(r) {
  return `<p class="hint">Nog ${esc(r.days)} dagen tot geplande oogst · gemiddelde astronomische daglengte ${fmt(r.mean_astronomical_daylength_hours, " uur")} · op oogstdatum ${fmt(r.end_daylength_hours, " uur")}.</p><div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Weeraannname</th><th>Gewichteffect</th></tr></thead><tbody>${(r.scenarios || []).map((x) => `<tr><td>${{ dark: "Donker", average: "Gemiddeld", light: "Licht" }[x.name] || esc(x.name)}</td><td>${esc(x.weather_assumption)}</td><td>Onbekend</td></tr>`).join("")}</tbody></table></div><p class="hint">Zonder gevalideerd effectmodel wordt geen gewichtsverschil ingevuld.</p>`;
}
function applyPermissions() {
  if (!user) return;
  const disable = (el, reason) => {
    if (!el) return;
    el.disabled = true;
    el.title = reason;
  };
  if (user.role === "lezer") {
    [
      "new-cultivation",
      "edit-plan",
      "event-btn",
      "harvest-btn",
      "cultivation-state",
      "new-variety",
      "new-scenario",
      "add-scan-event",
      "confirm-scan",
      "confirm-import",
      "confirm-history",
      "legacy-confirm", "lighting-resume", "lighting-revoke", "lighting-new-version",
      "revoke-import",
      "resume-import",
    ].forEach((id) =>
      disable($("#" + id), "Je account heeft alleen leesrechten."),
    );
    [
      "measure-form",
      "scan-form",
      "import-form",
      "review-form",
      "history-upload-form",
      "history-mapping-form",
      "history-correction-form", "company-import-form", "legacy-config-form", "legacy-row-form", "lighting-create-form", "lighting-interpretation-form",
    ].forEach((id) => {
      const f = $("#" + id);
      if (f) {
        $$("input,select,textarea,button", f).forEach((el) =>
          disable(el, "Je account heeft alleen leesrechten."),
        );
        if (!$(".permission-note", f))
          f.insertAdjacentHTML(
            "afterbegin",
            '<p class="notice permission-note">Je account heeft alleen leesrechten.</p>',
          );
      }
    });
  }
  if (user.role !== "admin") {
    disable($("#new-mapping"), "Alleen beheerders mogen koppelingen wijzigen.");
    const f = $("#bay-settings");
    if (f)
      $$("input,select,button", f).forEach((el) =>
        disable(el, "Alleen beheerders mogen de oppervlakte wijzigen."),
      );
  }
}
const authChannel =
  "BroadcastChannel" in window
    ? new BroadcastChannel("chrysant-session")
    : null;
if (authChannel)
  authChannel.onmessage = (e) => {
    if (e.data === "logout") {
      user = null;
      cultivations = [];
      varieties = [];
      bays = [];
      sessionStorage.removeItem("chrysant-user");
      sessionStorage.removeItem("chrysant-base");
      clearInterval(poll);
      auth(false, "De sessie is in een ander venster afgesloten.");
    }
  };
window.addEventListener("hashchange", route);
window.addEventListener("online", () => {
  connection();
  syncQueue();
});
window.addEventListener("offline", connection);
(async () => {
  if ("serviceWorker" in navigator)
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const status = await api("/api/auth/status");
    if (status.user) {
      user = status.user;
      sessionStorage.setItem("chrysant-user", JSON.stringify(user));
      await start();
    } else auth(status.setup_required);
  } catch (e) {
    const cached = sessionStorage.getItem("chrysant-user");
    if (!navigator.onLine && cached) {
      user = JSON.parse(cached);
      const base = JSON.parse(
        sessionStorage.getItem("chrysant-base") || "null",
      );
      if (base?.owner === user.id) {
        cultivations = base.cultivations;
        varieties = base.varieties;
        bays = base.bays;
        appSettings = base.settings || appSettings;
      }
      shell();
      currentPage = "measure";
      await measure();
      applyPermissions();
      toast("Offline: alleen lokale concepten en metingen beschikbaar.");
    } else auth(false, "Server niet bereikbaar. Controleer de verbinding.");
  }
})();

function profileLabel(value) {
  return (
    {
      basic: "Basis: teelt en tussenmetingen",
      light: "Licht: basis + geverifieerd lamplicht",
      climate: "Klimaat: licht + temperatuur, RV en water",
    }[value] || esc(value)
  );
}
function modelProfileFields(settings) {
  return `<div class="field"><label for="model_feature_profile">Welke invoer mag het model gebruiken?</label><select id="model_feature_profile" name="model_feature_profile">${["basic", "light", "climate"].map((value) => `<option value="${value}" ${(settings.model_feature_profile || "basic") === value ? "selected" : ""}>${profileLabel(value)}</option>`).join("")}</select><p class="hint">Licht en klimaat vragen complete, bevestigde bronnen en het hogere ingestelde minimumaantal teelten. Ontbrekende waarden worden niet als nul ingevuld.</p></div><label class="inline field"><input type="checkbox" name="environment_include_daylight_estimate" ${settings.environment_include_daylight_estimate ? "checked" : ""}> Een als benadering gemarkeerde daglichtsom toestaan als extra modelinvoer.</label><p class="hint">Dit maakt een schatting niet tot een geverifieerde meting. CO₂ wordt alleen gebruikt als de bron betrouwbaar is.</p><label class="inline field"><input type="checkbox" name="environment_include_co2" ${settings.environment_include_co2 ? "checked" : ""}> Betrouwbare CO₂ als extra klimaatkenmerk gebruiken (vereist volledige dekking).</label>`;
}
function planningExtras(plan) {
  return `${field("planned_stems", "Gepland aantal takken (optioneel)", "number", plan.planned_stems ?? "", 'min="0" step="1"')}${field("planned_lamp_hours", "Geplande branduren per dag (optioneel)", "number", plan.planned_lamp_hours ?? "", 'min="0" max="24" step="0.1"')}<div class="full">${area("interlight_description", "Tussenlicht (optioneel)", plan.interlight_description || "")}${area("lighting_schedule", "Belichtingsschema / strategie (optioneel)", plan.lighting_schedule || "")}</div>`;
}
function planningExtraPayload(data) {
  return {
    planned_stems: data.planned_stems ? +data.planned_stems : null,
    planned_lamp_hours: data.planned_lamp_hours
      ? +data.planned_lamp_hours
      : null,
    interlight_description: data.interlight_description || null,
    lighting_schedule: data.lighting_schedule || null,
  };
}
const monthNames = [
  "januari",
  "februari",
  "maart",
  "april",
  "mei",
  "juni",
  "juli",
  "augustus",
  "september",
  "oktober",
  "november",
  "december",
];
function seasonFields(key, label, value = "") {
  const [month, day] = value.split("-");
  return `<fieldset class="season-fields"><legend>${label}</legend><div class="form-grid"><div class="field"><label for="${key}_month">Maand</label><select name="${key}_month" id="${key}_month" required><option value="">Kies maand</option>${monthNames.map((name, i) => `<option value="${String(i + 1).padStart(2, "0")}" ${Number(month) === i + 1 ? "selected" : ""}>${name}</option>`).join("")}</select></div>${field(`${key}_day`, "Dag van de maand", "number", day ? +day : "", 'required min="1" max="31" step="1"')}</div></fieldset>`;
}
function seasonDate(value) {
  if (!value) return "—";
  const [m, d] = value.split("-");
  return `${+d} ${monthNames[+m - 1] || m}`;
}
async function cropProfilesPanel() {
  const alive = pageOwner("manage"),
    panel = $("#crop-profiles");
  if (!alive() || !panel) return;
  try {
    const data = await api("/api/crop-profiles");
    if (!alive() || !panel.isConnected) return;
    const profiles = Array.isArray(data) ? data : data.profiles || [];
    panel.innerHTML = `<div class="section-title"><h2>Soort- en seizoensprofielen</h2><button id="new-crop-profile">＋ Lengtecurve vastleggen</button></div><p class="hint">Per soort en plantperiode: streeflengten op expliciete KD-dagen, doelgewicht en tussenlicht. Een winterperiode mag over de jaargrens lopen. Eerdere versies blijven behouden.</p>${profiles.length ? profiles.map((profile) => `<details><summary>${esc(varieties.find((v) => v.id === profile.variety_id)?.name || "Soort")} · planten ${seasonDate(profile.season_start)} t/m ${seasonDate(profile.season_end)} · vanaf ${date(profile.valid_from)}</summary><p class="hint">${profile.valid_until ? "Geldig t/m " + date(profile.valid_until) : "Geen einddatum"} · ${esc(profile.source)}<br>Doelgewicht: ${fmt(profile.target_weight_g, " g")} · ${esc(profile.interlight_description || "Geen tussenlicht vastgelegd")}</p><div class="table-wrap"><table><thead><tr><th>KD-dag</th><th>Streeflengte</th></tr></thead><tbody>${(profile.length_curve || []).map((point) => `<tr><td>${esc(point.kd_day)}</td><td>${fmt(point.length_cm, " cm")}</td></tr>`).join("")}</tbody></table></div><button class="small" data-profile-version="${profile.id}">Nieuwe profielversie</button></details>`).join("") : empty("Nog geen seizoensprofielen", "Zonder vastgelegde curve blijft een lengteafwijking onbekend.")}`;
    $("#new-crop-profile").onclick = () => cropProfileForm();
    $$("[data-profile-version]", panel).forEach(
      (button) =>
        (button.onclick = () =>
          cropProfileForm(
            profiles.find((p) => p.id === +button.dataset.profileVersion),
          )),
    );
  } catch (error) {
    if (alive() && panel.isConnected)
      panel.innerHTML = `<h2>Soort- en seizoensprofielen</h2><p class="notice error">${esc(error.message)}</p>`;
  }
}
async function koppelsleutelsPanel(nieuw = null) {
  const alive = pageOwner("manage"),
    panel = $("#koppelsleutels");
  if (!alive() || !panel) return;
  try {
    const keys = list(await api("/api/koppelsleutels"));
    if (!alive() || !panel.isConnected) return;
    panel.innerHTML = `<h2>Bedrijfskoppeling (alleen-lezen API)</h2><p class="hint">Sleutels voor het Bedrijfsdashboard en andere bedrijfsapps. Zij geven alleen leestoegang tot samenvatting en datasets via <code>/api/koppeling/v1</code>; gewichten verschijnen daar alleen uit een vrijgegeven model. De volledige sleutel wordt één keer getoond.</p>${nieuw ? `<div class="notice warn" role="status"><strong>Kopieer deze sleutel nu.</strong> Hij wordt niet opnieuw getoond.<div class="field" style="margin-top:10px"><label for="koppelsleutel-waarde">Sleutel voor ${esc(nieuw.naam)}</label><input id="koppelsleutel-waarde" readonly value="${esc(nieuw.sleutel)}"></div></div>` : ""}<form id="koppelsleutel-form"><div class="form-grid">${field("koppel_naam", "Naam (bijv. Bedrijfsdashboard)", "text", "", 'required maxlength="100"')}</div>${formError()}<button type="submit">＋ Sleutel aanmaken</button></form>${keys.length ? `<div class="table-wrap"><table><thead><tr><th>Naam</th><th>Begint met</th><th>Aangemaakt</th><th>Laatst gebruikt</th><th>Status</th><th></th></tr></thead><tbody>${keys.map((k) => `<tr><td>${esc(k.naam)}</td><td><code>${esc(k.voorvoegsel)}…</code></td><td>${dateTime(k.aangemaakt_op)}${k.aangemaakt_door ? " · " + esc(k.aangemaakt_door) : ""}</td><td>${dateTime(k.laatst_gebruikt_op)}</td><td>${k.actief ? tag("Actief") : tag("Ingetrokken " + date(k.ingetrokken_op), "gray")}</td><td>${k.actief ? `<button class="small danger" data-koppelsleutel="${k.id}">Intrekken</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : empty("Nog geen koppelsleutels", "Maak een sleutel aan zodra het Bedrijfsdashboard gegevens moet ophalen.")}`;
    const waarde = $("#koppelsleutel-waarde", panel);
    if (waarde) waarde.onfocus = () => waarde.select();
    bindForm("#koppelsleutel-form", async (d) => {
      const created = await api("/api/koppelsleutels", {
        method: "POST",
        body: { naam: d.koppel_naam },
      });
      toast("Koppelsleutel aangemaakt");
      await koppelsleutelsPanel(created);
    });
    $$("[data-koppelsleutel]", panel).forEach(
      (button) =>
        (button.onclick = async () => {
          if (
            !confirm(
              "Deze koppelsleutel intrekken? Apps die hem gebruiken verliezen direct toegang.",
            )
          )
            return;
          try {
            await api(`/api/koppelsleutels/${button.dataset.koppelsleutel}`, {
              method: "DELETE",
            });
            toast("Koppelsleutel ingetrokken");
            await koppelsleutelsPanel();
          } catch (error) {
            toast(error.message);
          }
        }),
    );
  } catch (error) {
    if (alive() && panel.isConnected)
      panel.innerHTML = `<h2>Bedrijfskoppeling</h2><p class="notice error">${esc(error.message)}</p>`;
  }
}
function cropProfileForm(profile) {
  let counter = 0;
  const dialog = modal(
    profile ? "Nieuwe profielversie" : "Lengtecurve en seizoensprofiel",
    `<form id="crop-profile-form"><div class="field"><label for="profile_variety_id">Soort</label><select id="profile_variety_id" name="variety_id" required>${options(varieties, "id", "name", profile?.variety_id)}</select></div><div class="form-grid">${seasonFields("season_start", "Plantperiode vanaf", profile?.season_start)}${seasonFields("season_end", "Plantperiode tot en met", profile?.season_end)}${field("profile_valid_from", "Profiel geldig vanaf", "date", "", "required")}${field("profile_valid_until", "Profiel geldig tot (optioneel)", "date")}${field("profile_target_weight", "Seizoensdoelgewicht (g, optioneel)", "number", profile?.target_weight_g ?? "", 'min="0.1" max="1000" step="0.1"')}</div>${area("profile_interlight", "Tussenlicht", profile?.interlight_description || "")}<h3>Lengtecurve</h3><p class="hint">Vul de streeflengte per KD-dag in. Tussen de punten wordt geïnterpoleerd; buiten de curve blijft het doel onbekend. Er worden geen gewichten of metingen naar een andere KD-dag omgerekend.</p><div id="curve-points"></div><div class="actions"><button type="button" class="small" id="add-curve-point">＋ KD-punt</button><button type="button" class="small" id="clear-curve-points">Alleen doelgewicht / tussenlicht</button></div>${area("profile_source", "Bron / onderbouwing van dit profiel")}${formError()}<button type="submit" class="primary">Profielversie opslaan</button></form>`,
  );
  function pointRow(point = {}) {
    const i = counter++;
    $("#curve-points", dialog).insertAdjacentHTML(
      "beforeend",
      `<div class="curve-point" data-point="${i}">${field(`curve_kd_${i}`, "KD-dag", "number", point.kd_day ?? "", 'required min="0" max="500" step="1"')}${field(`curve_length_${i}`, "Streeflengte (cm)", "number", point.length_cm ?? "", 'required min="0.1" max="500" step="0.1"')}<button type="button" class="small danger" aria-label="KD-punt verwijderen" data-remove-point="${i}">×</button></div>`,
    );
    $$("[data-remove-point]", dialog).forEach(
      (button) =>
        (button.onclick = () => button.closest(".curve-point").remove()),
    );
  }
  (profile?.length_curve?.length ? profile.length_curve : [{}, {}]).forEach(
    pointRow,
  );
  $("#add-curve-point", dialog).onclick = () => pointRow();
  $("#clear-curve-points",dialog).onclick=()=>$("#curve-points",dialog).replaceChildren();
  $("#profile_source", dialog).required = true;
  bindForm("#crop-profile-form", async (data) => {
    const curve = $$("[data-point]", dialog).map((row) => ({
      kd_day: +data[`curve_kd_${row.dataset.point}`],
      length_cm: +data[`curve_length_${row.dataset.point}`],
    }));
    if (!curve.length && !data.profile_target_weight && !data.profile_interlight?.trim()) throw new Error("Vul een lengtecurve, seizoensdoelgewicht of tussenlicht in.");
    if (new Set(curve.map((p) => p.kd_day)).size !== curve.length)
      throw new Error("Elke KD-dag mag één keer in de curve staan.");
    const season = (key) =>
      `${data[key + "_month"]}-${String(data[key + "_day"]).padStart(2, "0")}`;
    await api("/api/crop-profiles", {
      method: "POST",
      body: {
        variety_id: +data.variety_id,
        season_start: season("season_start"),
        season_end: season("season_end"),
        valid_from: data.profile_valid_from,
        valid_until: data.profile_valid_until || null,
        target_weight_g: data.profile_target_weight
          ? +data.profile_target_weight
          : null,
        length_curve: curve.sort((a, b) => a.kd_day - b.kd_day),
        interlight_description: data.profile_interlight || null,
        source: data.profile_source,
        supersedes_id: profile?.id || null,
      },
    });
    dialog.close();
    toast("Seizoensprofiel als nieuwe versie opgeslagen");
    await cropProfilesPanel();
  });
}
async function growthPanel(id) {
  const alive = pageOwner("cultivation"),
    panel = $("#growth-target");
  if (!alive() || !panel) return;
  try {
    const data = await api(`/api/cultivations/${id}/growth-target`);
    if (!alive() || !panel.isConnected) return;
    const reasons = {
      plant_date_unknown:
        "De plantdatum ontbreekt; er kan geen seizoensprofiel worden gekozen.",
      no_profile:
        "Er is nog geen geldig profiel voor deze soort en plantperiode.",
      ambiguous:
        "Meer dan één profiel past. Controleer de geldigheidsdatums en seizoensvensters in Beheer.",
    };
    panel.innerHTML = `<h2>Groei & streeflengte</h2>${data.reason ? `<p class="notice warn">${esc(reasons[data.reason] || data.reason)}</p>` : ""}<div class="summary"><div><small>Nu KD-dag</small><strong>${fmt(data.actual_kd_day)}</strong></div><div><small>Streeflengte op deze dag</small><strong>${fmt(data.target_length_cm, " cm")}</strong></div></div><p class="hint">KD10-doel ${fmt(data.target_kd10_cm, " cm")} · KD30-doel ${fmt(data.target_kd30_cm, " cm")}${data.profile ? " · profielversie " + esc(data.profile.id) : ""}</p>${data.comparisons?.length ? `<div class="table-wrap"><table><thead><tr><th>Meting</th><th>Werkelijke KD-dag</th><th>Gemeten lengte</th><th>Streeflengte</th><th>Afwijking</th></tr></thead><tbody>${data.comparisons.map((row) => `<tr><td>${esc(row.kind)}</td><td>${fmt(row.actual_kd_day)}</td><td>${fmt(row.mean_length_cm, " cm")}</td><td>${fmt(row.target_length_cm, " cm")}</td><td>${fmt(row.difference_cm, " cm")}</td></tr>`).join("")}</tbody></table></div>` : empty("Nog geen lengtevergelijkingen", "Registreer eerst een tussenmeting.")}<p class="hint">${esc(data.notice || "De gemeten lengte blijft op de werkelijke meetdag staan. Er wordt alleen met een expliciet vastgelegd doel vergeleken.")}</p>`;
  } catch (error) {
    if (alive() && panel.isConnected)
      panel.innerHTML = `<h2>Groei & streeflengte</h2><p class="notice error">${esc(error.message)}</p>`;
  }
}
const ENV_FEATURES = {
  env_lamp_light_mol_m2: ["Lamplichtsom", "mol/m²"],
  env_daylight_estimate_mol_m2: ["Daglichtsom (benadering)", "mol/m²"],
  env_total_light_mol_m2: ["Totale lichtsom", "mol/m²"],
  env_temperature_c: ["Gemiddelde temperatuur", "°C"],
  env_rh_pct: ["Gemiddelde RV", "%"],
  env_water_l_m2: ["Watergift", "L/m²"],
  env_co2_ppm: ["Betrouwbare CO₂", "ppm"],
};
const PHASE_LABELS = {
  LD: "LD: planten → licht uit",
  KD_toM1: "KD: licht uit → meting 1",
  M1_toM2: "Meting 1 → meting 2",
  M2_toasof: "Meting 2 → peildatum",
};
function featureTable(features = {}) {
  const keys = [
    ...new Set([...Object.keys(ENV_FEATURES), ...Object.keys(features)]),
  ];
  return `<div class="table-wrap"><table><thead><tr><th>Kenmerk</th><th>Waarde</th></tr></thead><tbody>${keys
    .map((key) => {
      const [label, unit] = ENV_FEATURES[key] || [
        key.replace(/^env_/, "").replaceAll("_", " "),
        "",
      ];
      return `<tr><td>${esc(label)}</td><td>${features[key] === null || features[key] === undefined ? "Onbekend" : fmt(features[key], unit ? " " + unit : "")}</td></tr>`;
    })
    .join("")}</tbody></table></div>`;
}
function coverageTable(coverage = {}) {
  const labels = {
    lamp_light: "Lamplicht",
    temperature: "Temperatuur",
    rh: "RV",
    water: "Watergift",
    co2: "CO₂",
    daylight: "Daglicht",
    daylight_estimate: "Daglicht (benadering)",
  };
  return `<div class="table-wrap"><table><thead><tr><th>Bron</th><th>Geldige dagen</th><th>Dekking</th><th>Waarde op gedekte dagen</th></tr></thead><tbody>${Object.entries(
    coverage,
  )
    .map(
      ([key, row]) =>
        `<tr><td>${esc(labels[key] || key)}</td><td>${fmt(row.valid_days)} / ${fmt(row.expected_days)}</td><td>${row.fraction == null ? "Onbekend" : fmt(row.fraction * 100, "%")}</td><td>${row.observed_value == null ? "Onbekend" : fmt(row.observed_value, row.unit ? " " + row.unit : "")}</td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}
async function environmentPanel(id, asOf = "") {
  const alive = pageOwner("cultivation"),
    panel = $("#environment-panel");
  if (!alive() || !panel) return;
  try {
    const data = await api(
      `/api/cultivations/${id}/environment${asOf ? "?as_of=" + encodeURIComponent(asOf) : ""}`,
    );
    if (!alive() || !panel.isConnected) return;
    panel.innerHTML = `<div class="section-title"><h2>Licht, klimaat & water per fase</h2>${tag(data.profile === "basic" ? "Basisprofiel" : data.eligible ? "Dekking voldoet" : "Onvoldoende dekking", data.eligible ? "" : "warn")}</div><p class="hint">Invoerprofiel: ${profileLabel(data.profile || "basic")} · ${date(data.window?.start)} – ${date(data.window?.end)}. Alleen afgeronde lokale dagen; dagtotalen na de peildatum worden niet gebruikt.</p><p class="notice">Dit zijn de actuele waarnemingen tot de peildatum. De invoer voor een KD-model is apart op het bijbehorende meetmoment afgekapt.</p>${(data.missing || []).map((reason) => `<p class="notice warn">${esc(environmentReason(reason))}</p>`).join("")}${featureTable(data.features)}<details><summary>Dekking over de hele periode</summary>${coverageTable(data.coverage)}</details>${Object.entries(
      data.phases || {},
    )
      .map(
        ([phase, row]) =>
          `<details><summary>${esc(PHASE_LABELS[phase] || phase)} · ${date(row.window?.start)} – ${date(row.window?.end)}</summary>${featureTable(row.features)}${coverageTable(row.coverage)}</details>`,
      )
      .join(
        "",
      )}<p class="hint">CO₂=0 uit een onbetrouwbare bron telt niet als nulmeting. Onbekende eenheden of een onzekere koppeling leveren geen geverifieerd kenmerk op.</p><details><summary>Herkomst en kwaliteitscontrole</summary>${qualityView(data.quality)}${(data.warnings || []).map((reason) => `<p class="hint">${esc(environmentReason(reason))}</p>`).join("")}<div class="table-wrap"><table><tbody>${Object.entries(
      data.source_ids || {},
    )
      .map(
        ([key, values]) =>
          `<tr><th>${esc({ priva_source_ids: "Priva-bronversies", priva_value_ids: "Meetwaarden", mapping_revision_ids: "Koppelingsversies", calibration_ids: "Bronvalidaties", settings_id: "Instellingversie", legacy_lighting: "Aanvullende Excelbelichting" }[key] || key)}</th><td>${esc(
            sourceReferenceText(values),
          )}</td></tr>`,
      )
      .join("")}</tbody></table></div></details>`;
  } catch (error) {
    if (alive() && panel.isConnected)
      panel.innerHTML = `<h2>Licht, klimaat & water per fase</h2><p class="notice error">${esc(error.message)}</p>`;
  }
}

const HISTORY_FIELDS = [
  ["round_name", "Ronde"],
  ["bay_number", "Kapnummer"],
  ["variety", "Soort / soortbedden"],
  ["plant_date", "Plantdatum"],
  ["light_out_date", "Licht uit"],
  ["harvest_start_date", "Begin oogst"],
  ["harvest_end_date", "Leegdatum"],
  ["weight_g", "Eindgewicht per tak (g)"],
  ["density", "Plantdichtheid"],
  ["beds", "Aantal bedden"],
  ["stem_count", "Geoogste takken"],
  ["loss_fraction", "Uitval (fractie 0–1)"],
  ["planned_stems", "Gepland aantal takken"],
  ["historical_alar_total", "Historisch alartotaal"],
  ["historical_alar_unit", "Eenheid historisch alartotaal"],
];
function importTabs(active) {
  return `<nav class="tabs" aria-label="Importtype"><a href="#imports" class="${active === "priva" ? "active" : ""}">Priva · CSV / ZIP</a><a href="#imports/history" class="${active === "history" ? "active" : ""}">Historische teelten · Excel</a><a href="#imports/lighting" class="${active==='lighting'?'active':''}">Aanvullende Excelbelichting</a></nav>`;
}
async function historicalImports() {
  const alive = pageOwner("imports");
  const data = await api("/api/history-imports");
  if (!alive()) return;
  const rows = Array.isArray(data) ? data : data.imports || [];
  $("#main").innerHTML =
    pageHead(
      "Historische teelten uit Excel",
      "Bedrijfsbestanden herkennen of zelf kolommen koppelen. Altijd eerst controleren en bevestigen.",
    ) +
    importTabs("history") +
    `<section class="card"><form id="history-upload-form"><div class="notice"><strong>Priva gaat altijd voor.</strong> Historische Excelplanning en gewichten blijven aparte bronnen. Excelbelichting kan ontbrekende tijdstippen aanvullen en vervangt geen aanwezige Priva-bron.</div><div class="upload"><h2>Bestaande teeltregistratie toevoegen</h2><p class="hint">Kies één of beide .xlsx-werkboeken. Bedrijfsprofielen worden aan de inhoud herkend; een bestandsnaam is geen bewijs voor een jaartal.</p><label for="history-file">Excel-werkboeken</label><input id="history-file" name="file" type="file" accept=".xlsx" multiple required></div>${formError()}<button type="submit" class="primary">Bestanden uploaden & bekijken</button><p id="history-upload-progress" class="hint" role="status"></p></form></section><section id="company-imports" class="card" hidden></section><section class="card"><h2>Historische importgeschiedenis</h2>${rows.length ? rows.map((job) => `<article style="padding:15px 0;border-bottom:1px solid var(--line)"><div class="section-title"><div><h3>${esc(job.filename)} ${job.profile&&job.profile!=='generic'?tag(companyProfileLabel(job.profile),'gray'):''} ${tag(historyStatus(job.status), job.status === "confirmed" ? "" : "warn")}</h3><p class="hint">Import #${esc(job.id)} · ${date(job.created_at)}</p></div><button class="small" data-history="${job.id}">Controleren →</button></div></article>`).join("") : empty("Nog geen historische werkboeken", "De bron blijft bewaard; een upload alleen maakt geen kapteelten aan.")}</section>`;
  bindForm("#history-upload-form", async () => {
    const files=[...$("#history-file").files], uploaded=[];
    const progress=$("#history-upload-progress");
    for(let index=0;index<files.length;index++){
      const form=new FormData();form.append('file',files[index]);
      if(progress.isConnected)progress.textContent=`Bestand ${index+1} van ${files.length} ontvangen en inhoud verkennen: ${files[index].name}`;
      uploaded.push(await api('/api/history-imports',{method:'POST',body:form}));
    }
    if(!alive())return;
    await historicalImports();
    if(alive()&&uploaded.length===1&&(!uploaded[0].profile||uploaded[0].profile==='generic'))await historyReview(uploaded[0].id);
    else if(alive())toast(`${uploaded.length} werkboeken ontvangen. Kies hieronder de gecontroleerde importmethode.`);
  });
  $$("[data-history]").forEach(
    (button) => (button.onclick = () => {const job=rows.find(job=>job.id===+button.dataset.history);return job?.profile==='legacy_weight_workbook'&&job.mapping?.program_import_id?legacyCompanyReview(job.id,job.mapping.program_import_id):historyReview(button.dataset.history)}),
  );
  companyImportsPanel(rows);
  applyPermissions();
}
function historyStatus(status) {
  return (
    {
      uploaded: "Kolommen kiezen",
      preview: "Controlevoorbeeld",
      review: "Controle nodig",
      mapped: "Controlevoorbeeld",
      preview_ready: "Controlevoorbeeld",
      confirmed: "Bevestigd",
      failed: "Mislukt",
    }[status] || status
  );
}
function sourceValue(value) {
  return value === null || value === undefined
    ? ""
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}
async function historyReview(id) {
  const alive = pageOwner("imports");
  let job = await api(`/api/history-imports/${id}`);
  if (!alive()) return;
  const dialog = modal(
    `Historisch werkboek #${id}`,
    '<div id="history-detail"></div>',
  );
  const container = $("#history-detail", dialog);
  const active = () => alive() && container.isConnected && dialog.open;
  let mapping = job.mapping ||
    job.mapping_config || {
      sheet: job.sheets?.[0]?.name || "",
      header_row: 1,
      column_map: {},
      defaults: {},
      overrides: [],
      date_year_policy: "explicit_only",
    };
  let overrides = (mapping.overrides || []).map((row) => ({ ...row }));
  function draw() {
    if (!active()) return;
    const finalized = job.status === "confirmed";
    const sheet =
      job.sheets?.find((sheet) => sheet.name === mapping.sheet) ||
      job.sheets?.[0];
    if (sheet) mapping.sheet = sheet.name;
    container.innerHTML = `<p class="notice">Alleen de kolommen die je zelf koppelt worden gebruikt. Onzekere jaartallen, soorten en gewichten blijven ter beoordeling staan. Pas na bevestiging ontstaan teeltregistraties.</p><details><summary>Oorspronkelijk bestand en werkbladen</summary><p class="hint">${esc(job.filename)} · SHA-256 ${esc(job.sha256)}</p>${(job.sheets || []).map((item) => `<p class="hint">${esc(item.name)} · ${fmt(item.max_row)} rijen · ${fmt(item.max_column)} kolommen</p>`).join("")}</details><form id="history-mapping-form"><div class="form-grid"><div class="field"><label for="history-sheet">Werkblad</label><select id="history-sheet" name="sheet">${(job.sheets || []).map((item) => `<option value="${esc(item.name)}" ${item.name === mapping.sheet ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></div>${field("history-header-row", "Rij met kolomkoppen", "number", mapping.header_row || 1, 'required min="1" max="200" step="1"')}</div><div id="history-source-preview"></div><h3>Welke kolom bevat welk teeltgegeven?</h3><div class="form-grid" id="history-column-fields"></div>${field("history-default-beds", "Vast aantal bedden als de bronkolom ontbreekt (optioneel)", "number", mapping.defaults?.beds ?? "", 'min="1" max="6" step="1"')}<p class="hint">Een lege koppeling blijft onbekend. Jaartallen moeten expliciet in een datum of handmatige correctie staan.</p>${formError()}<button type="submit" class="primary">Kolomkoppeling controleren</button></form><section id="history-row-preview" class="section"></section><details><summary>Beoordelingshistorie (${job.reviews?.length || 0})</summary>${(job.reviews || []).map((review) => `<article><h3>${dateTime(review.created_at)}</h3><p class="hint">Werkblad ${esc(review.mapping?.sheet)} · koprij ${esc(review.mapping?.header_row)}<br>${(review.mapping?.overrides || []).map((row) => "Bronrij " + esc(row.row_number) + ": " + esc(row.reason)).join("<br>") || "Geen rijcorrecties in deze versie."}</p></article>`).join("")}</details>`;
    function drawSheet() {
      const selected = job.sheets.find(
        (item) => item.name === $("#history-sheet", dialog).value,
      );
      if (!selected) return;
      const sourceHeader = (selected.sample_rows || []).find(
        (row) => row.row_number === +$("#history-header-row", dialog).value,
      );
      const knownHeaders=new Map((selected.headers||[]).map(column=>[column.column,column.value]));
      const cols=Array.from({length:selected.max_column||knownHeaders.size},(_,index)=>{
        const column=excelColumn(index+1);
        return {column,value:knownHeaders.has(column)?knownHeaders.get(column):sourceHeader?.cells?.[column]??null};
      });
      const previewCols=cols.slice(0,100);
      $("#history-source-preview", dialog).innerHTML =
        `<details open><summary>Voorbeeld uit ${esc(selected.name)}</summary><div class="table-wrap history-preview"><table><thead><tr><th>Bronrij</th>${previewCols.map((col) => `<th>${esc(col.column)} · ${esc(sourceValue(col.value))}</th>`).join("")}</tr></thead><tbody>${(selected.sample_rows || []).map((row) => `<tr><td>${esc(row.row_number)}</td>${previewCols.map((col) => `<td>${esc(sourceValue(row.cells?.[col.column]))}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
      $("#history-column-fields", dialog).innerHTML = HISTORY_FIELDS.map(
        ([key, label]) =>
          `<div class="field"><label for="map-${key}">${label}</label><select id="map-${key}" name="map_${key}"><option value="">Niet in dit werkblad</option>${cols.map((col) => `<option value="${esc(col.column)}" ${mapping.column_map?.[key] === col.column ? "selected" : ""}>${esc(col.column)} · ${esc(sourceValue(col.value) || "(geen kop in voorbeeld)")}</option>`).join("")}</select></div>`,
      ).join("");
    }
    drawSheet();
    let headerRequest = 0;
    async function loadHeaders() {
      const request = ++headerRequest,
        form = $("#history-mapping-form", dialog);
      mapping.column_map = Object.fromEntries(
        HISTORY_FIELDS.filter(([key]) => $("#map-" + key, dialog)?.value).map(
          ([key]) => [key, $("#map-" + key, dialog).value],
        ),
      );
      const sheet = $("#history-sheet", dialog).value,
        headerRow = +$("#history-header-row", dialog).value;
      if (!headerRow || headerRow < 1) return;
      $$("#history-column-fields select,button[type=submit]",form).forEach(control=>control.disabled=true);
      try {
        const preview = await api(
          `/api/history-imports/${id}/sheet-preview?sheet=${encodeURIComponent(sheet)}&header_row=${headerRow}`,
        );
        if (!active() || !form.isConnected || request !== headerRequest) return;
        const selected = job.sheets.find((item) => item.name === sheet);
        Object.assign(selected, preview);
        drawSheet();
      } catch (error) {
        if (active() && form.isConnected)
          toast("Kolomkoppen niet geladen: " + error.message);
      } finally {
        if(active()&&form.isConnected&&request===headerRequest){
          $$("#history-column-fields select,button[type=submit]",form).forEach(control=>control.disabled=false);
          applyPermissions();
        }
      }
    }
    $("#history-sheet", dialog).onchange = () => {
      drawSheet();
      loadHeaders();
    };
    $("#history-header-row", dialog).onchange = loadHeaders;
    bindForm("#history-mapping-form", async (data) => {
      mapping = {
        sheet: data.sheet,
        header_row: +data["history-header-row"],
        column_map: Object.fromEntries(
          HISTORY_FIELDS.filter(([key]) => data["map_" + key]).map(([key]) => [
            key,
            data["map_" + key],
          ]),
        ),
        defaults: data["history-default-beds"]
          ? { beds: +data["history-default-beds"] }
          : {},
        overrides,
        date_year_policy: "explicit_only",
      };
      job = await api(`/api/history-imports/${id}/mapping`, {
        method: "POST",
        body: mapping,
      });
      if (active()) draw();
    });
    drawRows();
    if (finalized)
      $$(
        "input,select,textarea,button",
        $("#history-mapping-form", dialog),
      ).forEach((el) => (el.disabled = true));
    applyPermissions();
  }
  function drawRows() {
    if (!active()) return;
    const rows = job.preview?.rows || [],
      summary = job.preview?.summary || job.summary || {},
      finalized = job.status === "confirmed";
    const target = $("#history-row-preview", dialog);
    target.innerHTML = `<h3>Controlevoorbeeld</h3>${
      Object.keys(summary).length
        ? `<div class="notice">${Object.entries(summary)
            .map(
              ([key, value]) =>
                `${esc({ total: "Totaal", total_rows: "Totaal rijen", valid: "Geldig", valid_rows: "Geldige rijen", ambiguous: "Te beoordelen", ambiguous_rows: "Te beoordelen", excluded: "Uitgesloten", excluded_rows: "Uitgesloten", created: "Aangemaakt", duplicate: "Al geïmporteerd" }[key] || key)}: <strong>${esc(sourceValue(value))}</strong>`,
            )
            .join(" · ")}</div>`
        : ""
    }${rows.length ? `<div class="table-wrap history-preview"><table><thead><tr><th>Bronrij</th><th>Status</th><th>Ronde / kap / soort</th><th>Gewicht</th><th>Aandacht</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(row.row_number)}</td><td>${tag({ valid: "Geldig", ambiguous: "Beoordelen", invalid: "Ongeldig", excluded: "Uitgesloten", duplicate: "Al geïmporteerd", imported: "Geïmporteerd" }[row.status] || row.status, row.status === "valid" ? "" : "warn")}</td><td>${esc(sourceValue(row.effective?.round_name))}<br>Kap ${esc(sourceValue(row.effective?.bay_number))} · ${esc(sourceValue(row.effective?.variety))}</td><td>${esc(sourceValue(row.effective?.weight_g)) || "—"}</td><td>${esc((row.reasons || []).join(" · "))}</td></tr>`).join("")}</tbody></table></div>` : empty("Nog geen gekoppeld voorbeeld", "Kies de bronkolommen en laat de app de rijen controleren.")}${
      rows.length && !finalized
        ? `<form id="history-correction-form" class="history-row-editor"><h3>Een bronrij beoordelen of corrigeren</h3><div class="field"><label for="history-correct-row">Bronrij</label><select id="history-correct-row">${rows
            .slice()
            .sort(
              (a, b) =>
                (a.status === "valid") - (b.status === "valid") ||
                a.row_number - b.row_number,
            )
            .map(
              (row) =>
                `<option value="${row.row_number}">Rij ${row.row_number} · ${row.status === "valid" ? "geldig" : "controle"} · ${esc((row.reasons || []).join(", "))}</option>`,
            )
            .join(
              "",
            )}</select></div><div id="history-correction-fields"></div>${formError()}<button type="submit">Correctie toepassen & opnieuw controleren</button></form>`
        : ""
    }${rows.length ? `<div class="actions" style="margin-top:18px">${!finalized ? `<button class="primary" id="confirm-history" ${summary.ambiguous > 0 || rows.some((row) => !["valid", "excluded", "duplicate"].includes(row.status)) ? 'disabled title="Corrigeer of sluit alle onduidelijke rijen uit met een reden"' : ""}>Gecontroleerde teelten definitief importeren</button>` : '<span class="tag">Bevestigd · teelten geregistreerd</span>'}</div>` : ""}`;
    if (job.preview?.total > (job.preview?.limit || 200)) {
      const offset = job.preview.offset || 0,
        limit = job.preview.limit || 200,
        total = job.preview.total;
      target.insertAdjacentHTML(
        "afterbegin",
        `<div class="actions" style="margin-bottom:12px"><button type="button" id="history-previous" class="small" ${offset === 0 ? "disabled" : ""}>← Vorige rijen</button><span class="hint" style="align-self:center">${offset + 1}–${Math.min(offset + rows.length, total)} van ${total}</span><button type="button" id="history-next" class="small" ${offset + limit >= total ? "disabled" : ""}>Volgende rijen →</button></div>`,
      );
      async function showPage(nextOffset) {
        try {
          const preview = await api(
            `/api/history-imports/${id}/rows?offset=${nextOffset}&limit=${limit}`,
          );
          if (active()) {
            job.preview = preview;
            drawRows();
            applyPermissions();
          }
        } catch (error) {
          toast(error.message);
        }
      }
      $("#history-previous", dialog).onclick = () =>
        showPage(Math.max(0, offset - limit));
      $("#history-next", dialog).onclick = () => showPage(offset + limit);
    }
    if (!rows.length || finalized) return;
    function drawCorrection() {
      const row = rows.find(
          (row) => row.row_number === +$("#history-correct-row", dialog).value,
        ),
        existing = overrides.find(
          (override) => override.row_number === row.row_number,
        );
      const values = { ...row.effective, ...existing?.fields };
      $("#history-correction-fields", dialog).innerHTML =
        `<p class="hint">Laat bestaande waarden staan als ze juist zijn. De oorspronkelijke bronrij blijft bewaard. Geef alle datums met volledig jaartal op; afzonderlijke gewichtsklassen blijven afzonderlijk.</p><div class="form-grid">${HISTORY_FIELDS.map(([key, label]) => field("correct_" + key, label, "text", sourceValue(values[key]).replace(key.endsWith("_date") ? /T.*$/ : /$^/, ""), key.endsWith("_date") || key.includes("harvest_") ? 'placeholder="JJJJ-MM-DD"' : "")).join("")}</div><label class="inline field"><input type="checkbox" name="exclude" ${existing?.exclude ? "checked" : ""}> Deze rij uitsluiten van import</label>${area("correction-reason", "Onderbouwing van correctie / uitsluiting", existing?.reason || "")}`;
      $("#correction-reason", dialog).required = true;
    }
    $("#history-correct-row", dialog).onchange = drawCorrection;
    drawCorrection();
    bindForm("#history-correction-form", async (data) => {
      const rowNumber = +$("#history-correct-row", dialog).value;
      const fields = Object.fromEntries(
        HISTORY_FIELDS.map(([key]) => [key, data["correct_" + key] || null]),
      );
      overrides = overrides.filter((row) => row.row_number !== rowNumber);
      overrides.push({
        row_number: rowNumber,
        fields,
        exclude: data.exclude === "on",
        reason: data["correction-reason"],
      });
      mapping = { ...mapping, overrides };
      job = await api(`/api/history-imports/${id}/mapping`, {
        method: "POST",
        body: mapping,
      });
      if (active()) draw();
    });
    $("#confirm-history", dialog).onclick = async () => {
      if (
        !confirm(
          "Werkblad, kolomkoppelingen en alle overgebleven rijen gecontroleerd? Deze historische teelten nu definitief registreren?",
        )
      )
        return;
      try {
        await api(`/api/history-imports/${id}/confirm`, {
          method: "POST",
          body: { confirmed: true },
        });
        job = await api(`/api/history-imports/${id}`);
        if (active()) {
          draw();
          toast("Historische teelten bevestigd");
          await getBase();
          if (active()) await historicalImports();
        }
      } catch (error) {
        toast(error.message);
      }
    };
  }
  draw();
}

function excelColumn(number) {
  let label = "";
  while (number) {
    number--;
    label = String.fromCharCode(65 + (number % 26)) + label;
    number = Math.floor(number / 26);
  }
  return label;
}

function dateTime(value) {
  if (!value) return "—";
  const text = String(value),
    timestamp =
      text.includes("T") && !/Z$|[+-]\d\d:\d\d$/.test(text) ? text + "Z" : text;
  return new Intl.DateTimeFormat("nl-NL", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Amsterdam",
  }).format(new Date(timestamp));
}
const INPUT_LABELS = {
  density: ["Plantdichtheid", "takken/m²"],
  m1_weight_g: ["Gewicht meting 1", "g"],
  m1_length_cm: ["Lengte meting 1", "cm"],
  m1_kd_day: ["Werkelijke KD-dag meting 1", ""],
  m2_weight_g: ["Gewicht meting 2", "g"],
  m2_length_cm: ["Lengte meting 2", "cm"],
  m2_kd_day: ["Werkelijke KD-dag meting 2", ""],
  growth_g_day: ["Groei tussen de metingen", "g/dag"],
  stage_daylength_hours: ["Astronomische daglengte bij meting", "uur"],
  stage_daylength_trend: ["Verandering daglengte", "uur/dag"],
  plant_week_sin: ["Seizoenspositie (sinus)", ""],
  plant_week_cos: ["Seizoenspositie (cosinus)", ""],
  ...ENV_FEATURES,
};
function environmentReason(value) {
  if (typeof value !== "string") return sourceValue(value);
  if (ENV_FEATURES[value])
    return (
      "Onvoldoende geverifieerde dekking: " +
      ENV_FEATURES[value][0].toLowerCase() +
      "."
    );
  const labels = {
    supplemental_excel_lighting_used_Priva_always_primary: "Aanvullende Excelbelichting gebruikt; Priva blijft altijd de primaire bron.",
    planning_not_known_at_cutoff:
      "De planning was op deze peildatum niet beschikbaar.",
    invalid_cultivation_window: "De teeltperiode is niet geldig.",
    window_exceeds_bound: "De periode is te lang om volledig te verwerken.",
    bounded_read_truncated:
      "De bronselectie is afgekapt; gebruik een kleinere periode.",
    current_partial_day_not_observed:
      "De lopende dag is nog niet volledig en telt niet mee.",
    daylight_is_estimated_not_verified:
      "Daglicht is een benadering, geen geverifieerde meting.",
    measurement_before_light_out:
      "Een meting ligt vóór licht uit; de fasegrens is onzeker.",
    invalid_second_measurement_boundary:
      "De tweede meting levert geen geldige fasegrens op.",
  };
  if (labels[value]) return labels[value];
  if (value.startsWith("daily_records_cross_phase_boundary_"))
    return "Een dagtotaal kruist een meetmoment en is niet over twee fasen verdeeld.";
  if (value.startsWith("partial_coverage_"))
    return (
      "De bron is slechts gedeeltelijk gedekt: " +
      value.replace("partial_coverage_", "").replaceAll("_", " ") +
      "."
    );
  return value;
}
function forecastInputs(f) {
  if (!("configured_feature_profile" in f)) return "";
  const features = f.features_used || {},
    environment = f.environment;
  const supplemental=environment?.source_ids?.legacy_lighting||f.source_ids?.environment?.legacy_lighting;
  const visible = Object.entries(features).filter(
    ([key]) =>
      key in INPUT_LABELS &&
      key !== "plant_week_sin" &&
      key !== "plant_week_cos",
  );
  const table = visible.length
    ? `<div class="table-wrap"><table><thead><tr><th>Gebruikt kenmerk</th><th>Waarde</th></tr></thead><tbody>${visible
        .map(([key, value]) => {
          const [label, unit] = INPUT_LABELS[key];
          return `<tr><td>${esc(label)}</td><td>${value === null || value === undefined ? "Onbekend" : fmt(value, unit ? " " + unit : "")}</td></tr>`;
        })
        .join("")}</tbody></table></div>`
    : "";
  return `<details class="forecast-inputs"><summary>Gebruikte modelinvoer · ${f.feature_profile ? profileLabel(f.feature_profile) : "nog geen model"}</summary><p class="hint">Ingesteld voor nieuwe modellen: ${profileLabel(f.configured_feature_profile)}. ${f.observed_until ? "Waarnemingen gebruikt tot " + dateTime(f.observed_until) + "." : ""}</p><p class="notice">${esc(f.input_notice || "Alleen informatie die op de invoerpeildatum bekend was kan worden gebruikt.")} Een ontbrekend klimaatkenmerk wordt niet als nul ingevuld.</p>${supplemental?.job_ids?.length?`<p class="notice">Deze modelinvoer gebruikt aanvullende Excelbelichting voor ontbrekende dagen. Priva blijft altijd leidend. ${esc(sourceReferenceText(supplemental))}</p>`:''}${table || '<p class="hint">Er zijn geen numerieke modelinvoeren gebruikt voor deze verwachting.</p>'}${
    environment && f.feature_profile && f.feature_profile !== "basic"
      ? `<details><summary>Klimaatdekking van de gebruikte modelinvoer</summary>${coverageTable(environment.coverage)}${Object.entries(
          environment.phases || {},
        )
          .map(
            ([phase, row]) =>
              `<details><summary>${esc(PHASE_LABELS[phase] || phase)}</summary>${featureTable(row.features)}${coverageTable(row.coverage)}</details>`,
          )
          .join(
            "",
          )}<p class="hint">Bronversies: ${esc((environment.source_ids?.priva_source_ids || []).join(", ") || "geen")} · validaties: ${esc((environment.source_ids?.calibration_ids || []).join(", ") || "geen")}</p></details>`
      : ""
  }${f.unavailable_models?.length ? `<details><summary>Waarom andere modellen niet beschikbaar zijn</summary>${f.unavailable_models.map((model) => `<p class="notice warn">Model #${esc(model.model_id)} · ${profileLabel(model.profile)}<br>${(model.missing || []).map((reason) => esc(environmentReason(reason))).join(" · ")}</p>`).join("")}</details>` : ""}${f.associations?.length ? `<details><summary>Waargenomen modelverbanden</summary><p class="notice warn">${esc(f.association_notice || "Deze samenhang bewijst geen effect van extra licht, water of remstof.")}</p><div class="table-wrap"><table><thead><tr><th>Kenmerk</th><th>Samenhang (g per eenheid)</th></tr></thead><tbody>${f.associations.map((row) => `<tr><td>${esc(INPUT_LABELS[row.feature]?.[0] || row.feature)}</td><td>${fmt(row.coefficient_g_per_unit)}</td></tr>`).join("")}</tbody></table></div><p class="hint">Positief of negatief beschrijft dit getrainde model. Het is geen berekening van een te verwachten ingreepeffect.</p></details>` : ""}<details><summary>Technische herkomst van deze verwachting</summary>${json({ model_id: f.model_id, feature_version: f.feature_version, source_ids: f.source_ids, features_used: f.features_used })}</details></details>`;
}
async function forecastHistoryPanel(id) {
  const alive = pageOwner("cultivation"),
    panel = $("#forecast-history");
  if (!alive() || !panel) return;
  try {
    const rows = await api(`/api/cultivations/${id}/forecasts?limit=100`);
    if (!alive() || !panel.isConnected) return;
    panel.innerHTML = `<h2>Eerdere verwachtingen</h2><p class="hint">Elke vastgelegde prognose houdt zijn oorspronkelijke invoerpeildatum, model en bronversies. Oudere verwachtingen worden niet overschreven.</p>${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Peildatum</th><th>Model / profiel</th><th>Gewicht</th><th>80%-interval</th><th>Status</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${dateTime(row.as_of)}</td><td>${row.model_id ? "#" + esc(row.model_id) : "Geen model"} ${esc(row.response?.model_kind || "")}<br><span class="hint">${row.response?.feature_profile ? profileLabel(row.response.feature_profile) : "Geen modelprofiel"}</span></td><td>${fmt(row.point_g, " g")}</td><td>${row.low_g != null && row.high_g != null ? fmt(row.low_g) + " – " + fmt(row.high_g) + " g" : "Onbekend"}</td><td>${tag(statusLabel(row.status), row.status === "operational" ? "" : "gray")}<details><summary>Herkomst</summary>${json({ observed_until: row.response?.observed_until, source_ids: row.source_ids, features: row.features })}</details></td></tr>`).join("")}</tbody></table></div>` : empty("Nog geen vastgelegde verwachtingen")}`;
  } catch (error) {
    if (alive() && panel.isConnected)
      panel.innerHTML = `<h2>Eerdere verwachtingen</h2><p class="notice error">${esc(error.message)}</p>`;
  }
}

async function varietyTargetHistory(id) {
  const alive=pageOwner('manage'),panel=$('#variety-target-history');
  if(!alive()||!panel)return;
  try {
    const rows=await api(`/api/varieties/${id}/targets`);
    if(!alive()||!panel.isConnected)return;
    panel.innerHTML=`<summary>Doelgewichthistorie (${rows.length})</summary><p class="hint">Elk doelgewicht geldt vanaf het vastgelegde moment. Het wordt niet teruggeprojecteerd op eerdere prognoses.</p>${rows.length?`<div class="table-wrap"><table><thead><tr><th>Vastgelegd op</th><th>Doelgewicht</th><th>Versie</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${dateTime(row.created_at)}</td><td>${fmt(row.target_weight_g,' g')}</td><td>#${esc(row.id)}</td></tr>`).join('')}</tbody></table></div>`:empty('Geen historische doelversies bekend')}`;
  }catch(error){if(alive()&&panel.isConnected)panel.innerHTML=`<summary>Doelgewichthistorie</summary><p class="notice error">${esc(error.message)}</p>`;}
}
async function comparisonPanel() {
  const alive=pageOwner('models'),panel=$('#control-comparison');
  if(!alive()||!panel)return;
  const rounds=[...new Set(cultivations.map(c=>c.round_name))];
  panel.innerHTML=`<h2>Bijgestuurd & controle: beschrijvend vergelijken</h2><p class="notice warn">Een gewichtsverschil tussen groepen bewijst geen effect van extra licht, water of remstof. Weer, selectie en andere ingrepen kunnen verschillen verklaren.</p>${rounds.length?`<form id="comparison-form"><div class="form-grid"><div class="field"><label for="comparison-round">Ronde</label><select id="comparison-round" name="round_name">${rounds.map(round=>`<option value="${esc(round)}">${esc(round)}</option>`).join('')}</select></div>${field('comparison-asof','Peildatum (optioneel, Nederlandse tijd)','datetime-local','',`max="${localNow()}"`)}</div>${formError()}<button type="submit">Groepen bekijken</button></form><div id="comparison-result" class="section"></div>`:empty('Nog geen rondes','Registreer eerst kapteelten met de categorie standaard, bijgestuurd of controle.')}`;
  if(!rounds.length)return;
  const target=$('#comparison-result',panel);
  let request=0;
  async function load(data) {
    const version=++request,params=new URLSearchParams({round_name:data.round_name});
    if(data['comparison-asof'])params.set('as_of',data['comparison-asof']);
    const result=await api('/api/interventions/compare?'+params);
    if(!alive()||!panel.isConnected||!target.isConnected||version!==request)return;
    const groups=[['standard','Standaard'],['control','Controle'],['adjusted','Bijgestuurd']];
    const statuses={overlapping_bays:'Controle- en bijgestuurde teelten delen een kap; een onafhankelijk groepsverschil is daarom niet beschikbaar.',insufficient_controls:'Onvoldoende onafhankelijke controlekappen (minimaal vier vereist).',no_adjusted_or_harvest_data:'Geen complete combinatie van bijgestuurde teelten en controles.',truncated_data:'De selectie is afgekapt; het verschil wordt niet berekend.',descriptive_noncausal:'Beschrijvend verschil; geen bewezen ingreepeffect.'};
    const exclusions={missing_or_nonpositive_harvest_weight:'Het werkelijke eindgewicht ontbreekt of is ongeldig.',multiple_harvest_quality_classes:'Meerdere gewichtsklassen hebben geen gezamenlijk soortgewicht.',no_category_revision_known_as_of:'Teeltcategorie was op deze peildatum nog niet geregistreerd.',unknown_category:'Teeltcategorie onbekend.',ineligible_label_quality:'Teeltlabel is ambigu of historisch toegewezen.',no_known_actual_harvest:'Geen geschikte werkelijke oogst op deze peildatum.'};
    target.innerHTML=`<p class="hint">Peildatum ${dateTime(result.as_of)} · ${esc(result.round_name)}</p>${result.truncation?.cultivations||result.truncation?.events?'<p class="notice warn">De bronselectie is begrensd en onvolledig. Deze resultaten geven geen volledige vergelijking.</p>':''}${result.comparisons?.length?result.comparisons.map(comparison=>`<section style="margin-bottom:24px"><h3>${esc(comparison.variety||'Soort')}</h3><div class="table-wrap"><table><thead><tr><th>Groep</th><th>Teelten</th><th>Verschillende kappen</th><th>Gem. oogstgewicht</th><th>Spreiding (SD)</th><th>Oogst − prognose</th></tr></thead><tbody>${groups.map(([key,label])=>{const group=comparison.groups?.[key]||{};return `<tr><td>${label}</td><td>${fmt(group.cultivations)}</td><td>${fmt(group.bays)}</td><td>${fmt(group.mean_final_weight_g,' g')}</td><td>${fmt(group.sd_final_weight_g,' g')}</td><td>${fmt(group.mean_prediction_residual_g,' g')}<br><span class="hint">n=${fmt(group.residual_n)}${(group.residual_forecasts||[]).map(source=>`<br>${fmt(source.count)} ${source.status==='experimental'?'experimenteel':'vrijgegeven'}`).join('')}</span></td></tr>`}).join('')}</tbody></table></div><p class="notice ${comparison.difference_status==='descriptive_noncausal'?'':'warn'}">${esc(statuses[comparison.difference_status]||'Geen volledig vergelijkbare groepen beschikbaar.')}<br>Onafhankelijke controlekappen zonder overlap: ${fmt(comparison.independent_control_bays??comparison.control_bays)}.<br>Bijgestuurd minus controle: <strong>${fmt(comparison.adjusted_minus_control_observed_difference_g,' g')}</strong>.<br>Verschil gemiddelde prognosefout: ${fmt(comparison.average_prediction_residual_difference_g,' g')}.</p></section>`).join(''):empty('Nog geen geschikte oogstvergelijking','Zonder betrouwbare, afgeronde oogsten blijft het gewichtsverschil onbekend.')}<details><summary>Geregistreerde gebeurtenissen (${result.events?.length||0})</summary><p class="hint">Hoeveelheden blijven per oorspronkelijke eenheid en reikwijdte gescheiden; selectief en volvelds worden niet bij elkaar opgeteld.</p>${result.events?.length?`<div class="table-wrap"><table><thead><tr><th>Soort / groep</th><th>Gebeurtenis</th><th>Aantal</th><th>Hoeveelheid</th><th>Reikwijdte</th></tr></thead><tbody>${result.events.map(event=>`<tr><td>${esc(varieties.find(v=>v.id===event.variety_id)?.name||'—')}<br>${esc(statusLabel(event.category))}</td><td>${esc(event.kind)}</td><td>${fmt(event.events)}</td><td>${fmt(event.total_amount,event.unit&&event.unit!=='unspecified'?' '+event.unit:'')}</td><td>${esc(event.scope==='unspecified'?'Niet vastgelegd':event.scope)}</td></tr>`).join('')}</tbody></table></div>`:empty('Geen gebeurtenissen in deze selectie')}</details><details><summary>Uitgesloten teelten (${result.excluded_cultivations?.length||0})</summary>${(result.excluded_cultivations||[]).map(row=>`<p class="hint">Kap ${esc(row.bay_number)} · ${esc(exclusions[row.reason]||row.reason)}</p>`).join('')||'<p class="hint">Geen aanvullende uitsluitingen.</p>'}</details>${(result.notices||[]).map(notice=>`<p class="hint">${esc(notice)}</p>`).join('')}`;
  }
  bindForm('#comparison-form',load);
  try {await load(payload($('#comparison-form',panel)));}catch(error){if(alive()&&target.isConnected)target.innerHTML=`<p class="notice error">${esc(error.message)}</p>`;}
}

const LEGACY_REASON_LABELS={year_missing:'Het plantjaar is niet bevestigd.',planning_not_verified_actual:'Dit zijn planningsgegevens; werkelijke datums zijn nog niet bevestigd.',schema_empty:'De soortcode ontbreekt.',schema_unrecognized:'De soortcode is niet eenduidig herkenbaar.',schema_invalid_beds:'De verdeling van de soortbedden klopt niet.',bed_count_unknown:'Het aantal bedden is onbekend.',inactive_species:'Historische, niet meer actieve soortcode.',partial_bed_allocation:'De bron beschrijft minder dan zes bedden.',mixed_species_review:'Gemengde kap: soorttoewijzing en gewicht moeten worden gecontroleerd.',formula_cache_unverified:'De bron bevat een formule; de bewaarde uitkomst is niet geverifieerd.',plant_week_day_unknown:'Plantweek of weekdag ontbreekt of is niet bevestigd.',duplicate_bay_in_round:'Deze kap staat meer dan één keer in de ronde.',round_label_conflict:'De rondenaam in het blad verschilt van de bladnaam.',unrecognized_round_layout:'De indeling van dit rondeblad is niet herkenbaar.'};
function legacyReason(value) {return LEGACY_REASON_LABELS[value]||environmentReason(value);}
function sourceCellsTable(cells,sheet='') {
  if(!cells||typeof cells!=='object')return '<p class="hint">Geen celverwijzingen beschikbaar.</p>';
  const labels=Object.fromEntries(HISTORY_FIELDS);
  Object.assign(labels,{schema_code:'Soortcode',plant_week:'Plantweek',plant_weekday:'Plantdag',light_out_week:'Week licht uit',light_out_weekday:'Dag licht uit',planned_harvest_week:'Geplande oogstweek',planned_harvest_weekday:'Geplande oogstdag',interlight:'Tussenlicht',remarks:'Opmerkingen'});
  return `<div class="table-wrap"><table><thead><tr><th>Gegeven / broncel</th><th>Letterlijke waarde</th><th>Formule / bewaarde uitkomst</th></tr></thead><tbody>${Object.entries(cells).map(([key,evidence])=>{const rows=Array.isArray(evidence)?evidence:[evidence];return rows.map(item=>{if(item===null||typeof item!=='object')return `<tr><td>${esc(labels[key]||key)}</td><td>${esc(sourceValue(item))}</td><td>—</td></tr>`;return `<tr><td>${esc(labels[key]||key)}<br><span class="hint">${esc(item.sheet||sheet)} ${esc(item.cell||item.coordinate||'')}</span></td><td>${esc(sourceValue(item.value??item.raw))||'—'}</td><td>${item.formula?`<code>${esc(item.formula)}</code><br>Bewaard: ${esc(sourceValue(item.cached))||'onbekend'}<br><span class="tag warn">Niet geverifieerd</span>`:'Geen formule'}</td></tr>`}).join('')}).join('')}</tbody></table></div>`;
}

function companyProfileLabel(profile) {return {legacy_weight_workbook:'Bedrijfsbestand: teeltschema en gewichten',legacy_program_workbook:'Bedrijfsbestand: Teeltprogramma'}[profile]||profile;}
function companyImportsPanel(jobs) {
  const panel=$('#company-imports');if(!panel)return;
  const weights=jobs.filter(job=>job.profile==='legacy_weight_workbook'),programs=jobs.filter(job=>job.profile==='legacy_program_workbook');
  if(!weights.length&&!programs.length)return;
  panel.hidden=false;
  panel.innerHTML=`<h2>Herkende bedrijfsbestanden koppelen</h2><p class="hint">De herkenning gebruikt werkbladstructuur en kolomkoppen. Het teeltschema levert gewichten en datumbewijs; het Teeltprogramma levert soortbedden en planningsbewijs. Rondekoppelingen en plantjaren blijven ter controle.</p><form id="company-import-form"><div class="form-grid"><div class="field"><label for="company-weight">Bestand met teeltschema / eindgewichten</label><select id="company-weight" name="weight_id" required><option value="">Kies de gewichtsbron</option>${options(weights,'id',job=>`#${job.id} · ${job.filename}`)}</select></div><div class="field"><label for="company-program">Bijbehorend Teeltprogramma</label><select id="company-program" name="program_id" required><option value="">Kies de bijbehorende planning</option>${options(programs,'id',job=>`#${job.id} · ${job.filename}`)}</select></div></div>${!weights.length||!programs.length?'<p class="notice warn">Upload beide bestanden om het bedrijfsvoorbeeld te koppelen. Zelf kolommen koppelen blijft beschikbaar via de importgeschiedenis.</p>':''}${formError()}<button type="submit" class="primary" ${!weights.length||!programs.length?'disabled title="Beide herkende bedrijfsbestanden zijn nodig"':''}>Bedrijfsvoorbeeld voorbereiden</button></form><p class="notice warn">Jaartallen in bestandsnamen zijn geen jaarankers. Bevestig de plantjaren en weekdagbetekenis zelf voordat datums definitief worden geïmporteerd.</p>`;
  bindForm('#company-import-form',async data=>legacyCompanyReview(+data.weight_id,+data.program_id));
}
async function legacyCompanyReview(weightId,programId) {
  const alive=pageOwner('imports');
  let job=await api(`/api/history-imports/${weightId}`);
  if(!alive())return;
  const sources=await api('/api/history-imports');if(!alive())return;
  const programs=(sources.imports||sources).filter(source=>source.profile==='legacy_program_workbook');
  const existing=job.mapping?.program_import_id?job.mapping:{};
  let config={program_import_id:programId||existing.program_import_id,round_years:existing.round_years||{},weekday_convention:'iso_monday_1',single_species_beds:existing.single_species_beds??null,group_rounds:existing.group_rounds||{},group_round_reason:existing.group_round_reason||'',program_record_selection:existing.program_record_selection||{},program_record_selection_reason:existing.program_record_selection_reason||'',overrides:existing.overrides||[],include_unweighted_planning:existing.include_unweighted_planning||false,include_program_only_planning:existing.include_program_only_planning||false,planning_status:existing.planning_status||'historical',exclude_ambiguous:false,exclusion_reason:''};
  let weekdayConfirmed=Object.keys(config.round_years).length>0;
  const dialog=modal('Bedrijfsbestanden · gecontroleerde reconstructie','<div id="legacy-company-detail"><p class="hint">Bronnen en rondekoppelingen worden verkend…</p></div>');
  const container=$('#legacy-company-detail',dialog),active=()=>alive()&&container.isConnected&&dialog.open;
  async function preview(){
    const controls=$$('input,select,textarea,button',container).map(element=>[element,element.disabled]);
    controls.forEach(([element])=>element.disabled=true);container.setAttribute('aria-busy','true');
    try{job=await api(`/api/history-imports/${weightId}/legacy-preview`,{method:'POST',body:config});if(active())draw();}
    finally{controls.forEach(([element,disabled])=>{if(element.isConnected)element.disabled=disabled});container.removeAttribute('aria-busy');if(active())applyPermissions();}
  }
  const roundKey=value=>typeof value==='string'?value:value.round_name||value.name||String(value.round_number??value.round??'');
  function draw() {
    if(!active())return;
    const profile=job.legacy_profile||{},matches=profile.matches||[],rounds=(profile.program_rounds||[]).map(roundKey).filter(Boolean),finalized=job.status==='confirmed';
    const selectedRounds=[...new Set(matches.map(match=>config.group_rounds[String(match.group_index)]||match.matched_round).filter(Boolean))];
    const yearRounds=config.include_program_only_planning?rounds:selectedRounds;
    container.innerHTML=`<p class="notice"><strong>Priva gaat altijd voor.</strong> Dit voorbeeld reconstrueert historische teelten, planning en gewichten. Het werkblad met vijfminutenbelichting wordt via een aparte aanvullende import verwerkt.</p><p class="hint">Gewichtsbron #${weightId} · ${esc(job.filename)} · ${esc(job.sha256)}</p><form id="legacy-config-form"><div class="field"><label for="legacy-program">Bijbehorend Teeltprogramma</label><select id="legacy-program" name="program_import_id" required>${options(programs,'id',source=>`#${source.id} · ${source.filename}`,config.program_import_id)}</select></div><h3>1. Controleer rondekoppelingen</h3><p class="hint">Voorstellen zijn gebaseerd op overeenkomstige kap-, plantweek- en weekdagwaarden. Controleer conflicten aan de hand van de bron; een bestandsnaam bepaalt de koppeling niet.</p>${matches.length?`<div class="table-wrap"><table><thead><tr><th>Blok / bronrijen</th><th>Gekoppelde ronde</th><th>Letterlijke bronankers</th></tr></thead><tbody>${matches.map(match=>`<tr><td>Blok ${esc(match.group_index)}<br>${esc(match.start_row)}–${esc(match.end_row)}</td><td><select aria-label="Teeltprogrammaronde voor blok ${esc(match.group_index)}" data-group-round="${esc(match.group_index)}" data-proposed-round="${esc(match.matched_round||'')}"><option value="">Geen eenduidige koppeling</option>${rounds.map(round=>`<option value="${esc(round)}" ${(config.group_rounds[String(match.group_index)]||match.matched_round)===round?'selected':''}>${esc(round)}</option>`).join('')}</select><label class="inline"><input type="checkbox" data-confirm-group="${esc(match.group_index)}" ${config.group_rounds[String(match.group_index)]?'checked':''}> Deze rondekoppeling handmatig bevestigen</label></td><td>${(match.candidates||[]).map(candidate=>`${esc(candidate.round_name)}: ${fmt(candidate.exact)} gelijk / ${fmt(candidate.contradictions)} tegenstrijdig`).join('<br>')||'Geen bruikbare ankers'}</td></tr>`).join('')}</tbody></table></div>`:empty('Geen gekoppelde rondeblokken','Controleer of de geselecteerde bronnen bij elkaar horen.')}${area('legacy-group-reason','Onderbouwing van gewijzigde rondekoppelingen',config.group_round_reason)}<h3>2. Bevestig plantjaren en weekdagen</h3><p class="notice warn">Vul het ISO-jaar van de eerste plantweek van elke ronde in. Overgangen naar een volgend kalenderjaar worden alleen op basis van deze expliciete ankers gereconstrueerd. Geen jaar wordt uit de bestandsnaam overgenomen.</p>${yearRounds.length?`<div class="form-grid">${yearRounds.map((round,index)=>`<div class="field"><label for="legacy-year-${index}">${esc(round)} · plantjaar</label><input id="legacy-year-${index}" data-year-round="${esc(round)}" type="number" min="1900" max="2100" step="1" value="${esc(config.round_years[round]??'')}" placeholder="Onbekend totdat bevestigd"></div>`).join('')}</div>`:empty('Kies eerst een rondekoppeling','Pas het voorbeeld opnieuw toe om de bijbehorende jaarankers te tonen.')}<div class="form-grid"><div class="field"><label for="legacy-weekday">Geverifieerde betekenis van weekdagen</label><select id="legacy-weekday" name="weekday_convention" required><option value="">Nog niet bevestigd</option><option value="iso_monday_1" ${weekdayConfirmed?'selected':''}>Maandag = 1 t/m zondag = 7</option></select></div>${field('legacy-single-beds','Bedden bij enkelsoortige code (leeg = onbekend)','number',config.single_species_beds??'','min="1" max="6" step="1"')}</div><label class="inline field"><input type="checkbox" name="years_confirmed" required> Ik heb de ingevulde plantjaren en weekdagbetekenis aan de bron gecontroleerd.</label><details><summary>Opties voor ontbrekende gewichten en planning</summary><label class="inline field"><input type="checkbox" name="include_unweighted_planning" ${config.include_unweighted_planning?'checked':''}> Kapregels zonder eindgewicht als historische planning meenemen.</label><label class="inline field"><input type="checkbox" name="include_program_only_planning" ${config.include_program_only_planning?'checked':''}> Ook alleen in het Teeltprogramma aanwezige regels als historische planning meenemen.</label><div class="field"><label for="legacy-planning-status">Status voor regels zonder eindgewicht</label><select id="legacy-planning-status" name="planning_status"><option value="historical" ${config.planning_status==='historical'?'selected':''}>Historische planning</option><option value="planned" ${config.planning_status==='planned'?'selected':''}>Gepland (bewust actief in planning opnemen)</option></select></div><p class="hint">Een geplande datum wordt geen werkelijke oogstdatum. Ontbrekende gewichten blijven onbekend.</p><label class="inline field"><input type="checkbox" name="exclude_ambiguous" ${config.exclude_ambiguous?'checked':''}> Alle resterende onduidelijke regels uitsluiten van definitieve import.</label>${area('legacy-exclusion-reason','Reden voor gezamenlijke uitsluiting',config.exclusion_reason)}</details>${formError()}<button type="submit" class="primary">Jaarankers en koppelingen opnieuw controleren</button></form><section id="legacy-company-rows" class="section"></section>`;
    bindForm('#legacy-config-form',async data=>{
      const overrides={};$$('[data-group-round]',dialog).forEach(input=>{if(input.value&&(input.value!==input.dataset.proposedRound||$(`[data-confirm-group="${input.dataset.groupRound}"]`,dialog)?.checked))overrides[input.dataset.groupRound]=input.value});
      if(Object.keys(overrides).length&&!data['legacy-group-reason']?.trim())throw new Error('Onderbouw elke gewijzigde rondekoppeling.');
      if(data.exclude_ambiguous==='on'&&!data['legacy-exclusion-reason']?.trim())throw new Error('Geef een reden voor het uitsluiten van onduidelijke regels.');
      if(data.exclude_ambiguous==='on'&&!confirm('Alle resterende onduidelijke bronregels uitsluiten? De bron en reden blijven bewaard.'))return;
      config={...config,program_import_id:+data.program_import_id,round_years:Object.fromEntries($$('[data-year-round]',dialog).filter(input=>input.value).map(input=>[input.dataset.yearRound,+input.value])),weekday_convention:data.weekday_convention,single_species_beds:data['legacy-single-beds']?+data['legacy-single-beds']:null,group_rounds:overrides,group_round_reason:data['legacy-group-reason'],include_unweighted_planning:data.include_unweighted_planning==='on',include_program_only_planning:data.include_program_only_planning==='on',planning_status:data.planning_status,exclude_ambiguous:data.exclude_ambiguous==='on',exclusion_reason:data['legacy-exclusion-reason']||''};
      weekdayConfirmed=true;await preview();
    });
    drawRows();
    if(finalized)$$('input,select,textarea,button',$('#legacy-config-form',dialog)).forEach(input=>input.disabled=true);
    applyPermissions();
  }
  function drawRows(){
    if(!active())return;
    const target=$('#legacy-company-rows',dialog),rows=job.preview?.rows||[],summary=job.preview?.summary||job.summary||{},finalized=job.status==='confirmed';
    target.innerHTML=`<h3>3. Controleer kappen, soorten en gewichtsklassen</h3><div class="notice">${Object.entries(summary).map(([key,value])=>`${esc(({total_rows:'Bronregels',valid:'Geldig',ambiguous:'Te beoordelen',excluded:'Uitgesloten',duplicate:'Al geïmporteerd'})[key]||key)}: ${esc(sourceValue(value))}`).join(' · ')}</div><p class="hint">Gemengde kappen en toegewezen historische gewichten blijven gemarkeerd. 95 / 65 g blijft twee klassen, zonder gemiddeld eindgewicht. Een onduidelijke soort is geen zuiver trainingslabel.</p>${rows.length?`<div class="table-wrap history-preview"><table><thead><tr><th>Bronrij / status</th><th>Ronde / kap / soort</th><th>Planten / licht uit / oogst</th><th>Gewicht</th><th>Beoordeling</th></tr></thead><tbody>${rows.map(row=>{const value=row.effective||{};return `<tr><td>${esc(row.row_number)} ${tag(({valid:'Geldig',ambiguous:'Beoordelen',excluded:'Uitgesloten',duplicate:'Bestaand',imported:'Geïmporteerd'})[row.status]||row.status,row.status==='valid'?'':'warn')}</td><td>${esc(value.round_name||'Onbekende ronde')}<br>Kap ${esc(value.bay_number??row.original?.bay_number??'—')} · ${esc(sourceValue(value.variety??value.schema_code)||'Onbekende soort')}</td><td>${legacyDate(value.plant_date)}<br>${legacyDate(value.light_out_date)}<br>${legacyDate(value.harvest_end_date)}</td><td>${legacyRowWeight(row)}</td><td>${(row.reasons||[]).map(reason=>esc(legacyReason(reason))).join('<br>')}</td></tr>`}).join('')}</tbody></table></div>`:empty('Geen teeltregels beschikbaar')}${rows.length&&!finalized?`<form id="legacy-row-form" class="history-row-editor"><h3>Bronregel beoordelen</h3><div class="field"><label for="legacy-review-row">Bronrij</label><select id="legacy-review-row">${rows.slice().sort((a,b)=>(a.status==='valid')-(b.status==='valid')||a.row_number-b.row_number).map(row=>`<option value="${row.row_number}">Rij ${row.row_number} · ${esc(row.effective?.round_name||'')} · kap ${esc(row.effective?.bay_number??row.original?.bay_number??'')} · ${row.status==='ambiguous'?'controle nodig':row.status}</option>`).join('')}</select></div><div id="legacy-review-fields"></div>${formError()}<button type="submit">Rijcorrectie opnieuw controleren</button></form><button class="primary" id="legacy-confirm" ${summary.ambiguous>0?'disabled title="Los alle onduidelijke regels op of sluit ze met een reden uit"':''}>Gecontroleerde teelten definitief importeren</button>`:finalized?'<p class="tag">Bevestigd · bronkoppelingen bewaard</p>':''}`;
    const offset=job.preview?.offset||0,limit=job.preview?.limit||200,total=job.preview?.total||rows.length;
    if(total>limit){target.insertAdjacentHTML('afterbegin',`<div class="actions"><button id="legacy-previous" class="small" ${offset?'':'disabled'}>← Vorige rijen</button><span class="hint">${offset+1}–${Math.min(offset+rows.length,total)} van ${total}</span><button id="legacy-next" class="small" ${offset+limit>=total?'disabled':''}>Volgende rijen →</button></div>`);const page=async next=>{try{const response=await api(`/api/history-imports/${weightId}/rows?offset=${next}&limit=${limit}`);if(active()){job.preview=response;drawRows();applyPermissions()}}catch(error){toast(error.message)}};$('#legacy-previous',dialog).onclick=()=>page(Math.max(0,offset-limit));$('#legacy-next',dialog).onclick=()=>page(offset+limit);}
    if(!rows.length||finalized)return;
    function editor(){
      const row=rows.find(row=>row.row_number===+$('#legacy-review-row',dialog).value),existing=config.overrides.find(item=>item.row_number===row.row_number),value={...row.effective,...existing?.fields};
      $('#legacy-review-fields',dialog).innerHTML=`<p class="hint">Corrigeer uitsluitend wat je hebt gecontroleerd. Lege datums en soortcodes blijven onbekend; formule-uitkomsten worden niet automatisch als feiten overgenomen.</p><div class="form-grid">${HISTORY_FIELDS.map(([key,label])=>field('legacy-correct-'+key,label,'text',sourceValue(value[key]).replace(key.endsWith('_date')?/T.*$/:/$^/,''))).join('')}</div><label class="inline field"><input name="exclude" type="checkbox" ${existing?.exclude?'checked':''}> Deze bronregel uitsluiten</label>${row.original?.anchor_mismatch?`<label class="inline field"><input type="checkbox" name="accept_anchor_mismatch" ${existing?.accept_anchor_mismatch?'checked':''}> Ik heb de tegenstrijdige plantweek/-dag gecontroleerd en accepteer deze bronkoppeling.</label>`:''}${row.original?.duplicate_program_bay?`<div class="field"><label for="legacy-program-record">Kies de gecontroleerde programmarij voor deze ronde en kap</label><select id="legacy-program-record" name="program_record_row"><option value="">Nog geen bronrij gekozen</option>${(row.original.program_candidates||[]).map(candidate=>`<option value="${candidate.row_number}" ${config.program_record_selection[row.effective?.round_name+':'+row.effective?.bay_number]===candidate.row_number?'selected':''}>Programmarij ${candidate.row_number} · ${esc(sourceValue(candidate.source_cells?.schema_code?.value))}</option>`).join('')}</select></div><details><summary>Alle dubbele programmarijen vergelijken</summary>${(row.original.program_candidates||[]).map(candidate=>`<h3>Programmarij ${candidate.row_number}</h3>${sourceCellsTable(candidate.source_cells,row.effective?.round_name||'Teeltprogramma')}`).join('')}</details>`:''}${area('legacy-row-reason','Onderbouwing van de rijbeoordeling',existing?.reason||'')}<details><summary>Broncellen en oorspronkelijke gegevens</summary>${sourceCellsTable(row.original?.weight_source_cells||row.original?.source_cells||row.source_cells,row.source?.sheet||row.original?.sheet||'teeltschema')}${row.original?.program_source_cells?sourceCellsTable(row.original.program_source_cells,row.source?.program_sheet||row.effective?.round_name||'Teeltprogramma'):''}</details>`;
      $('#legacy-row-reason',dialog).required=true;
    }
    $('#legacy-review-row',dialog).onchange=editor;editor();
    bindForm('#legacy-row-form',async data=>{
      const number=+$('#legacy-review-row',dialog).value,row=rows.find(row=>row.row_number===number),existing=config.overrides.find(item=>item.row_number===number),fields={...existing?.fields};
      HISTORY_FIELDS.forEach(([key])=>{const baseline=sourceValue(row.effective?.[key]).replace(key.endsWith('_date')?/T.*$/:/$^/,'');if(data['legacy-correct-'+key]!==baseline)fields[key]=data['legacy-correct-'+key]||null});
      if(data.program_record_row){config.program_record_selection[row.effective.round_name+':'+row.effective.bay_number]=+data.program_record_row;config.program_record_selection_reason=data['legacy-row-reason'];}
      config.overrides=config.overrides.filter(item=>item.row_number!==number);config.overrides.push({row_number:number,fields,exclude:data.exclude==='on',accept_anchor_mismatch:data.accept_anchor_mismatch==='on',reason:data['legacy-row-reason']});await preview();
    });
    $('#legacy-confirm',dialog).onclick=async()=>{if(!confirm('Jaarankers, weekdagen, soortbedden, gewichtsklassen en uitgesloten regels gecontroleerd? Deze historische teelten definitief registreren?'))return;try{await api(`/api/history-imports/${weightId}/confirm`,{method:'POST',body:{confirmed:true}});job=await api(`/api/history-imports/${weightId}`);if(active()){draw();toast('Bedrijfshistorie bevestigd');await getBase();if(active())await historicalImports()}}catch(error){toast(error.message)}};
  }
  function legacyRowWeight(row){
    const classes=(row.cultivations||[]).flatMap(cultivation=>cultivation.quality_classes||[]);
    if(classes.length)return classes.map(item=>fmt(item.weight_g,' g')).join(' / ');
    const raw=row.effective?.weight_g??row.effective?.weight_raw??row.original?.weight_raw??row.original?.weight_g;
    return raw===null||raw===undefined||raw===''?'Onbekend':esc(sourceValue(raw))+' g';
  }
  try {if(job.legacy_profile&&job.mapping?.program_import_id===config.program_import_id)draw();else await preview();}catch(error){if(active())container.innerHTML=`<p class="notice error">${esc(error.message)}</p><p class="hint">De uploads blijven bewaard. Sluit dit venster en controleer de geselecteerde bestanden.</p>`;}
}

function legacyDate(value){if(!value)return 'Onbekend';const text=String(value);return /^\d{4}-\d{2}-\d{2}/.test(text)&&Number.isFinite(Date.parse(text))?date(text):esc(text)+' (niet gevalideerd)';}
function rawSourceTable(rows){
  if(rows&&!Array.isArray(rows))rows=Object.entries(rows).map(([number,cells])=>({bronrij:number,...(cells&&typeof cells==='object'?cells:{waarde:cells})}));
  if(!rows?.length)return empty('Geen bronvoorbeeld beschikbaar');
  const keys=[...new Set(rows.flatMap(row=>Array.isArray(row)?row.map((_,index)=>String(index)):Object.keys(row||{})))].slice(0,45);
  return `<div class="table-wrap history-preview"><table><thead><tr>${keys.map(key=>`<th>${esc(key)}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,12).map(row=>`<tr>${keys.map(key=>`<td>${esc(sourceValue(row[key]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
async function legacyLighting(){
  const alive=pageOwner('imports');
  const [sources,result]=await Promise.all([api('/api/history-imports'),api('/api/legacy-lighting')]);if(!alive())return;
  let jobs=Array.isArray(result)?result:result.jobs||[];
  const books=(sources.imports||sources).filter(book=>book.profile==='legacy_weight_workbook');
  $('#main').innerHTML=pageHead('Aanvullende Excelbelichting','Vijfminutenregels als aparte, herleidbare bron bewaren.')+importTabs('lighting')+`<p class="notice"><strong>Priva gaat altijd voor.</strong> Excel mag ontbrekende dagen aanvullen. Aanwezige Priva-bronnen worden nooit vervangen, ook bij een lege of onbetrouwbare Priva-waarde. Lichtbronnen worden niet dubbel opgeteld.</p><section class="card"><form id="lighting-create-form"><h2>Werkblad data verkennen</h2><p class="hint">De grote bron wordt in een achtergrondtaak verwerkt. Je kunt dit scherm later opnieuw openen; de voortgang blijft bewaard.</p><div class="field"><label for="lighting-source-book">Geüpload werkboek</label><select id="lighting-source-book" name="history_import_id" required><option value="">Kies de aanvullende bron</option>${options(books,'id',book=>`#${book.id} · ${book.filename}`)}</select></div>${books.length?'':'<p class="notice warn">Upload eerst het oorspronkelijke werkboek via Historische teelten · Excel.</p>'}${formError()}<button type="submit" class="primary" ${books.length?'':'disabled title="Eerst het herkende werkboek uploaden"'}>Aanvullende lichtbron verkennen</button></form></section><section class="card"><div class="section-title"><h2>Verwerking & bronversies</h2><button id="lighting-list-refresh" class="small">↻ Vernieuwen</button></div><div id="lighting-job-list"></div></section>`;
  const target=$('#lighting-job-list');
  function draw(){if(!alive()||!target.isConnected)return;target.innerHTML=jobs.length?jobs.map(job=>`<article style="padding:15px 0;border-bottom:1px solid var(--line)"><div class="section-title"><div><h3>Taak #${job.id} · versie ${esc(job.version)} ${tag(statusLabel(job.status),'gray')}</h3><p class="hint">${esc(job.filename)} · ${fmt(job.progress_rows)} / ${job.total_rows?fmt(job.total_rows):'nog onbekend'} rijen</p></div><button class="small" data-lighting-job="${job.id}">Controleren →</button></div>${['queued','previewing','importing'].includes(job.status)?lightingProgress(job):''}${job.error?`<p class="notice error">${esc(job.error)}</p>`:''}</article>`).join(''):empty('Nog geen aanvullende lichtbronnen');$$('[data-lighting-job]',target).forEach(button=>button.onclick=()=>lightingJobReview(button.dataset.lightingJob));}
  async function refresh(){const value=await api('/api/legacy-lighting');if(!alive()||!target.isConnected)return;jobs=Array.isArray(value)?value:value.jobs||[];draw()}
  draw();$('#lighting-list-refresh').onclick=()=>refresh().catch(error=>toast(error.message));
  clearInterval(poll);poll=setInterval(()=>{if(alive()&&jobs.some(job=>['queued','previewing','importing'].includes(job.status)))refresh().catch(()=>{})},3000);
  bindForm('#lighting-create-form',async data=>{const job=await api(`/api/legacy-lighting/from-history/${data.history_import_id}`,{method:'POST',body:{new_version:false}});if(!alive())return;await refresh();if(alive())await lightingJobReview(job.id)});applyPermissions();
}
function lightingProgress(job){return `<progress aria-label="Voortgang aanvullende Excelbron" ${job.total_rows?`value="${job.progress_rows||0}" max="${job.total_rows}"`:''}></progress>`;}
async function lightingJobReview(id){
  const alive=pageOwner('imports');let job=await api(`/api/legacy-lighting/${id}`);if(!alive())return;
  const dialog=modal('Aanvullende Excelbron · broncontrole','<div id="lighting-job-detail"></div>'),container=$('#lighting-job-detail',dialog),active=()=>alive()&&container.isConnected&&dialog.open;let timer;
  dialog.addEventListener('close',()=>clearTimeout(timer),{once:true});
  async function refresh(){job=await api(`/api/legacy-lighting/${id}`);if(active())draw()}
  async function action(name){try{job=await api(`/api/legacy-lighting/${id}/${name}`,{method:'POST'});if(active())draw()}catch(error){toast(error.message)}}
  function draw(){
    clearTimeout(timer);if(!active())return;
    const preview=job.preview||{},interpretation=job.interpretation||{},busy=['queued','previewing','importing'].includes(job.status);
    container.innerHTML=`<p class="notice"><strong>Aanvullende Excelbron</strong> · werkboek #${esc(job.history_import_id)} · bronversie ${esc(job.version)}.<br>Priva gaat altijd voor; de aanvullende bron blijft apart herkenbaar.</p><h3>${esc(statusLabel(job.status))}</h3><p class="hint">${fmt(job.progress_rows)} / ${job.total_rows?fmt(job.total_rows):'nog onbekend'} rijen · ${esc(job.phase||'')}</p>${busy?lightingProgress(job):''}${job.error?`<p class="notice error">${esc(job.error)}</p>`:''}<p>Bronperiode: <strong>${legacyDate(preview.start)} – ${legacyDate(preview.end)}</strong> · ${fmt(preview.row_count)} rijen.</p><details><summary>Werkbladkoppen, modules en ruwe waarden</summary><p class="hint">${esc(job.filename)} · werkblad ${esc(preview.sheet||'data')}<br>SHA-256 ${esc(job.checksum)}</p>${rawSourceTable(preview.header_rows)}${preview.modules?.length?`<div class="table-wrap"><table><thead><tr><th>Module</th><th>Bronkolom</th><th>Oorspronkelijke kop</th><th>Eenheid</th></tr></thead><tbody>${preview.modules.map(module=>`<tr><td>${esc(module.module)}</td><td>${esc(module.column)}</td><td>${esc(sourceValue(module.header))}</td><td>${esc(module.unit||'onbekend')}</td></tr>`).join('')}</tbody></table></div>`:''}${rawSourceTable(preview.sample)}</details><details><summary>Kwaliteitscontrole</summary>${qualityView(preview.quality)}${qualityView(preview.warnings)}</details>${job.status==='preview'?lightingInterpretationForm():job.status==='completed'?`<p class="notice">Vastgelegde interpretatie: ${esc(interpretation.timezone||'onbekend')} · Excel ${esc(interpretation.date_system||'onbekend')} · ${fmt(interpretation.sample_seconds,' s')}.<br>Tijdstempel ${esc(({interval_start:'begin interval',interval_end:'einde interval',unknown:'onbekend'})[interpretation.timestamp_role]||'onbekend')} · ${interpretation.value_kind==='linear_dim_percent'?'gecontroleerd lineair dimpercentage':'percentage onbekend: geen DLI'}.</p><p class="hint">Een geverifieerde lichtsom vraagt daarnaast datumgeldige lampcapaciteit en een expliciete zelfstandige modulebijdrage. Het percentage is al in de brandduur verwerkt.</p>${user.role==='admin'?'<button type="button" id="lighting-calibrate" class="small">Lampconfiguratie vastleggen</button>':''}<section id="lighting-daily-report" class="section"></section>`:''}<div class="actions" style="margin-top:20px"><button id="lighting-refresh">↻ Status ophalen</button>${job.status==='failed'?'<button id="lighting-resume">Taak hervatten</button>':''}${!busy&&job.status!=='revoked'?'<button id="lighting-revoke" class="danger">Bronversie herroepen</button>':''}${['completed','revoked'].includes(job.status)?'<button id="lighting-new-version">Nieuwe interpretatieversie</button>':''}</div>`;
    $('#lighting-refresh',dialog).onclick=()=>refresh().catch(error=>toast(error.message));
    if($('#lighting-resume',dialog))$('#lighting-resume',dialog).onclick=()=>action('resume');
    if($('#lighting-revoke',dialog))$('#lighting-revoke',dialog).onclick=()=>{if(confirm('Deze aanvullende bronversie herroepen? De oorspronkelijke bron en het auditspoor blijven bewaard.'))action('revoke')};
    if($('#lighting-new-version',dialog))$('#lighting-new-version',dialog).onclick=async()=>{if(!confirm('Een nieuwe interpretatieversie maken met behoud van deze bronversie?'))return;try{const next=await api(`/api/legacy-lighting/from-history/${job.history_import_id}`,{method:'POST',body:{new_version:true}});if(active()){dialog.close();await lightingJobReview(next.id)}}catch(error){toast(error.message)}};
    if($('#lighting-interpretation-form',dialog)){
      $('#lighting-reason',dialog).required=true;$('#lighting-reason',dialog).minLength=3;
      bindForm('#lighting-interpretation-form',async data=>{job=await api(`/api/legacy-lighting/${id}/confirm`,{method:'POST',body:{confirmed:true,reason:data['lighting-reason'],interpretation:{timezone:data.timezone,date_system:data.date_system,sample_seconds:+data['lighting-sample-seconds'],unit:data.unit,timestamp_role:data.timestamp_role,value_kind:data.value_kind}}});if(active())draw()});
    }
    if($('#lighting-calibrate',dialog))$('#lighting-calibrate',dialog).onclick=()=>calibrationForm('legacy_excel');
    if(job.status==='completed')lightingDailyReport(job,dialog);
    applyPermissions();if(busy)timer=setTimeout(()=>refresh().catch(error=>{if(active())toast(error.message)}),2500);
  }
  draw();
}
function lightingInterpretationForm(){return `<form id="lighting-interpretation-form" class="section"><h3>Tijd en percentages expliciet controleren</h3><p class="notice warn">Onbekende tijd- of dimbetekenis mag als archief worden bewaard; zij levert geen DLI op. De bestandsnaam bepaalt nooit het gegevensjaar.</p><div class="form-grid"><div class="field"><label for="lighting-timezone">Gecontroleerde tijdzone</label><select id="lighting-timezone" name="timezone" required><option value="">Bevestig de tijdzone</option><option value="Europe/Amsterdam">Nederland · Europe/Amsterdam</option></select></div><div class="field"><label for="lighting-date-system">Gecontroleerd Excel-datumstelsel</label><select id="lighting-date-system" name="date_system" required><option value="">Bevestig het datumstelsel</option><option value="1900">Excel-datumstelsel 1900</option><option value="1904">Excel-datumstelsel 1904</option></select></div>${field('lighting-sample-seconds','Gecontroleerde tijdstap (seconden)','number','','required min="300" max="300" step="1"')}<div class="field"><label for="lighting-unit">Eenheid in de bron</label><select id="lighting-unit" name="unit" required><option value="">Bevestig de eenheid</option><option value="%">Percentage (%)</option></select></div><div class="field"><label for="lighting-timestamp-role">Betekenis van het tijdstempel</label><select id="lighting-timestamp-role" name="timestamp_role"><option value="unknown">Onbekend · alleen archiveren</option><option value="interval_start">Begin van het interval</option><option value="interval_end">Einde van het interval</option></select></div><div class="field"><label for="lighting-value-kind">Betekenis van het percentage</label><select id="lighting-value-kind" name="value_kind"><option value="unknown">Onbekend · geen lichtsom</option><option value="linear_dim_percent">Gecontroleerd lineair dimpercentage</option></select></div></div>${area('lighting-reason','Hoe zijn tijd en percentages gecontroleerd?')}<label class="inline field"><input type="checkbox" name="confirmed" required> Ik bevestig deze broninterpretatie. Excel blijft ondergeschikt aan Priva.</label>${formError()}<button type="submit" class="primary">Broninterpretatie bevestigen & verwerken</button></form>`;}
async function lightingDailyReport(job,dialog){
  const panel=$('#lighting-daily-report',dialog);if(!panel)return;
  panel.innerHTML=`<h3>Dagrapport & Priva-voorrang</h3><form id="lighting-daily-form"><div class="form-grid">${field('lighting-report-start','Van','date',job.preview?.start?.slice(0,10)||'','required')}${field('lighting-report-end','Tot en met','date',job.preview?.end?.slice(0,10)||'','required')}${field('lighting-report-modules','Modules (komma gescheiden)','text',(job.preview?.modules||[]).map(module=>module.module).join(','),'required')}</div>${formError()}<button type="submit">Dagrapport bekijken</button></form><div id="lighting-daily-result"></div>`;
  bindForm('#lighting-daily-form',async data=>{const params=new URLSearchParams({start:data['lighting-report-start'],end:data['lighting-report-end'],modules:data['lighting-report-modules']});const report=await api('/api/legacy-lighting/daily?'+params);if(panel.isConnected&&dialog.open){const target=$('#lighting-daily-result',dialog);function draw(offset=0){target.innerHTML=lightingDailyTable(report,offset);const previous=$('#lighting-report-previous',target),next=$('#lighting-report-next',target);if(previous)previous.onclick=()=>draw(Math.max(0,offset-200));if(next)next.onclick=()=>draw(offset+200)}draw()}});
}
function lightingDailyTable(report,offset=0){
  const allRows=Object.entries(report.days||{}).flatMap(([day,modules])=>Object.entries(modules).map(([module,value])=>({day,module,...value}))),rows=allRows.slice(offset,offset+200);
  return `${allRows.length>200?`<div class="actions"><button type="button" id="lighting-report-previous" class="small" ${offset?'':'disabled'}>← Vorige dagen</button><span class="hint">${offset+1}–${Math.min(offset+200,allRows.length)} van ${allRows.length} dag-/moduleregels</span><button type="button" id="lighting-report-next" class="small" ${offset+200>=allRows.length?'disabled':''}>Volgende dagen →</button></div>`:''}<p class="notice">Een aanwezige Priva-bron blokkeert Excel voor die dag en module, ook bij een lege of onbetrouwbare Priva-waarde. Een streep is onbekend, geen nulmeting.</p>${rows.length?`<div class="table-wrap"><table><thead><tr><th>Datum / module</th><th>Bronstatus</th><th>Dekking</th><th>Dimgewogen uren</th><th>Lichtsom</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${date(row.day)} · ${esc(row.module)}</td><td>${esc(({blocked_by_priva:'Priva gaat voor; Excel niet gebruikt',verified_supplemental:'Geverifieerde aanvullende Excelbron',unverified:'Niet geverifieerd'})[row.quality]||row.quality)}</td><td>${row.coverage==null?'Onbekend':fmt(row.coverage*100,'%')}</td><td>${fmt(row.full_power_hours,' h')}</td><td>${fmt(row.mol_m2_day,' mol/m²')}</td></tr>`).join('')}</tbody></table></div>`:empty('Geen dagregels voor deze selectie')}${qualityView(report.warnings)}<details><summary>Herkomst per dag en bronversie</summary>${rawSourceTable(rows.map(row=>({datum:row.day,module:row.module,bronversie:row.source_ids?.legacy_lighting_job_id,geschiedenis:row.source_ids?.history_import_id,broncellen:row.cell_refs,waarschuwingen:row.warnings})))}</details>`;
}

function sourceReferenceText(value){
  if(value===null||value===undefined)return '—';
  if(Array.isArray(value))return value.map(sourceReferenceText).join(', ');
  if(typeof value==='object')return Object.entries(value).map(([key,item])=>(({job_ids:'Excelbronversies',day_ids:'Excel-dagregels',history_import_ids:'Werkboeken',source_checksums:'Bronchecksums'})[key]||key)+': '+sourceReferenceText(item)).join(' · ');
  return String(value);
}
