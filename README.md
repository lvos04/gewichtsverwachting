# Gewichtsverwachting chrysant

Nederlandstalige lokale webapp voor 40 kappen: teeltplanning, vijf-taksmetingen, Priva-import, oogstregistratie en controleerbare gewichtsprognoses. Python 3.12, FastAPI, SQLite en scikit-learn (productieafhankelijkheden vastgezet in `constraints.txt`); één container, één poort. Geen verplichte externe API of gegevensverzending.

**Versie 0.2 voegt klimaatprofielen, review van de aangeleverde historische Excel-werkboeken en seizoenscurves toe.** Registratie en import werken zonder getraind model. De nauwkeurigheid is nog niet aangetoond met bedrijfsgegevens. Een doelgewicht van 105 g wordt nooit als voorspelling gepresenteerd. Zie [opleverstatus](docs/STATUS.md) voor resterende punten uit het bouwplan.

**Versie 0.3** voert het verbeterplan uit: aanmeldbeperking per account én echt IP (vertrouwde proxy instelbaar), optionele tweestapsverificatie, strikte oorsprongcontrole, oogstcorrectie met reden, back-upstatus met schijfruimte, dagelijkse opschoning en een uitschakelbare bedrijfskoppeling (standaard uit). Zie [wijzigingen](docs/WIJZIGINGEN.md).

## Starten

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://127.0.0.1:8000` en maak de eerste beheerder aan met een wachtwoord van minimaal twaalf tekens. Er zijn geen standaardaccounts, wachtwoorden of fictieve teelten. Database-migratie en de 40 kappen worden automatisch geïnitialiseerd. Voor lokale OCR zijn Tesseract met de taalpakketten Nederlands/Engels en libzbar nodig; de container bevat die. Zonder OCR blijft handmatige scanreview beschikbaar.

```bash
docker build -t gewichtsverwachting:0.3.0 .
docker run --rm -p 8000:8000 -e DATA_DIR=/data -v chrysant-data:/data gewichtsverwachting:0.3.0
```

Bij een bedrijfsproxy met een eigen vertrouwde CA kan de bouw optioneel `--secret id=build_ca,src=/pad/naar/ca-bundle.pem` gebruiken. Certificaatcontrole blijft ingeschakeld; de CA wordt niet in de image opgeslagen.

[Installatie op TrueNAS via Custom App](docs/TRUENAS.md) · [gebruik en import](docs/GEBRUIK.md) · [back-up en herstel](docs/HERSTEL.md) · [model en seizoen](docs/MODEL.md) · [historische Excel-import](docs/EXCEL.md) · [bedrijfskoppeling API](docs/koppeling-api.md) · [wijzigingen](docs/WIJZIGINGEN.md)

## Tijd van het jaar

Het model gebruikt cyclische plantweekkenmerken en de daglengte en daglengterichting op het bijbehorende KD-meetmoment. Een latere raadpleging voert geen langere lichtperiode in een model in dat op KD10 of KD30 is getraind. De interface toont of dagen langer of korter worden en hoe dit zich tot de geplande oogst ontwikkelt. Standaardbreedtegraad 52° N is een expliciete Nederlandse aanname, instelbaar via **Beheer → Algemene instellingen** (de optionele omgevingsvariabele `GREENHOUSE_LATITUDE_DEG` is alleen de beginwaarde). Astronomische daglengte is geen gemeten kaslichtsom. Bewolking, lampen en schermen vragen afzonderlijke gevalideerde invoer; donkere/gemiddelde/lichte toekomstscenario’s krijgen geen verzonnen gewichteffect.

## Gegevens en veiligheid

Originele bedrijfsbestanden horen uitsluitend op de persistente dataset. Deze publieke repository bevat synthetische testgegevens. De aangeleverde Excel-werkboeken en Priva-voorbeelden blijven buiten de publieke repository; de import bewaart de originelen alleen in de lokale dataset.

- Rollen: beheerder, medewerker, lezer; gehashte wachtwoorden, HTTP-only sessies, oorsprongcontrole en aanmeldbeperking.
- Metingen worden per gebruiker lokaal in IndexedDB gebufferd, met unieke meet-ID en servercontrole tegen dubbele synchronisatie. Offline beschikbaarheid vergt dat de app eerst online is geopend; HTTPS is vereist voor PWA-installatie buiten localhost.
- Elke gewijzigde planning, koppeling, doelgewicht, seizoenscurve, importreview en scanreview krijgt historie/audit.
- Modellen kunnen basis-, licht- of klimaatkenmerken gebruiken. Controleerbare gegevensdekking en onafhankelijke toetsing gaan vóór een gewichtsuitkomst.
- Externe weer- en AI-providers staan uit. Teeltacties worden door mensen vastgelegd, niet automatisch naar de kas gestuurd.
- De app gebruikt één ASGI-proces voor de duurzame achtergrondimport. Stel geen extra Uvicorn-workers in.

## Ontwikkeling en controles

```bash
pip install -r requirements-dev.txt
pytest -q
python -m playwright install chromium
python tests/browser_smoke.py
```

GitHub Actions voert tests en containerbouw uit. Tags `v*` publiceren een versie-image naar `ghcr.io/lvos04/gewichtsverwachting`. [De workflow](.github/workflows/image.yml) moet succesvol zijn voordat die tag als installatie-image beschikbaar is. De broncode bouwt ook lokaal zonder containerregister.
