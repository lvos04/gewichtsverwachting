# Priva Bridge als bron voor klimaat en licht

Vanaf 0.4.0 kan de gewichtsapp haar klimaat- en lichtdagwaarden uit de **Priva Bridge** halen, de lokale
gegevensbron op de TrueNAS (implementatieplan *Priva Bridge v1.1*, sectie 2 en 8). De gewichtsapp is daar de eerste
afnemer. De wekelijkse handmatige upload van Priva-exports verhuist naar de Bridge; de bestaande CSV-import in deze
app blijft als terugval werken.

**Standaard staat de koppeling uit.** Uit betekent precies het gedrag van 0.3: klimaat, licht en modelkenmerken
komen alleen uit de eigen Priva-import, en de app maakt geen enkele verbinding met de Bridge.

## Instellen (Beheer → Klimaat- en lichtdata uit Priva Bridge)

Alleen beheerders zien dit paneel.

| Instelling | Sleutel | Standaard | Toelichting |
|---|---|---|---|
| Klimaat- en lichtdata uit Priva Bridge | `bridge_enabled` | **uit** | Aan: dagelijks ophalen, “Nu ophalen” en gebruik in de berekeningen. |
| Bridge-URL | `bridge_url` | leeg | **Alleen intern netwerk**: privé-IP (`10.x`, `172.16–31.x`, `192.168.x`, `fd00::/8`, loopback) of een interne naam (zonder punt, of eindigend op `.lan`, `.local`, `.internal`, `.home.arpa`). Bij elk verzoek controleert de app opnieuw dat de naam naar een privé-adres wijst. Een achtervoegsel `/api/v1` wordt weggehaald. |
| Eigen CA-certificaat | `bridge_ca_pem` | leeg | Optioneel, voor `https://` met een eigen certificaat. Alleen het openbare CA-certificaat (PEM). |
| Voorrang bij overlap | `bridge_voorrang` | `bridge_ok_verdacht` | Zie hieronder. |
| Dagelijks ophalen vanaf (uur) | `bridge_sync_hour` | 6 | Nederlandse tijd. De Bridge verwerkt rond 04:15; 06:00 laat ruimte. |
| Dagen terug bij dagelijks ophalen | `bridge_sync_days` | 14 | Elke dag worden deze afgelopen dagen opnieuw opgevraagd, zodat revisies in de Bridge (plan: hercontrole van de laatste zeven dagen) binnenkomen. |
| API-token | — (aparte opslag) | geen | Eén token per afnemende app, aangemaakt in de Bridge-beheer-UI met scope `read:data`. |

De instellingen (behalve het token) worden zoals alle instellingen als nieuwe, geauditeerde instellingenversie
bewaard. Berekeningen met een peildatum gebruiken de instelling die op dat moment gold.

**Token.** Het token wordt versleuteld opgeslagen (tabel `bridge_credentials`; HMAC-SHA256-sleutelstroom met
encrypt-then-MAC, alleen standaardbibliotheek) en nooit naar de webinterface, de instellingenhistorie of de auditlog
teruggegeven. De sleutel komt uit de omgevingsvariabele `BRIDGE_TOKEN_KEY` (aanbevolen op TrueNAS) of anders uit
`/data/secrets/bridge-token.key` (wordt bij eerste gebruik aangemaakt, rechten 0600). De map `secrets/` gaat
bewust **niet** mee in de back-up-zip: een back-up bevat dus geen bruikbaar token. Na herstel op een andere
machine (of zonder dezelfde `BRIDGE_TOKEN_KEY`) meldt Beheer dat het token niet te ontsleutelen is; voer het dan
opnieuw in.

**Verbinding testen** roept `GET /api/v1/status` aan (en `GET /api/v1/metrics`) en toont de laatste complete dag,
de bronmodus, ontbrekende dagen, waarschuwingen en welke gebruikte meetwaarden in de catalogus ontbreken. Dit kan
ook met de schakelaar uit, zodat je eerst kunt testen.

