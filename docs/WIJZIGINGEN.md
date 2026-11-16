# Wijzigingen

## 0.3.0 — verbeterplan (november 2026)

Alles wat het gedrag voor gebruikers of beheerders verandert, staat achter een instelling in **Beheer**. De
standaardwaarden behouden het bestaande gedrag, behalve waar dat aantoonbaar onveilig of kapot was (aangegeven).

### Nieuwe instellingen

| Instelling (Beheer) | Sleutel | Standaard | Toelichting |
|---|---|---|---|
| Maximale inlogpogingen per IP-adres | `login_ip_max_attempts` | **25** | Was gelijk aan het accountlimiet (5), waardoor één persoon achter een proxy/tunnel iedereen blokkeerde. `0` = oud gedrag. |
| Maximale vertraging na foute pogingen | `login_delay_max_seconds` | 1 s | 0,25 s per eerdere fout op dat account, maximaal deze waarde. `0` = uit. |
| Vertrouwde proxy's | `trusted_proxies` | leeg (geen) | IP's/CIDR's waarvan `X-Forwarded-For` wordt gebruikt. Leeg = omgevingsvariabele `TRUSTED_PROXIES`, anders geen. Zie [TrueNAS](TRUENAS.md). |
| Strikte oorsprongcontrole | `strict_origin_check` | **aan** | Schrijfverzoek zonder `Origin` moet een `Referer` van dezelfde host hebben. De webapp en PWA-synchronisatie sturen altijd `Origin`; alleen scripts zonder die header worden geweigerd. |
| Tweestapsverificatie verplicht voor beheerders | `mfa_required_admins` | uit | Kan pas aan als de eigen beheerder MFA heeft (voorkomt buitensluiten). |
| Bedrijfskoppeling ingeschakeld | `koppeling_enabled` | **uit** | Uit → `/api/koppeling/v1/*` geeft `403 {"fout":"koppeling_uitgeschakeld"}`. Schakelaar staat boven het sleutelbeheer. |
| Auditlog bewaren (dagen) | `audit_keep_days` | 0 = onbeperkt | Alleen > 0 kort de auditlog dagelijks in (met een auditregel over het inkorten). |
| Bewaartermijn back-ups | `backup_keep_days` (bestond) | 0 = onbeperkt | Ongewijzigd; Beheer toont nu advies (30 dagen), schijfruimte en een waarschuwing over een kopie elders. |

### Beveiliging
* Aanmeldbeperking per account én per echt IP-adres, met vertrouwde proxy voor `X-Forwarded-For` (rechts-naar-links,
  niet te vervalsen) en oplopende vertraging na mislukte pogingen.
* Optionele TOTP-tweestapsverificatie per gebruiker (QR-code of handmatige sleutel), beveiligd tegen hergebruik van
  codes; beheerder kan MFA van een ander account resetten; noodcommando `python -m app.beheer_cli mfa-uit <naam>`.
* Strikte oorsprongcontrole met terugval op `Referer`.
* `html`-template-helper in de webinterface (`app/static/js/util.js`) die standaard escapet; nieuwe schermen
  gebruiken die. De bestaande Content-Security-Policy (`script-src 'self'`, geen inline scripts) blijft actief.

### Gegevensintegriteit en onderhoud
* Oogstregistratie corrigeerbaar met verplichte reden (nieuwe, gekoppelde versie; origineel blijft bewaard) en
  audit van voor/na. Historische referentie en prognoses gebruiken vanaf het correctiemoment de gecorrigeerde
  waarde, zonder terugwerkende kracht.
* Dagelijkse opschoning van verlopen sessies, verlopen MFA-stappen en oude aanmeldpogingen.
* `/api/audit` ondersteunt paginering (`before_id`, `limit`); standaard ongewijzigd de nieuwste 1000.
* Back-upstatus (`GET /api/backups/status`) met schijfruimte en advies.

### Historische Excel-import (uit 0.2.2)
* Bulkbeoordeling van importregels (`app/static/history_bulk.js`): gelijke antwoorden voor alle passende bronrijen
  over alle pagina's (filter op status, reden, ronde, blok, werkblad of soort) of alleen voor aangevinkte rijen,
  ook op andere pagina's. Eerst een proefberekening, verplichte onderbouwing; bestaande en handmatige waarden blijven
  standaard behouden. Iedere wijziging doorloopt opnieuw de kwaliteitscontrole; pas de definitieve importknop maakt
  teelten aan.
* De gekoppelde teelt-/gewicht- en programmawerkboeken tonen alle regels: 840 teeltschemaregels plus 520 uitsluitend
  programmaregels (1.360 unieke regels; gekoppelde rijen tellen één keer). Regels zonder gewicht blijven zichtbaar
  maar standaard uitgesloten. Jaarankers kunnen voor meerdere geselecteerde rondes tegelijk worden ingevuld.
* `history_bulk.js` staat in de offline-cache van de service worker (cacheversie verhoogd naar `v3`) en wordt als
  module van dezelfde oorsprong geladen, binnen de bestaande Content-Security-Policy.

### Performance
* Overzicht/taken (`dashboard_data`) filtert en telt in SQL in plaats van alle metingen en planningen te laden.
* Teeltdetail laadt takken en planning voor alle metingen in een vast aantal queries (geen N+1).
* Prognoses worden alleen herberekend na een relevante wijziging (dirty-vlag), bij een nieuwe dag of wanneer een
  vooruit geplande instelling ingaat, in plaats van elke minuut alle teelten te controleren.

### Onderhoudbaarheid
* Invoerschema's in `app/core_schemas.py`; zuivere webhulpfuncties in `app/static/js/util.js`.
* ruff (alleen echte fouten) en ruff-format (nieuwe modules) in CI.
* Versie uit één bron (`app/__init__.py`); `pyproject.toml` leest die dynamisch.
* Image-workflow: een push naar een `ccr-*`-branch of handmatig starten publiceert `<versie>-rc`, `branch-<naam>` en
  `sha-<kort>` (handmatig ook `preview`), nooit `latest`. Tag-builds `v*` ongewijzigd.

### Migratie
* Schema `0005`: MFA-velden op `users`, tabel `mfa_challenges`, en `supersedes_id`/`reason`/`author_id` op
  `harvests`. Alleen toevoegingen.
