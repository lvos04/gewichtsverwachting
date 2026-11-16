const $ = (s, r = document) => r.querySelector(s),
  $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const fmt = (v, unit = "") =>
  v === null || v === undefined
    ? "—"
    : `${Number(v).toLocaleString("nl-NL", { maximumFractionDigits: 1 })}${unit}`;
const date = (v) =>
  v
    ? new Intl.DateTimeFormat("nl-NL", {
        dateStyle: "medium",
        timeZone: "Europe/Amsterdam",
      }).format(
        new Date(
          String(v).includes("T") && !/Z$|[+-]\d\d:\d\d$/.test(v) ? v + "Z" : v,
        ),
      )
    : "—";
const today = () =>
  new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Amsterdam" });
const localNow = () =>
  new Date()
    .toLocaleString("sv-SE", { timeZone: "Europe/Amsterdam" })
    .replace(" ", "T")
    .slice(0, 16);
const json = (v) => `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`;
// getRandomValues remains available on a trusted LAN over HTTP, where
// randomUUID and service workers can require HTTPS. Keep UUIDv4 retry IDs valid.
function newClientId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

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
  const owner=user?.id;
  const out = await Promise.allSettled([
    api("/api/cultivations"),
    api("/api/varieties"),
    api("/api/bays"),
    api("/api/settings"),
  ]);
  if(!user || user.id!==owner) return;
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
  try { await api("/api/auth/logout", { method: "POST" }); }
  catch (error) { toast('Afmelden is niet gelukt: '+error.message); return; }
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
  const generation=busyNavigation, owner=user?.id;
  return () => !!user && user.id===owner && busyNavigation===generation && currentPage===page;
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
  const alive=pageOwner("dashboard");
  if(!alive()) return;
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
  if(!alive()) return;
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
    `<p class="sub">Registreer elke soort in een gemengde kap als een eigen kapteelt.</p><form id="cult-form"><div class="form-grid">${field("round_name", "Ronde", "text", "", 'required placeholder="Bijv. Winter 2026–2027"')}<div class="field"><label for="bay_number">Kap</label><select name="bay_number" id="bay_number" required><option value="">Kies een kap</option>${Array.from({ length: 40 }, (_, i) => `<option value="${i + 1}" ${i + 1 === bay ? "selected" : ""}>Kap ${i + 1}</option>`).join("")}</select></div><div class="field"><label for="variety_id">Soort</label><select name="variety_id" id="variety_id" required>${options(varieties)}</select></div>${field("beds", "Aantal bedden", "number", 6, 'min="1" max="6" step="1" required')}<div class="field"><label for="category">Teeltcategorie</label><select name="category" id="category"><option value="standard">Standaard</option><option value="adjusted">Bijgestuurd</option><option value="control">Controle</option></select></div><div class="field"><label for="status">Status</label><select name="status" id="status"><option value="active">Lopend</option><option value="planned">Gepland</option></select></div>${field("plant_date", "Plantdatum", "date", "", "required")}${field("light_out_date", "Licht uit", "date", "", "required")}${field("expected_harvest_date", "Verwachte oogst", "date", "", "required")}${field("density", "Plantdichtheid (takken/m²)", "number", "", 'min="0.1" max="500" step="0.1" required')}</div>${formError()}<button class="primary" type="submit">Kapteelt opslaan</button></form>`,
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
  const alive=pageOwner("cultivation");
  if(!alive()) return;
  const c = await api(`/api/cultivations/${Number(id)}`);
  if(!alive()) return;
  let f;
  try {
    f = await api(`/api/cultivations/${c.id}/forecast`);
  } catch (e) {
    f = { status: "unknown", warnings: [e.message] };
  }
  if(!alive()) return;
  const p = c.planning?.[c.planning.length - 1] || {};
  $("#main").innerHTML =
    pageHead(
      `Kap ${c.bay_number} · ${esc(c.variety_name || varieties.find((v) => v.id === c.variety_id)?.name || "")}`,
      `${esc(c.round_name)} · ${esc(c.beds)} bedden · ${statusLabel(c.category)}`,
      `<a class="btn" href="/api/cards?ids=${c.id}" target="_blank" rel="noopener">▤ Teeltkaart</a><a class="btn primary" href="#measure/${c.id}">＋ Meting</a>`,
    ) +
    `<div class="two"><div><section class="card"><div class="section-title"><h2>Verwachting & seizoen</h2>${tag(statusLabel(f.status), f.status === "operational" ? "" : "warn")}</div>${forecastView(f)}</section><section class="card"><div class="section-title"><h2>Planning</h2><button id="edit-plan" class="small">Nieuwe versie</button></div>${
      c.planning?.length
        ? `<div class="form-grid"><div><label>Geplant</label><p>${date(p.plant_date)}</p></div><div><label>Licht uit</label><p>${date(p.light_out_date)}</p></div><div><label>Verwachte oogst</label><p>${date(p.expected_harvest_date)}</p></div><div><label>Plantdichtheid</label><p>${fmt(p.density, " /m²")}</p></div></div><details><summary>${c.planning.length} planningversie(s)</summary>${c.planning
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
    }</section><section class="card"><h2>Metingen</h2>${measurementHistory(c.measurements || [])}</section></div><div><section class="card"><h2>Registreren</h2><div class="actions"><button id="event-btn">＋ Gebeurtenis</button><button id="harvest-btn">＋ Oogst</button><button id="cultivation-state">Teeltstatus</button></div><p class="hint" style="margin-top:12px">Leg afwijkingen, remstof, licht en teeltinstellingen met hun werkelijke datum vast.</p></section><section class="card"><h2>Teelttijdlijn</h2><div class="timeline">${
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
}
function forecastView(f) {
  const season = f.season;
  return `<div class="summary"><div><small>Verwacht eindgewicht</small><strong>${fmt(f.point_g, " g")}</strong></div><div><small>80%-voorspelinterval</small><strong style="font-size:18px">${f.low_g != null && f.high_g != null ? `${fmt(f.low_g)} – ${fmt(f.high_g)} g` : "Onbekend"}</strong></div><div><small>Soortdoel</small><strong style="font-size:18px">${fmt(f.target_g, " g")}</strong></div></div>${f.point_g == null ? '<p class="hint">Nog geen betrouwbare gewichtsverwachting beschikbaar. Registreer metingen en complete oogsten.</p>' : ""}${season ? `<div class="notice"><strong>${esc({ longer: "De dagen worden langer", shorter: "De dagen worden korter", turning: "Rond het keerpunt van de daglengte" }[season.trend] || "Seizoensverloop")}</strong><br>Astronomische daglengte: ${fmt(season.daylength_hours, " uur")} ${season.trend_hours_per_day != null ? "· verandering " + fmt(season.trend_hours_per_day * 60, " min/dag") : ""}<p class="hint" style="margin-bottom:0">Daglengte is een seizoensindicatie. De werkelijke lichtsom in de kas hangt ook af van weer, schermen en lampen.</p>${season.expected_remainder ? `<details><summary>Resterend seizoen: donker / gemiddeld / licht</summary>${typeof season.expected_remainder === "string" ? esc(season.expected_remainder) : seasonRemainder(season.expected_remainder)}</details>` : ""}</div>` : ""}${f.actual_kd_day != null ? `<p class="notice ${f.can_steer ? "" : "warn"}">Nu KD-dag ${esc(f.actual_kd_day)} · ingesteld bijstuurvenster tot KD${esc(f.steering_until_kd ?? appSettings.steering_until_kd)}. ${f.can_steer ? "Binnen het bijstuurvenster." : "Buiten het bijstuurvenster."} Alleen menselijke beoordeling; gebeurtenissen worden handmatig vastgelegd.</p>` : ""}${(f.warnings || []).map((w) => `<p class="notice warn">${esc(typeof w === "string" ? w : JSON.stringify(w))}</p>`).join("")}${f.as_of ? `<p class="hint">Invoerpeildatum: ${date(f.as_of)}</p>` : ""}`;
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
    `<p class="hint">Eerdere versies en oorspronkelijke meetmomenten blijven behouden.</p><form id="plan-form"><div class="form-grid">${field("plant_date", "Plantdatum", "date", p.plant_date || "", "required")}${field("light_out_date", "Licht uit", "date", p.light_out_date || "", "required")}${field("expected_harvest_date", "Verwachte oogst", "date", p.expected_harvest_date || "", "required")}${field("density", "Plantdichtheid (takken/m²)", "number", p.density || "", 'required min="0.1" step="0.1"')}</div>${area("reason", "Reden voor wijziging")}${formError()}<button class="primary" type="submit">Nieuwe versie bewaren</button></form>`,
  );
  bindForm("#plan-form", async (d) => {
    await api(`/api/cultivations/${id}/planning`, {
      method: "POST",
      body: { ...d, density: +d.density },
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
  if(!el.isConnected) return;
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
  const alive=pageOwner("measure");
  if(!alive()) return;
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
  if(!alive() || !form.isConnected) return;
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
    if(!alive() || !form.isConnected) return;
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
  if(!alive() || !form.isConnected) return;
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
    if(!alive() || !form.isConnected) return;
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
  if(!alive() || !form.isConnected) return;
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
  const alive=pageOwner("imports");
  if(!alive()) return;
  clearInterval(poll);
  const jobs = list(await api("/api/imports"));
  if(!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Priva-gegevens importeren",
      "Van CSV of ZIP naar controleerbare historie. Bekijk eerst het voorbeeld, bevestig daarna.",
    ) +
    `<section class="card"><form id="import-form"><div class="upload"><div style="font-size:27px;color:var(--green);margin-bottom:8px" aria-hidden="true">↥</div><h2>Historie toevoegen</h2><p class="sub" style="font-size:13px">Selecteer één of meer CSV-bestanden, of een ZIP met mappen.</p><label for="files" class="hint">Bestanden kiezen</label><input id="files" name="files" type="file" accept=".csv,.zip" multiple required></div><p class="hint">Werkelijke rijdatums zijn leidend. Een weeknummer of bestandsnaam bepaalt nooit het gegevensjaar.</p>${formError()}<button type="submit" class="primary">Uploaden & voorbeeld maken</button></form></section><section class="card"><div class="section-title"><h2>Importgeschiedenis</h2><button id="refresh-imports" class="small">↻ Vernieuwen</button></div><div id="import-list">${jobs.length ? jobs.map(importRow).join("") : empty("Nog geen imports", "Je oorspronkelijke bestanden en de herkomst blijven bewaard.")}</div></section>`;
  $("#refresh-imports").onclick = route;
  bindForm("#import-form", async (_, form) => {
    const fd = new FormData();
    for (const f of $("#files").files) fd.append("files", f);
    const job = await api("/api/imports", { method: "POST", body: fd });
    toast("Bestanden ontvangen. Het voorbeeld wordt voorbereid.");
    await imports();
    await showImport(job.id);
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
  dialog.addEventListener('close', () => clearTimeout(jobTimer), {once:true});
  async function draw() {
    clearTimeout(jobTimer);
    if (!$("#import-detail") || !dialog.open) return;
    $('.modal-head h2', dialog).textContent=`Import #${id} · ${statusLabel(j.status)}`;
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
      try { j = await api(`/api/imports/${id}`); await draw(); }
      catch(error) { toast(error.message); }
    };
    applyPermissions();
    if(['queued','previewing','importing'].includes(j.status)) jobTimer=setTimeout(async()=>{
      try {j=await api(`/api/imports/${id}`);await draw();}
      catch(error) {toast('Importstatus niet bereikbaar: '+error.message);}
    },2000);
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
  const alive=pageOwner("climate");
  if(!alive()) return;
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
    if(!alive() || !$("#climate-result")) return;
    $("#climate-result").innerHTML =
      `<div class="grid"><div class="card metric"><div class="label">Lichtsom lampen</div><div class="value">${fmt(c.derived?.lamp_dli)}</div><div class="hint">mol/m² · geselecteerde periode</div></div><div class="card metric"><div class="label">Lichtsom daglicht</div><div class="value">${fmt(c.derived?.daylight_dli)}</div><div class="hint">mol/m² · geselecteerde periode</div></div><div class="card metric"><div class="label">Totale lichtsom</div><div class="value">${fmt(c.derived?.total_dli)}</div><div class="hint">Alleen bij geverifieerde invoer</div></div></div><section class="card section"><h2>Datakwaliteit</h2>${qualityView(c.quality)}${qualityView(c.derived?.reasons)}${c.mapping_uncertain ? '<p class="notice warn">Een of meer historische koppelingen zijn onzeker. Controleer de ingangsdatums in Beheer.</p>' : ""}${c.conflicts?.length ? `<details><summary>Bronconflicten (${c.conflicts.length})</summary>${json(c.conflicts)}</details>` : ""}</section>${c.derived?.daily?.length ? `<section class="card"><h2>Lichtsom per dag</h2><p class="hint">Alleen volledige dagen met bevestigde broninstellingen krijgen een lichtsom. Een streep betekent onbekend.</p><div class="table-wrap"><table><thead><tr><th>Dag</th><th>Lampen</th><th>Daglicht</th><th>Totale DLI</th><th>Kwaliteit</th></tr></thead><tbody>${c.derived.daily.map((day) => `<tr><td>${date(day.date)}</td><td>${fmt(day.lamp_dli)}</td><td>${fmt(day.daylight_dli)}</td><td>${fmt(day.total_dli)}</td><td>${esc(typeof day.quality === "object" ? JSON.stringify(day.quality) : day.quality)}</td></tr>`).join("")}</tbody></table></div><p class="hint">DLI in mol/m² per dag.</p></section>` : ""}<section class="card"><h2>Meetreeksen (${c.series?.length || 0})</h2>${c.series?.length ? c.series.map((s) => `<details><summary>${esc(s.label)} · ${esc(s.object_type)} ${esc(s.object_number)} ${s.channel ? "· kanaal " + esc(s.channel) : ""}</summary><p class="hint">Eenheid: ${esc(s.unit || "onbevestigd")} · ${s.dates?.length || 0} dag(en)</p><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Waarde</th><th>Aantal</th><th>Kwaliteit</th></tr></thead><tbody>${(s.dates || []).map((v) => `<tr><td>${date(v.date)}</td><td>${fmt(v.value)}</td><td>${fmt(v.count)}</td><td>${esc(typeof v.quality === "object" ? JSON.stringify(v.quality) : v.quality)}</td></tr>`).join("")}</tbody></table></div></details>`).join("") : empty("Nog geen meetreeksen in deze periode", "Importeer Priva-bestanden of selecteer een andere periode.")}</section><div class="notice">De lengte van de dag verandert met het seizoen. Dat is iets anders dan gemeten straling in de kas. Regen, bewolking, schermen en lampen bepalen de ontvangen lichtsom.</div>`;
  }
}
function modelMetrics(metrics) {
  const model=metrics.model||{}, baseline=metrics.baseline||{};
  return `<div class="table-wrap"><table><tbody><tr><th>MAE model</th><td>${fmt(model.mae_g,' g')}</td></tr><tr><th>MAE referentie (dezelfde teelten)</th><td>${fmt(baseline.mae_g,' g')}</td></tr><tr><th>Bias model</th><td>${fmt(model.bias_g,' g')}</td></tr><tr><th>Dekking 80%-interval</th><td>${model.interval_80_coverage!=null?fmt(model.interval_80_coverage*100,'%'):'Onbekend'}</td></tr><tr><th>Geëvalueerde latere rondes</th><td>${fmt(metrics.unseen_rounds)}</td></tr><tr><th>Vrijgavebesluit</th><td>${metrics.release_gate==='passed'?'Criteria gehaald':'Experimenteel'}</td></tr></tbody></table></div>`;
}
async function models() {
  const alive=pageOwner("models");
  if(!alive()) return;
  const data = await api("/api/models"),
    rows = list(data),
    scenarios = list(await api("/api/scenarios"));
  if(!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Gewichtsverwachting",
      "Een eerlijke prognose begint met complete gegevens en onafhankelijke toetsing.",
      user.role === "admin"
        ? '<button class="primary" id="train-model">Nieuw model toetsen</button>'
        : "",
    ) +
    `<div class="notice"><strong>Vrijgave is een meetbaar besluit.</strong> Minimaal ${esc(appSettings.min_training_cultivations ?? 30)} complete standaardteelten, toetsing op ${esc(appSettings.required_unseen_rounds ?? 2)} latere volledig ongeziene rondes en verbetering tegenover dezelfde historische referentie. De genoemde 3,8 g is een doelreferentie, geen gemeten prestatie van deze app.</div><div class="two"><section class="card"><h2>Prognose per kapteelt</h2><form id="forecast-form">${selectCult()}${field("as_of", "Invoerpeildatum (optioneel)", "datetime-local")}${formError()}<button class="primary" type="submit">Verwachting bekijken</button></form><div id="forecast-result"></div></section><section class="card"><h2>Modelhistorie</h2>${rows.length ? rows.map((m) => `<article style="padding:15px 0;border-bottom:1px solid var(--line)"><h3>${esc(m.kind || m.model_kind || "Model")} #${esc(m.id)} ${tag(statusLabel(m.active ? "operational" : "experimental"), m.active ? "" : "warn")}</h3><p class="hint">${date(m.created_at)} · ${esc(m.data_hash || m.training_data_hash || "")}</p>${m.metrics ? modelMetrics(m.metrics) : ""}${m.release_reasons ? qualityView(m.release_reasons) : ""}<details><summary>Validatie en kenmerken</summary>${json(m)}</details>${user.role === "admin" ? `<button class="small" data-activate="${m.id}" ${m.operational_eligible !== true ? 'disabled title="Voldoet nog niet aan vrijgavecriteria"' : ""}>Operationeel activeren</button>` : ""}</article>`).join("") : empty("Nog geen getraind model", "Zonder betrouwbare oogstlabels blijft de verwachting onbekend.")}</section></div><section class="card"><div class="section-title"><h2>Handmatige toekomstscenario’s</h2><button id="new-scenario" class="small">＋ Scenario</button></div><p class="hint">Buitenweer en teeltinstellingen voor de nog onbekende periode. Handmatige invoer is geen waargenomen klimaat. Externe provider staat uit.</p>${scenarios.length ? scenarios.map((s) => `<details><summary>${esc(s.name || s.label || s.scenario || "Scenario")} · geldig ${date(s.valid_at)}</summary>${json(s)}</details>`).join("") : empty("Nog geen scenario’s", "Leg eventueel een donker, gemiddeld of licht toekomstscenario vast.")}</section>`;
  bindForm("#forecast-form", async (d) => {
    const f = await api(
      `/api/cultivations/${d.cultivation_id}/forecast${d.as_of ? "?as_of=" + encodeURIComponent(d.as_of) : ""}`,
    );
    if(!alive() || !$("#forecast-result")) return;
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
        modal("Resultaat modeltoetsing", result.models.map(m=>m.status==="insufficient_data"?`<p class="notice warn"><strong>${esc(m.kind)}</strong>: ${esc(m.complete_standard_cultivations)} complete standaardteelten, minimaal ${esc(m.required)} vereist. Nog niet getraind.</p>`:`<h3>${esc(m.kind)}</h3>${modelMetrics(m.metrics||{})}`).join(""));
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
  const alive=pageOwner("manage");
  if(!alive()) return;
  const mappings = list(await api("/api/mappings"));
  if(!alive()) return;
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
  if(!alive()) return;
  $("#main").innerHTML =
    pageHead(
      "Bedrijfsinstellingen",
      "Soorten, koppelingen en historie. Wijzigingen blijven traceerbaar.",
    ) +
    `${user.role === "admin" ? '<section class="card" id="general-settings"><p class="hint">Algemene instellingen laden…</p></section>' : ""}<div class="two"><div><section class="card"><div class="section-title"><h2>Soorten & doelgewicht</h2><button id="new-variety" class="small">＋ Soort</button></div><div class="table-wrap"><table><thead><tr><th>Soort</th><th>Doelgewicht</th><th></th></tr></thead><tbody>${varieties.map((v) => `<tr><td>${esc(v.name)}</td><td>${fmt(v.target_weight_g, " g")}</td><td>${user.role === "admin" ? `<button class="small" data-variety="${v.id}">Wijzigen</button>` : ""}</td></tr>`).join("")}</tbody></table></div></section><section class="card"><div class="section-title"><h2>Koppelingen met ingangsdatum</h2><button id="new-mapping" class="small">＋ Koppeling</button></div><p class="hint">Oude situaties blijven historisch intact. Kranen 41–45 hebben geen bevestigde kaptoewijzing.</p><div class="field"><label for="mapping-filter">Kap</label><select id="mapping-filter">${Array.from({ length: 40 }, (_, i) => `<option value="${i + 1}">Kap ${i + 1}</option>`).join("")}</select></div><div id="mapping-list"></div></section><section class="card"><h2>Oppervlakte per kap</h2><p class="hint">Een onbekende oppervlakte blijft leeg; zij wordt niet geschat.</p><form id="bay-settings"><div class="form-grid"><div class="field"><label for="bay-edit">Kap</label><select id="bay-edit" name="number">${options(bays, "number", (b) => "Kap " + b.number)}</select></div>${field("area_m2", "Oppervlakte (m²)", "number", bays[0]?.area_m2 || "", 'min="0.1" step="0.1"')}</div>${formError()}<button type="submit">Oppervlakte bewaren</button></form></section></div><div>${user.role === "admin" ? `<section class="card"><div class="section-title"><h2>Gebruikers</h2><button id="new-user" class="small">＋ Account</button></div>${users.map((u) => `<div style="display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--line)"><span><strong>${esc(u.username)}</strong><br><small class="muted">${esc(u.role)} · ${u.active === false ? "inactief" : "actief"}</small></span><button class="small" data-user="${u.id}">Beheren</button></div>`).join("")}</section><section class="card"><h2>Export & back-up</h2><p class="hint">Exporteer de bedrijfsregistratie of maak een consistente serverback-up inclusief bestanden.</p><div class="actions"><a class="btn" href="/api/export">↓ Volledige export</a><button id="backup-now">＋ Back-up maken</button></div><div id="backup-result"></div>${backups.length ? `<details><summary>Bewaarde back-ups (${backups.length})</summary>${backups.map((b) => `<p class="hint"><a href="/api/backups/${encodeURIComponent(b.filename)}">${esc(b.filename)}</a> · ${fmt(b.size / 1024, " kB")} · ${date(b.created_at)}</p>`).join("")}</details>` : ""}</section>` : '<section class="notice">Alleen beheerders kunnen gebruikers, oppervlakte, kalibraties en back-ups wijzigen.</section>'}</div></div>${
      user.role === "admin"
        ? `<section class="card"><div class="section-title"><h2>Gevalideerde eenheden & lichtinstellingen</h2><button id="new-calibration">＋ Validatie vastleggen</button></div><p class="hint">Leg alleen bevestigde bronbetekenissen en instellingen vast. Zonder bevestigde eenheid, lampkanaal of scherminvloed blijft de DLI ongeverifieerd.</p>${calibrations.length ? calibrations.map((c) => `<details><summary>${esc(c.kind)} · ${esc(c.label || "")} · ${esc(c.object_type || "")} ${esc(c.object_number || "")} · vanaf ${date(c.valid_from)}</summary>${json(c)}</details>`).join("") : empty("Nog geen bronvalidaties", "De actuele installatieconfiguratie wordt niet naar historische jaren teruggeprojecteerd.")}<details><summary>Laatst geregistreerde wijzigingen</summary>${
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
  if (user.role === "admin") await settingsPanel();
  if(!alive()) return;
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
          `<form id="target-form">${field("target_weight_g", "Doelgewicht per tak (g)", "number", v.target_weight_g, 'required min="0.1" max="1000" step="0.1"')}${formError()}<button class="primary" type="submit">Bewaren</button></form>`,
        );
        bindForm("#target-form", async (d) => {
          await api(`/api/varieties/${v.id}`, {
            method: "PATCH",
            body: { target_weight_g: +d.target_weight_g },
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
    ],
  ],
];
async function settingsPanel() {
  const alive=pageOwner("manage");
  const panel = $("#general-settings");
  if (!panel) return;
  try {
    const result = await api("/api/settings");
    if(!alive() || !panel.isConnected) return;
    appSettings = result.settings;
    panel.innerHTML = `<h2>Algemene instellingen</h2><p class="hint">Elke wijziging krijgt een ingangsdatum en blijft in de historie bewaard. De ondergrenzen voor modelvrijgave kunnen niet worden verlaagd.</p><form id="settings-form">${SETTINGS_GROUPS.map(([title, fields]) => `<details ${title === "Teelt & seizoen" ? "open" : ""}><summary>${title}</summary><div class="form-grid" style="margin-top:16px">${fields.map(([key, label, min, max, step]) => field(key, label, "number", appSettings[key], `required min="${min}" max="${max}" step="${step}"`)).join("")}</div>${title === "Importbronprioriteiten" ? '<p class="hint">Bij conflicten wint de hogere prioriteit van een geldige bron. Bij gelijke prioriteit geldt de meest recent bevestigde bron. Ongeldige of ontbrekende waarden winnen niet van een geldige waarde.</p>' : ""}${title === "Automatische back-ups" ? '<p class="hint">0 bewaart back-ups onbeperkt. Bij een bewaartermijn worden oudere exemplaren pas na een geslaagde automatische back-up opgeruimd; de laatste blijft behouden.</p>' : ""}</details>`).join("")}${field("settings_effective_from", "Ingangsmoment (leeg = direct; Nederlandse tijd)", "datetime-local")}${area("settings_reason", "Reden voor wijziging")}${formError()}<button type="submit" class="primary">Instellingen als nieuwe versie bewaren</button></form><details><summary>Versiehistorie (${result.history?.length || 0})</summary>${result.history?.length ? result.history.map((v) => `<details><summary>Versie ${esc(v.id)} · vanaf ${date(v.effective_from)}</summary><p class="hint">${esc(v.reason)}</p><div class="table-wrap"><table><tbody>${SETTINGS_GROUPS.flatMap(([, fields]) => fields.map(([key, label]) => `<tr><td>${label}</td><td>${fmt(v.settings[key])}</td></tr>`)).join("")}</tbody></table></div></details>`).join("") : '<p class="hint">De zichtbare beginwaarden zijn nog niet als nieuwe versie gewijzigd.</p>'}</details><p class="hint">${esc(result.infrastructure)}</p>`;
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
          settings,
          effective_from: d.settings_effective_from || null,
          reason: d.settings_reason,
        },
      });
      toast("Instellingen als nieuwe versie opgeslagen");
      await settingsPanel();
    });
  } catch (e) {
    if(!alive() || !panel.isConnected) return;
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
function calibrationForm() {
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
          ? `<div class="form-grid">${field("ppfd", "Werkelijk PPFD (µmol/m²/s)", "number", "", 'required min="0.1" step="0.1"')}${field("dim_factor", "Dimfactor (0–1)", "number", "", 'required min="0" max="1" step="0.01"')}${field("hours_label", "Exact label brandduur", "text", "", "required")}${field("time_unit", "Eenheid brandduur", "text", "", 'required placeholder="s of h"')}</div><label class="inline field"><input type="checkbox" name="independent_contribution" required> Deze module en dit kanaal zijn een zelfstandige bijdrage en worden niet dubbel geteld.</label>`
          : `${field("factor", "Transmissiefactor (0–1)", "number", "", 'required min="0" max="1" step="0.01"')}<label class="inline field"><input type="checkbox" name="no_screen_effect"> Gecontroleerd dat er in deze periode geen schermverlies was.</label><p class="hint">Zonder deze bevestiging blijft daglicht-DLI geblokkeerd zolang een onderbouwde tijdreeks van schermstanden ontbreekt.</p>`;
  }
  $("#cal-kind").onchange = calFields;
  $("#reason").required = true;
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
        object_type: d.object_type || null,
        object_number: d.object_number ? +d.object_number : null,
        label: d.label || null,
        channel: d.channel || null,
        value,
        reason: d.reason,
        confirmed: true,
      },
    });
    $("#modal").close();
    manage();
  });
}
async function cards() {
  const alive=pageOwner("cards");
  if(!alive()) return;
  const scans = list(await api("/api/scans"));
  if(!alive()) return;
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
      "revoke-import",
      "resume-import",
    ].forEach((id) =>
      disable($("#" + id), "Je account heeft alleen leesrechten."),
    );
    ["measure-form", "scan-form", "import-form", "review-form"].forEach(
      (id) => {
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
      },
    );
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
