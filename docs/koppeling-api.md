# Bedrijfskoppeling API v1

Alleen-lezen koppeling waarmee het *Bedrijfsdashboard* (en andere bedrijfsapps) server-naar-server een vaste set
gegevens uit **Gewichtsverwachting chrysant** ophaalt. Deze app volgt het gedeelde contract voor alle bedrijfsapps;
dit document beschrijft het contract plus de app-specifieke KPI's en datasets.

* App-slug: `gewichtsverwachting`
* Pad-prefix: `/api/koppeling/v1` (los van de interne `/api/...`-routes van de webinterface)
* Alleen `GET`, JSON in UTF-8. Tijden ISO 8601 in UTC met `Z`; datums `YYYY-MM-DD`.
* Ontbrekende waarde = `null`, nooit een verzonnen 0.

## Belangrijk: geen verzonnen gewichten

De app presenteert een doelgewicht (bijv. 105 g) nooit als voorspelling en toont geen gewichtsuitkomst zonder
gevalideerd model. De koppeling volgt dat strikt:

* Een prognosegewicht wordt alleen gedeeld als de laatst berekende prognose uit een **operationeel vrijgegeven**
  model komt (status `gevalideerd`).
* Experimentele modellen, alleen een historisch gemiddelde, of geen model → `gewicht_g`, `ondergrens_g` en
  `bovengrens_g` zijn `null`, status `onbekend`, met een `toelichting`.
* Doelgewichten worden in geen enkele dataset of KPI gedeeld.
* Gemeten takgewichten (KD-metingen) en geregistreerde oogstgewichten zijn waarnemingen en worden wel gedeeld,
  duidelijk als meting/oogst benoemd.

## In- en uitschakelen (standaard uit)

De koppeling staat na installatie of upgrade **uit**. Een beheerder zet hem aan met de schakelaar
**Beheer → Bedrijfskoppeling (alleen-lezen API) → Bedrijfskoppeling ingeschakeld**. De keuze wordt als nieuwe,
geauditeerde instellingenversie bewaard (`koppeling_enabled`, auditactie `settings_changed`).

* Uit → **alle** routes onder `/api/koppeling/v1/*` geven `403 {"fout": "koppeling_uitgeschakeld"}`, ook met een
  geldige sleutel (en ook zonder sleutel). Sleutelbeheer blijft beschikbaar, zodat sleutels alvast kunnen worden
  aangemaakt of ingetrokken.
* Aan → het gedrag hieronder.

Beheer-API (sessie-auth, alleen beheerders): `GET /api/koppeling/status` → `{"ingeschakeld": false}`,
`PUT /api/koppeling/status` met `{"ingeschakeld": true}`.

## Authenticatie: koppelsleutels

* Header `Authorization: Bearer <sleutel>`. Zonder sleutel, met een onbekende of ingetrokken sleutel:
  `401 {"fout": "ongeldige_koppelsleutel"}` (met `WWW-Authenticate: Bearer`).
* Sessiecookies geven géén toegang tot deze routes en een koppelsleutel geeft géén toegang tot de webinterface.
* Sleutelformaat: `bk_` + 32 bytes cryptografisch willekeurig (urlsafe base64). De app slaat alleen de
  **SHA-256-hash** op plus de eerste 8 tekens als herkenbaar `voorvoegsel`. Opzoeken gebeurt via de hash, met een
  vergelijking in constante tijd.
* Opgeslagen per sleutel (tabel `koppelsleutels`, migratie `0004`): id, naam, voorvoegsel, hash, aangemaakt_op,
  aangemaakt_door, laatst_gebruikt_op (hooguit één schrijfactie per minuut), ingetrokken_op, ingetrokken_door.
* Rate limiting: 120 verzoeken per minuut per sleutel → `429 {"fout": "te_veel_verzoeken"}` met `Retry-After: 60`.
  De teller staat in het geheugen; de app draait bewust met één ASGI-proces.
* De tabel `koppelsleutels` wordt niet meegenomen in de CSV-export (wel in de volledige back-up).

### Beheer (alleen beheerders)

Via de bestaande beheer-API met sessie-auth en de bestaande oorsprongcontrole voor schrijfacties:

| Methode | Pad | Resultaat |
|---|---|---|
| `GET` | `/api/koppelsleutels` | Lijst; nooit hash of sleutel |
| `POST` | `/api/koppelsleutels` `{"naam": "Bedrijfsdashboard"}` | Geeft **eenmalig** `sleutel` terug |
| `DELETE` | `/api/koppelsleutels/{id}` | Intrekken (blijft zichtbaar als ingetrokken) |

Aanmaken en intrekken worden in de auditlog vastgelegd (`koppelsleutel.aanmaken`, `koppelsleutel.intrekken`).
In de webinterface: **Beheer → Bedrijfskoppeling (alleen-lezen API)**.

Zonder webinterface (bijv. in de container):

```bash
python -m app.koppeling_cli aanmaken "Bedrijfsdashboard"   # toont de sleutel één keer
python -m app.koppeling_cli lijst
python -m app.koppeling_cli intrekken 3
```

## Endpoints

### `GET /api/koppeling/v1/info`

```json
{"app": "gewichtsverwachting", "naam": "Gewichtsverwachting chrysant", "versie": "0.4.0", "api_versie": "1",
 "bedrijf": null, "datasets": ["samenvatting", "teelten", "oogstplanning", "metingen", "prognoses", "oogsten"],
 "tijd": "2026-11-16T15:00:00Z"}
```

`bedrijf` is `null`: de app heeft (nog) geen instelling voor een bedrijfsnaam.

