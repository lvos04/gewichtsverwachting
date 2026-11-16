# Opleverstatus 0.1

Dit is een uitvoerbare eerste release; niet alle onderdelen van het volledige bouwplan zijn al afgerond of met echte bedrijfsdata geaccepteerd.

| Onderdeel | In deze versie |
|---|---|
| 40 kappen, afdelingen, kranen en lichtmodules | Aanwezig; huidige mapping vanaf 16 november 2026, historische bevestiging nodig |
| Accounts, rollen, planning, oogst, gebeurtenissen | Aanwezig met historie/audit en meerdere soorten per kap |
| Vijf-taksmetingen, gemiddelde, spreiding, KD-controle | Aanwezig; mobiele offline wachtrij met unieke ID’s |
| CSV/ZIP-import | Alle vijf voorbeeldprofielen, preview/bevestiging, achtergrondstatus, hervatten, bronversies, herroepen, checksums en bronprioriteit |
| Klimaat en licht | Datumgebonden kwaliteitscontrole, beheerbare bevestigde kalibratie; ontbrekende betekenissen blokkeren betrouwbare DLI |
| Seizoensrichting | Cyclische plantweek, daglengte, toename/afname en ontwikkeling tot geplande oogst |
| Gewichtsmodellen | Werkende Ridge-training/evaluatie met datadrempel, tijd-/rondegrenzen en gecontroleerde activatie; geen getraind bedrijfsmodel meegeleverd |
| Toekomstweer | Handmatig scenarioarchief; externe provider uit, geen gekalibreerd gewichteffect |
| Teeltkaarten en scans | Twee per A4, QR met planningversie, lokale OCR-concepttekst, handmatige review en bevestiging |
| Back-up en herstel | SQLite backup-API, bestandenmanifest/checksums, offline herstel naar andere map, automatische dagelijkse lokale back-up |
| Container en TrueNAS-instructies | Eén niet-root-container en Custom App-formulier; eigen TrueNAS-acceptatietest nog nodig |
| Historische Excel-bronnen | Niet meegeleverd; nog geen import zonder de echte kolommen en toewijzingsregels |
| Lengtecurves, tussenlicht, geavanceerde teeltkaart | Nog geen beheer van soort/seizoenscurves of alle bestaande Excel-kaartvelden |
| Volledige klimaatfeatures in gewichtsmodel | Nog niet aangesloten op de Ridge-invoer; eerst bronbetekenis en echte historie valideren |
| Geautomatiseerd bijstuuradvies | Geen gekalibreerd advies of causaliteitsclaim; gebeurtenissen en controlecategorie wel aanwezig |
| Cloudflare Tunnel / domein / TrueNAS-installatie | Handleiding aanwezig; externe infrastructuur wordt niet automatisch ingericht |

De vijf oorspronkelijke voorbeeldbestanden zijn alleen gebruikt voor lokale verificatie en blijven buiten de publieke repository. De circa driejarige historie en echte KD10/KD30-oogstgegevens ontbreken nog. Het model kan daarom nog geen bewezen voordeel tegenover 3,8 g laten zien.

De geautomatiseerde tests gebruiken synthetische gegevens voor reproduceerbare rekenregels, idempotentie, bronkwaliteit, herstart, tijdlekken, rollen, scanbevestiging en herstel. De browsertest controleert echte mobiele invoer en offline synchronisatie. Een werkelijke gebruikerstest in de kas, handschriftherkenning op eigen kaarten en herstel op de eigen TrueNAS-dataset blijven nodig voor volledige acceptatie.

Lokale verificatie bij oplevering: 41 pytest-controles geslaagd; uitgebreide Playwright-browsercontrole; container gebouwd en gestart als UID568 met geslaagde healthcheck; de vijf originele voorbeelden leverden samen 8.792 genormaliseerde rijen op. Deze aantallen beschrijven softwarecontroles, geen bewezen voorspelkwaliteit op bedrijfsgegevens.
