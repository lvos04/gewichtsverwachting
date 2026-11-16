// Priva Bridge: beheerpaneel en bronweergave op de klimaatpagina.
// De browser praat nooit met de Bridge zelf; alles loopt via /api/bridge/* van deze app.
import { $, esc, fmt, date, html, raw } from "./js/util.js";

const QUALITY_LABELS = {
  ok: "ok",
  verdacht: "verdacht",
  onvolledig: "onvolledig",
  ontbreekt: "ontbreekt",
  onbekend: "onbekend",
  niet_opgehaald: "niet opgehaald",
};
const SOURCE_LABELS = {
  bridge: "Bridge",
  eigen_import: "Eigen import",
  ontbreekt: "Ontbreekt",
};
const dateTime = (v) =>
  v
    ? new Intl.DateTimeFormat("nl-NL", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Europe/Amsterdam",
      }).format(new Date(v))
    : "—";

function syncLine(s) {
  if (!s) return html`<span class="muted">nog niet</span>`;
  const label =
    s.status === "ok" ? "gelukt" : s.status === "bezig" ? "bezig…" : "mislukt";
  return html`${dateTime(s.started_at)} · ${s.trigger} · ${label}${s.period_from ? ` · ${date(s.period_from)} t/m ${date(s.period_to)}` : ""}${s.status === "ok" && s.trigger !== "test" ? ` · ${s.rows_received} regels ontvangen, ${s.rows_stored} nieuw/gewijzigd` : ""}${s.error ? raw(`<br><span class="error-text">${esc(s.error)}</span>`) : ""}`;
}