### `GET /api/koppeling/v1/samenvatting`

```json
{"app": "gewichtsverwachting", "gegenereerd_op": "2026-11-16T15:00:00Z",
 "kpis": [{"sleutel": "metingen_achterstallig", "label": "Metingen achterstallig", "waarde": 2,
           "eenheid": "metingen", "status": "let_op", "toelichting": "1 vandaag te meten (KD10/KD30/pilot)"}],
 "meldingen": [{"niveau": "waarschuwing", "tekst": "2 KD-meting(en) achterstallig", "sinds": "2026-11-14T00:00:00Z"}]}
```

`status`: `ok` | `let_op` | `actie` | `onbekend`. `niveau`: `info` | `waarschuwing` | `kritiek`.

| KPI-sleutel | Betekenis | Status |
|---|---|---|
| `actieve_teelten` | Aantal lopende teelten (status actief); toelichting met bezette kappen van 40 en geplande teelten | `ok` |
| `oogsten_komende_14_dagen` | Lopende/geplande teelten met verwachte oogstdatum vandaag t/m +14 dagen (laatste planningsversie) | `let_op` als er teelten voorbij hun oogstdatum zijn |
| `metingen_achterstallig` | KD10/KD30/pilot-meettaken met vervaldatum in het verleden (zelfde logica als het dashboard) | `ok` bij 0, `let_op` bij 1–2, `actie` bij ≥ 3 |
| `laatst_gemeten_gewicht` | Gemiddeld takgewicht (niet-uitgesloten takken) van de laatst gemeten KD-meting; een **meting**, geen prognose | `onbekend` + `null` zonder meting |
| `gevalideerde_modellen` | Aantal operationeel vrijgegeven modellen (KD10/KD30), 0–2 | `ok` bij 2, anders `let_op` |
| `gegevensdekking` | Standaardteelten (betrouwbaar label) met KD10, KD30 en één oogstgewicht, tegenover het ingestelde trainingsminimum. Indicatief; de definitieve toetsing gebeurt bij training | `ok` als het minimum gehaald is, anders `let_op` |
| `prognose_komende_oogsten` | Gemiddeld voorspeld takgewicht van komende oogsten (14 dagen), **alleen uit gevalideerde prognoses** | `onbekend` + `null` zonder gevalideerde prognose |

Meldingen: achterstallige KD-metingen, teelten voorbij verwachte oogstdatum zonder oogst, geen vrijgegeven
gewichtsmodel (info), mislukte Priva-imports in de afgelopen week, en een ontbrekende of verouderde lokale back-up.

### `GET /api/koppeling/v1/datasets/{naam}?sinds=<ISO>&limiet=<1..500>&cursor=<opaak>`

```json
{"app": "gewichtsverwachting", "dataset": "teelten", "gegenereerd_op": "...", "items": [], "volgende_cursor": null}
```

* `limiet` standaard 100, 1–500. Sortering stabiel op `id` oplopend; `volgende_cursor` is opaak, `null` aan het eind.
* `sinds` filtert op `bijgewerkt_op >= sinds`. Tijden zonder zone worden als UTC gelezen.
* Onbekende dataset → `404 {"fout": "onbekende_dataset"}`. Ongeldige `sinds`/`cursor` →
  `422 {"fout": "ongeldige_parameter", "veld": "..."}`; ongeldige `limiet` → `422`.
* **Privacy**: geen gebruikersnamen, notities, redenen, meetlocaties, OCR-tekst of andere vrije tekst.

| Dataset | Item-velden | `id` / `bijgewerkt_op` |
|---|---|---|
| `teelten` | `id, kap, afdeling, soort, ronde, bedden, categorie, status, labelkwaliteit, plantdatum, licht_uit_datum, verwachte_oogstdatum, dichtheid, geplande_takken, kd_dag` | teelt-id / laatste status- of planningsversie |
| `oogstplanning` | Geplande en lopende teelten met verwachte oogstdatum: `id, teelt_id, kap, afdeling, soort, ronde, status, verwachte_oogstdatum, dagen_tot_oogst, geplande_takken, bedden` | teelt-id / laatste status- of planningsversie |
| `metingen` | `id, teelt_id, kap, soort, ronde, type (KD10/KD30/pilot), gemeten_op, kd_dag, aantal_takken, gemiddeld_gewicht_g, gemiddelde_lengte_cm` (uitgesloten takken tellen niet mee) | meting-id / registratiemoment |
| `prognoses` | Laatst berekende prognose per geplande/lopende teelt: `id, teelt_id, kap, soort, ronde, verwachte_oogstdatum, peilmoment, modeltype, status (gevalideerd/onbekend), gewicht_g, ondergrens_g, bovengrens_g, toelichting` | prognose-id / berekeningsmoment |
| `oogsten` | `id, teelt_id, kap, soort, ronde, begindatum, einddatum, gemiddeld_takgewicht_g, aantal_takken, uitvalfractie, aantal_kwaliteitsklassen` | oogst-id / registratiemoment |

Prognoses worden door de app dagelijks en na elke relevante wijziging opnieuw berekend; de koppeling leest alleen
opgeslagen prognoses en start zelf geen berekening.

## Voorbeeld

```bash
curl -s -H "Authorization: Bearer $SLEUTEL" https://chrysant.example/api/koppeling/v1/samenvatting
curl -s -H "Authorization: Bearer $SLEUTEL" "https://chrysant.example/api/koppeling/v1/datasets/metingen?sinds=2026-11-01T00:00:00Z&limiet=200"
```
