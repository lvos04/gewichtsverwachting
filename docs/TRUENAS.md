# TrueNAS SCALE Custom App

Gebruik een versiegetagde image uit de geslaagde GitHub Actions-publicatie, bijvoorbeeld `ghcr.io/lvos04/gewichtsverwachting:v0.1.0`. Gebruik pas een tag die daadwerkelijk gepubliceerd is. De app is in een Linux-container getest; een installatie op de eigen TrueNAS-server blijft een afzonderlijke gebruikerstest.

1. Maak een dataset, bijvoorbeeld `/mnt/tank/apps/chrysant`. Geef de containergebruiker UID/GID **568:568** lees- en schrijfrechten via de TrueNAS ACL-editor. Gebruik geen algemene wereldschrijfrechten.
2. Open **Apps → Discover → Custom App** en vul de applicatienaam `gewichtsverwachting` in.
3. Image repository: `ghcr.io/lvos04/gewichtsverwachting`; tag: de gekozen vaste versie. De container gebruikt geen root en heeft geen privileged-modus nodig.
4. Publiceer containerpoort **8000/TCP** op een vrije hostpoort, bijvoorbeeld **30800**.
5. Voeg de dataset toe als hostpath, gemount op **`/data`**, met schrijfrecht.
6. Stel onderstaande omgevingsvariabelen in. Eén replica, één webproces; geen extra command-override nodig.
7. Start de app. Open eerst via het vertrouwde lokale netwerk `http://SERVER:30800`, maak de beheerder aan en leg een proefkapteelt en meting vast.

| Variabele | Waarde | Betekenis |
|---|---|---|
| `DATA_DIR` | `/data` | Alle persistente bestanden |
| `TZ` | `Europe/Amsterdam` | Tijdzone |
| `SECURE_COOKIES` | `true` bij extern HTTPS | Stuur sessiecookie uitsluitend via HTTPS |
| `GREENHOUSE_LATITUDE_DEG` | optioneel `52.0` | Alleen beginwaarde; daarna wijzigen via Beheer |
| `DISABLE_API_DOCS` | optioneel `1` | Verberg Swagger op `/docs` |

**Mobiele toegang:** configureer een bestaande Cloudflare Tunnel naar de hostpoort en gebruik een eigen HTTPS-hostnaam. Behoud de externe Host-header. Zet Cloudflare Access ervoor en maak daarnaast appaccounts voor medewerkers. De app vertrouwt willekeurige proxyheaders niet. Stel `SECURE_COOKIES=true` in zodra toegang via HTTPS gaat; rechtstreeks HTTP-aanmelden werkt daarna niet. Richt de eerste beheerder in voordat het adres breed bereikbaar wordt. Een tunnelaccount/domein wordt niet automatisch aangemaakt.

Een alternatief is Tailscale met HTTPS-toegang op het privénetwerk. PWA en offline shell werken op telefoons alleen in een veilige browsercontext (HTTPS).

**Continuïteit:** plan dagelijkse ZFS-snapshots; de app maakt daarnaast dagelijks een lokale SQLite-back-up zodra zij draait. Lokale back-ups op dezelfde dataset beschermen niet tegen verlies van die dataset: kopieer ze naar een andere bestemming. Stel back-upinterval en bewaartermijn in via **Beheer**; standaard is dit dagelijks en onbeperkt bewaren. Bij een ingestelde bewaartermijn worden oudere lokale back-ups pas na een geslaagde nieuwe back-up opgeruimd, met behoud van de nieuwste kopie. Maak vóór een update een handmatige back-up, voer een hersteltest uit en noteer de oude imagetag. De app maakt ook vóór schemamigraties van een bestaande database een consistente back-up.

**Controleren:** `/health` geeft `status: ok`. Controleer ook daadwerkelijk aanmelden, een offline meting synchroniseren, een importvoorbeeld bevestigen en een back-up herstellen. Een groene healthcheck alleen bewijst die functies niet.
