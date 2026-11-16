# Opleverstatus 0.2

Deze versie bevat de uitvoerbare registratie-, import- en modelwerkstromen. Agronomische voorspelkwaliteit en installatie op de eigen TrueNAS-server moeten met echte gegevens worden geaccepteerd.

| Onderdeel | In deze versie |
|---|---|
| Kappen, rollen, planning, oogst en gebeurtenissen | 40 kappen, meerdere soorten/bedden, accounts, aanvullende lichtafspraken en wijzigingshistorie |
| Mobiele metingen | Vijf takken, gemiddelde/spreiding, werkelijke KD-dag, offline concept/wachtrij en idempotente synchronisatie |
| Priva-import | Vijf profielen, CSV/ZIP, preview/bevestiging, achtergrondstatus, hervatten, bronversies, herroepen, checksums en instelbare bronprioriteit |
| Klimaat, licht en water | Fasewaarden, datadekking, gevalideerde eenheden/kalibraties en bronherkomst; kennis- en waarnemingspeildatum afzonderlijk |
| Gewichtsmodellen | Basis-, licht- en klimaatprofielen; optioneel betrouwbare CO₂ en expliciet geschat daglicht; geen invullen van onbekenden met nul |
| Eerlijke toetsing | Rondes vooruit in de tijd, volledige labelbeschikbaarheid, latere certificering uitgesloten uit vroegere training, uitgebreide profielen ook tegen dezelfde basis-Ridge getoetst |
| Seizoen en streeflengte | Daglengterichting, plantseizoenen over Nieuwjaar, versiegebonden lengtecurves en doelen; geen extrapolatie of verzonnen meetnormalisatie |
| Historische Excel | XLSX-upload, echte kop-/kolompreview, expliciete koppeling, rijreview, behoud van correcties, bevestiging en herhaalbaarheid |
| Aangeleverde teelt-/gewicht- en programmawerkboeken | Specifieke reviewworkflow; 840 teeltregels (646 met gewicht, 194 alleen planning), 1.360 programmaregels in 34 rondes; 21 blokmatches met programmarondes 7 tot en met 27; jaartallen per ronde expliciet; 12 tegenstrijdige ankers vragen handmatige review |
| Aanvullende Excel-belichting | 209.955 tijdstempelrijen over 40 modules; ruwe herkomst bewaard, reeksen buiten tijdvolgorde samengevoegd; dubbele tijden en tijdgaten/DST gemarkeerd; alleen mogelijke DLI na expliciete interpretatie en onafhankelijke PPFD-kalibratie; Priva-uren blokkeren Excel altijd, ook bij ongeldige waarden |
| Controlekappen | Beschrijvende vergelijking binnen ronde en soort, onafhankelijke kap-aantallen, pre-oogstprognoses en gescheiden middelen/eenheden; geen causale claims |
| Teeltkaarten en scans | Twee per A4, QR met planning-/profielversie, actuele afspraken en streeflengten; lokale OCR-concepttekst en menselijke bevestiging |
| Back-up en upgrade | Consistente SQLite-back-up, bronbestanden/checksums, herstel naar andere map, geteste upgrade vanaf schema 0001 met behoud van bestaande gegevens |
| TrueNAS | Versiegetagde container, één poort, dataset en Custom App-formulier; daadwerkelijke installatie op de eigen NAS nog uit te voeren |
| Externe toekomstweerprovider | Uit; handmatige scenario’s met uitgifte-/geldigheidsmoment blijven lokaal opgeslagen |
| Bewezen scenario-/ingreepeffect | Nog niet beschikbaar zonder passende gegevens en validatie; geen automatische kas- of middelenaansturing |

Bij de upgrade beginnen ontbrekende oude koppeling- en doelgewichthistorie pas op het migratiemoment. De huidige instelling wordt niet achteraf als bewezen historische kennis gepresenteerd. Bestaande basis-modellen en opgeslagen prognoses blijven behouden; nieuwe modellen bewaren hun eigen invoerprofiel.

De aangeleverde oorspronkelijke werkboeken en Priva-voorbeelden blijven buiten de publieke repository. De complete driejarige historie en betrouwbare KD10/KD30-oogstgegevens ontbreken nog. Er wordt daarom geen getraind bedrijfsmodel of bewezen voordeel tegenover 3,8 gram geclaimd. De gebruikersinterface blijft zonder dat model bruikbaar voor registratie, import, planning, kaarten en historische baselines.

De geautomatiseerde controles en browseracceptatie controleren softwaregedrag en importreview; ze vormen geen claim over agronomische voorspelkwaliteit. Werkelijke jaartoewijzingen en de twaalf tegenstrijdige ankers blijven expliciete menselijke reviewpunten. De aanvullende Excel-lichtreeks is niet fysisch gevalideerd zolang tijd-/percentagebetekenis en onafhankelijke PPFD niet zijn bevestigd.
