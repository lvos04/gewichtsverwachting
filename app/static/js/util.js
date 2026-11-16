// Algemene, zuivere hulpfuncties voor de webinterface (geen toestand, geen API-aanroepen).
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
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Template-helper die standaard escapet: html`<p>${tekst}</p>` escapet `tekst`.
// Geneste html``-resultaten en arrays daarvan blijven HTML; vertrouwde HTML uit
// bestaande hulpfuncties (field(), tag(), ...) markeer je expliciet met raw().
class SafeHtml {
  constructor(value) {
    this.value = value;
  }
  toString() {
    return this.value;
  }
}
const raw = (value) => new SafeHtml(String(value ?? ""));
const renderValue = (value) =>
  value instanceof SafeHtml
    ? value.value
    : Array.isArray(value)
      ? value.map(renderValue).join("")
      : esc(value);
function html(strings, ...values) {
  return new SafeHtml(
    strings.reduce(
      (out, part, i) =>
        out + part + (i < values.length ? renderValue(values[i]) : ""),
      "",
    ),
  );
}

export {
  $,
  $$,
  esc,
  fmt,
  date,
  today,
  localNow,
  json,
  newClientId,
  html,
  raw,
  SafeHtml,
};