export async function bridgePanel({ container, api, toast, alive }) {
  if (!container) return;
  let s;
  try {
    s = await api("/api/bridge/status");
  } catch (error) {
    container.innerHTML = String(
      html`<h2>Priva Bridge</h2><p class="notice error">${error.message}</p>`,
    );
    return;
  }
  if (!alive() || !container.isConnected) return;
  const fresh = s.freshness || {};
  const opties = Object.entries(s.voorrang_opties || {});
  container.innerHTML = String(html`<h2>Klimaat- en lichtdata uit Priva Bridge</h2>
<p class="hint">Optioneel. Uit (standaard): klimaat en licht komen alleen uit de eigen Priva-CSV-import, zoals altijd. Aan: deze app haalt dagelijks (en met “Nu ophalen”) dagwaarden per kap en afdeling op bij de Priva Bridge op het interne netwerk. De eigen CSV-import blijft als terugval werken.</p>
<form id="bridge-form">
<label class="inline field"><input type="checkbox" name="enabled" ${raw(s.enabled ? "checked" : "")}> <strong>Klimaat- en lichtdata uit Priva Bridge</strong></label>
<div class="form-grid">
<div class="field"><label for="bridge_url">Bridge-URL (alleen intern netwerk)</label><input id="bridge_url" name="url" type="url" maxlength="300" placeholder="http://192.168.1.20:8000" value="${s.url || ""}"></div>
<div class="field"><label for="bridge_voorrang">Voorrang bij overlap met de eigen import</label><select id="bridge_voorrang" name="voorrang">${opties.map(([value, label]) => html`<option value="${value}" ${raw(value === s.voorrang ? "selected" : "")}>${label}</option>`)}</select></div>
<div class="field"><label for="bridge_sync_hour">Dagelijks ophalen vanaf (uur, Nederlandse tijd)</label><input id="bridge_sync_hour" name="sync_hour" type="number" min="0" max="23" step="1" required value="${s.sync_hour}"></div>
<div class="field"><label for="bridge_sync_days">Dagen terug bij dagelijks ophalen</label><input id="bridge_sync_days" name="sync_days" type="number" min="1" max="400" step="1" required value="${s.sync_days}"></div>
</div>
<details ${raw(s.ca_set ? "open" : "")}><summary>Eigen CA-certificaat (optioneel, voor https met een eigen certificaat)</summary><div class="field"><label for="bridge_ca">CA-certificaat (PEM, alleen het openbare certificaat)</label><textarea id="bridge_ca" name="ca_pem" placeholder="-----BEGIN CERTIFICATE-----">${s.ca_pem || ""}</textarea></div><p class="hint">${s.ca_set ? "Er is een eigen CA ingesteld. Maak het veld leeg om hem te verwijderen." : "Leeg = standaard vertrouwde certificaten."}</p></details>
<div class="error-text" role="alert"></div>
<button type="submit">Bridge-instellingen bewaren</button>
</form>
<form id="bridge-token-form" autocomplete="off">
<div class="form-grid"><div class="field"><label for="bridge_token">API-token (scope read:data)</label><input id="bridge_token" name="token" type="password" autocomplete="new-password" minlength="8" maxlength="4096" placeholder="${s.token_set ? "Ingesteld · vul alleen in om te vervangen" : "Plak het token uit de Bridge-beheer-UI"}" required></div></div>
<p class="hint">${s.token_set ? (s.token_readable ? `Token ingesteld op ${dateTime(s.token_set_at)}. Het wordt versleuteld bewaard en nooit teruggetoond.` : "Er is een token opgeslagen, maar het kan niet worden ontsleuteld (bijvoorbeeld na herstel op een andere machine). Voer het opnieuw in.") : "Nog geen token. Het wordt versleuteld bewaard en nooit teruggetoond."}</p>
<div class="error-text" role="alert"></div>
<div class="actions"><button type="submit">Token bewaren</button>${s.token_set ? raw('<button type="button" class="danger" id="bridge-token-delete">Token verwijderen</button>') : ""}</div>
</form>
<div class="actions"><button type="button" id="bridge-test">Verbinding testen</button><button type="button" id="bridge-sync" ${raw(s.enabled ? "" : "disabled")}>Nu ophalen</button></div>
<details><summary>Andere periode ophalen (historie)</summary><div class="form-grid"><div class="field"><label for="bridge_from">Van</label><input id="bridge_from" type="date"></div><div class="field"><label for="bridge_to">Tot en met</label><input id="bridge_to" type="date"></div></div><p class="hint">Leeg = de laatste ${s.sync_days} afgelopen dagen. Vandaag wordt nooit opgehaald; die dag is nog niet compleet.</p></details>
<div id="bridge-test-result"></div>
<div class="table-wrap"><table><tbody>
<tr><th>Laatste complete dag (Bridge)</th><td>${fresh.last_complete_date ? date(fresh.last_complete_date) : "—"}</td></tr>
<tr><th>Laatste import in de Bridge</th><td>${fresh.last_import_at ? dateTime(fresh.last_import_at) : "—"}</td></tr>
<tr><th>Laatste synchronisatie</th><td>${syncLine(s.last_sync)}</td></tr>
<tr><th>Laatste gelukte synchronisatie</th><td>${syncLine(s.last_success)}</td></tr>
<tr><th>Laatste verbindingstest</th><td>${syncLine(s.last_test)}</td></tr>
<tr><th>Dagwaarden in cache (alle revisies)</th><td>${fmt(s.cached_rows)}</td></tr>
</tbody></table></div>
${s.running ? html`<p class="notice">Synchronisatie loopt…</p>` : ""}`);

  const form = $("#bridge-form", container);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    const error = $(".error-text", form);
    error.textContent = "";
    try {
      await api("/api/bridge/config", {
        method: "PUT",
        body: {
          enabled: d.enabled === "on",
          url: d.url || "",
          ca_pem: d.ca_pem || "",
          voorrang: d.voorrang,
          sync_hour: +d.sync_hour,
          sync_days: +d.sync_days,
        },
      });
      toast("Bridge-instellingen opgeslagen als nieuwe instellingenversie");
      await bridgePanel({ container, api, toast, alive });
    } catch (e) {
      error.textContent = e.message;
    }
  });
  const tokenForm = $("#bridge-token-form", container);
  tokenForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $(".error-text", tokenForm);
    error.textContent = "";
    try {
      await api("/api/bridge/token", {
        method: "PUT",
        body: { token: $("#bridge_token", tokenForm).value },
      });
      toast("Token versleuteld opgeslagen");
      await bridgePanel({ container, api, toast, alive });
    } catch (e) {
      error.textContent = e.message;
    }
  });
  const remove = $("#bridge-token-delete", container);
  if (remove)
    remove.onclick = async () => {
      if (!confirm("Bridge-token verwijderen? Ophalen stopt tot je een nieuw token invoert.")) return;
      await api("/api/bridge/token", { method: "DELETE" });
      toast("Token verwijderd");
      await bridgePanel({ container, api, toast, alive });
    };
  $("#bridge-test", container).onclick = async (event) => {
    event.target.disabled = true;
    const out = $("#bridge-test-result", container);
    try {
      const r = await api("/api/bridge/test", { method: "POST" });
      out.innerHTML = String(
        r.ok
          ? html`<p class="notice">Verbinding gelukt. Laatste complete dag: ${r.last_complete_date ? date(r.last_complete_date) : "onbekend"} · bron: ${r.source_mode || "onbekend"} · ${r.missing_dates_count} ontbrekende dag(en) · ${r.warnings.length} waarschuwing(en).${r.missing_metrics?.length ? ` Ontbreekt in de catalogus: ${r.missing_metrics.join(", ")}.` : ""}</p>${r.warnings.map((w) => html`<p class="hint">${w.code}: ${w.detail}</p>`)}`
          : html`<p class="notice error">Verbinding mislukt: ${r.message}</p>`,
      );
    } catch (e) {
      out.innerHTML = String(html`<p class="notice error">${e.message}</p>`);
    }
    event.target.disabled = false;
  };
  $("#bridge-sync", container).onclick = async (event) => {
    event.target.disabled = true;
    const start = $("#bridge_from", container).value,
      end = $("#bridge_to", container).value;
    try {
      await api("/api/bridge/sync", {
        method: "POST",
        body: start || end ? { start: start || null, end: end || null } : {},
      });
      toast("Ophalen gestart");
      for (let i = 0; i < 120 && alive(); i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const status = await api("/api/bridge/status");
        if (!status.running) break;
      }
    } catch (e) {
      toast(e.message);
    }
    if (alive()) await bridgePanel({ container, api, toast, alive });
  };
}