## Wat wordt opgehaald

Altijd server-side, nooit vanuit de browser. Per synchronisatie:

1. `GET /api/v1/status` — actualiteit (`last_import_at`, `last_complete_date`), ontbrekende dagen, waarschuwingen.
2. `GET /api/v1/metrics` — welke meetwaarden de catalogus kent.
3. `GET /api/v1/daily` met `scope=kap:1 … kap:40` en `scope=afdeling:1 … 4` (in groepen van tien scopes),
   `inherit=true`, `from`/`to` (lokale datums, inclusief), `limit=5000`, en de meetwaarden:

| Meetwaarde (catalogus) | Gebruik in de app |
|---|---|
| `lichtsom_lampen_kap` (mol/m²/dag, afgeleid in de Bridge) | Lichtsom lampen per kap: klimaatpagina en modelkenmerk `env_lamp_light_mol_m2` |
| `temp_etmaal_avg` (°C, afdeling → kap via `inherit=true`) | Etmaaltemperatuur: modelkenmerk `env_temperature_c` |
| `rv_avg` (%) | Relatieve vochtigheid: modelkenmerk `env_rh_pct` |
| `lamp_brandseconden` (s) | Lampuren (getoond) |
| `scherm_seconden` en varianten die met `scherm_seconden` beginnen (s) | Schermuren (getoond) |
| `temp_etmaal_min`, `temp_etmaal_max`, `vochtdeficit_avg`, `par_som_lampen` | Alleen getoond / bewaard |

Meetwaarden die niet in de catalogus staan, worden niet opgevraagd. De cursor (`next_cursor`) wordt gevolgd tot
`null`; een herhaalde cursor stopt de synchronisatie met een melding.

**Dagelijks:** de bestaande achtergrondtaak (elke minuut) start vanaf het ingestelde uur één keer per dag een
synchronisatie over de laatste *N* afgelopen dagen (vandaag nooit). Na een fout wacht de taak 30 minuten, of langer
als de Bridge een `Retry-After` gaf. **Nu ophalen** doet hetzelfde direct, optioneel over een zelfgekozen periode
(historie, maximaal tien jaar per keer).

## Cache en herkomst

Elke ontvangen regel komt in `bridge_daily_values` met scope, datum, meetwaarde, waarde, eenheid, aggregatie,
`coverage`, `quality`, `resolved_from`, `sources`, `import_id`, `revision`, de synchronisatie en het ophaalmoment.
De cache is **append-only**: alleen een afwijkende regel (andere waarde, kwaliteit, dekking, revisie, …) wordt
toegevoegd; ongewijzigde regels niet. Een berekening op een peildatum gebruikt per dag de laatste regel die op die
peildatum al was opgehaald, zodat oude prognoses reproduceerbaar blijven. Elke synchronisatie en test staat in
`bridge_syncs` (zichtbaar in Beheer).

## Ontbreekt is nooit 0

* `quality = ontbreekt` → waarde `null`, ook als de Bridge onverhoopt een getal meestuurt.
* Een waarde die geen eindig getal is → kwaliteit `onbekend`, nooit gebruikt.
* `onvolledig` (en `onbekend`) telt nooit als meting. Zonder eigen import blijft zo’n dag ontbrekend; de bestaande
  dekkingsregel (`environment_min_coverage`) bepaalt dan of het kenmerk beschikbaar is.
* Eenheden en bereik worden gecontroleerd (lichtsom 0–100 mol/m²/dag, temperatuur −40–65 °C, RV 0–100 %); een
  afwijkende eenheid maakt de regel onbruikbaar in plaats van om te rekenen.
* Een echte gemeten 0 met kwaliteit `ok` blijft 0.

## Voorrang tussen Bridge en eigen import

Per dag en per kenmerk:

| `bridge_voorrang` | Regel |
|---|---|
| `bridge_ok_verdacht` (standaard) | Bridge wint als de Bridge-dag kwaliteit `ok` of `verdacht` heeft; anders de eigen import. |
| `bridge_alleen_ok` | Bridge wint alleen bij `ok`; anders de eigen import. |
| `eigen_import_eerst` | De eigen import wint; de Bridge vult alleen dagen aan waar de eigen import niets geldigs heeft (`ok`/`verdacht`). |

Een Bridge-lichtsom gaat ook vóór aanvullende Excel-belichting (beide zijn ondergeschikt aan Priva; de Bridge-waarde
*is* Priva-data). Water en CO₂ komen in v1 niet uit de Bridge: `watergift` heeft nog geen bevestigde eenheid en
`co2_dosering` is een instelwaarde, geen meting.

## Waar zie je de bron?

* **Klimaat & licht** (met een kap gekozen): kolom *Bron lampen* in *Lichtsom per dag*, plus het blok *Priva Bridge*
  met per dag de Bridge-waarde, kwaliteit, dekking en welke bron telt, de laatste complete dag en het laatste
  ophaalmoment.
* **Teeltdetail → Licht, klimaat & water per fase**: aantal dagen per kenmerk uit de Bridge en uit de eigen import.
  De volledige herkomst (import-ID’s, aantal cacheregels en een hash daarvan) staat in het omgevingsresultaat onder
  `bridge`.
* **Beheer**: laatste complete dag, laatste import in de Bridge, laatste (gelukte) synchronisatie en fouten.

## Foutafhandeling (contract sectie 8.1)

| Antwoord | Gedrag |
|---|---|
| 401 | “Token geweigerd”; synchronisatie gestopt, cache ongewijzigd. |
| 403 | “Token mist scope `read:data`”. |
| 404 | “Endpoint niet gevonden; controleer URL/API-versie”. |
| 429 + `Retry-After` ≤ 60 s | Maximaal drie keer wachten en opnieuw. |
| 429 met langere of geen `Retry-After` | Stoppen; de dagtaak probeert pas na `Retry-After` opnieuw. |
| 3xx | Niet gevolgd (het token gaat nooit naar een ander adres). |
| Timeout (verbinden 5 s, lezen 30 s) of onbereikbaar | Melding; al opgehaalde regels blijven, de eigen import blijft gelden. |
| `application/problem+json` | `title` en `detail` worden ingekort in de melding getoond. |

## Aannames over het contract

Deze app is gebouwd tegen sectie 8 van het plan, vóórdat de Bridge zelf klaar was. Controleer bij de eerste echte
koppeling:

1. `GET /api/v1/metrics` geeft `{"data": [{"metric_key": …}]}` (ook `metric`/`key` of een lijst met namen wordt
   geaccepteerd). Bij een ander formaat valt de app terug op de vaste lijst hierboven.
2. Meerdere `scope`- en `metric`-parameters worden als herhaalde queryparameters gestuurd (`scope=kap:1&scope=kap:2`).
3. De eenheid van `lichtsom_lampen_kap` is een van `mol/m²/dag`, `mol/m2/dag`, `mol/m²/d`, `mol/m2/d`, `mol/m²`,
   `mol/m2`; van `temp_etmaal_avg` `°C`/`C`; van `rv_avg` `%`. Een andere spelling maakt de waarde onbruikbaar
   (veilig), dus pas zo nodig `MODEL_METRICS` in `app/bridge.py` aan.
4. Schermdoeken komen als aparte metric-sleutels die met `scherm_seconden` beginnen, of als één `scherm_seconden`.
5. `next_cursor` is een string of `null`; `freshness` staat in elk `/daily`-antwoord.
6. `revision` is een geheel getal; `coverage` ligt tussen 0 en 1.
7. Met `inherit=true` geeft de Bridge voor `kap:N` de afdelingswaarde met `resolved_from = "afdeling:M"`.
8. De kapnummers in de Bridge zijn dezelfde 1–40 als in deze app.