export function bridgeClimate(c) {
  const b = c.bridge;
  if (!b) return "";
  const fresh = b.freshness || {};
  const cell = (day, metric, feature) => {
    const m = day.metrics?.[metric];
    const src = feature ? day.sources?.[feature] : null;
    return html`<td>${m && m.value !== null && m.value !== undefined ? fmt(m.value) : "—"}${m ? html`<br><small class="muted">${QUALITY_LABELS[m.quality] || m.quality}${m.coverage !== null && m.coverage !== undefined ? ` · dekking ${Math.round(m.coverage * 100)}%` : ""}${src ? ` · telt: ${SOURCE_LABELS[src] || src}` : ""}</small>` : ""}</td>`;
  };
  const hours = (day, metric) => {
    const m = day.metrics?.[metric];
    return m && m.value !== null && m.value !== undefined ? fmt(m.value / 3600, " u") : "—";
  };
  const counts = b.source_counts || {};
  return String(html`<section class="card"><h2>Priva Bridge</h2>
<p class="hint">Laatste complete dag: <strong>${fresh.last_complete_date ? date(fresh.last_complete_date) : "onbekend"}</strong> · laatste import in de Bridge: ${fresh.last_import_at ? dateTime(fresh.last_import_at) : "—"} · laatst opgehaald: ${b.last_sync_at ? dateTime(b.last_sync_at) : "nog niet"}</p>
<p class="hint">Voorrang: ${b.voorrang_label}. Periode: ${counts.bridge || 0} kenmerk-dagen uit de Bridge, ${counts.eigen_import || 0} uit de eigen import, ${counts.ontbreekt || 0} ontbrekend. Een ontbrekende of onvolledige dag telt nooit als 0.</p>
${b.truncated ? html`<p class="notice warn">Te veel regels; de weergave is afgekapt.</p>` : ""}
<div class="table-wrap"><table><thead><tr><th>Dag</th><th>Scope</th><th>Lichtsom lampen (mol/m²)</th><th>Etmaaltemp. (°C)</th><th>RV (%)</th><th>Lampuren</th><th>Schermuren</th></tr></thead><tbody>${(b.days || []).map((day) => html`<tr><td>${date(day.date)}</td><td>${day.scope}</td>${cell(day, "lichtsom_lampen_kap", "lamp_light")}${cell(day, "temp_etmaal_avg", "temperature")}${cell(day, "rv_avg", "rh")}<td>${hours(day, "lamp_brandseconden")}</td><td>${hours(day, "scherm_seconden")}</td></tr>`)}</tbody></table></div>
<p class="hint">“Telt” is de bron die de berekening voor die dag gebruikt. Lamp- en schermuren zijn de brandseconden zoals de Bridge ze levert, omgerekend naar uren.</p></section>`);
}
