# Historische Excel-import met menselijke controle

De oorspronkelijke teelt-/gewicht- en teeltprogrammawerkboeken zijn aangeleverd. De app herkent hun werkelijke indeling met een aparte reviewworkflow; daarnaast blijft de generieke **XLSX**-import beschikbaar voor andere werkboeken. De originelen blijven ongewijzigd in de lokale dataset en worden niet in de publieke repository opgenomen. Oude `.xls`-bestanden eerst als XLSX opslaan; macro's en versleutelde werkboeken zijn niet toegestaan.

## Aangeleverde teelt- en programmawerkboeken

Open **Historische teelten → Excel** en upload beide werkboeken. Alle 840 regels uit het teeltschema blijven zichtbaar: 646 met een gewicht en 194 zonder gewicht. Die 194 zijn standaard uitgesloten van definitieve import, niet verwijderd; je kunt ze later als planning meenemen. Het programma bevat 1.360 kapregels, waarvan 520 zonder gekoppelde teeltschemaregel. Het standaardcontrolevoorbeeld toont zo 1.360 unieke regels: gekoppelde programmarijen tellen samen met de teeltschemaregel één keer. Alle rijen zijn bereikbaar via de paginering; een eerste scherm met 200 regels betekent niet dat de overige regels ontbreken. De 21 overeenkomende blokken koppelen aan programmarondes 7 tot en met 27.

Bestandsnamen en rondeblokken leveren geen betrouwbaar kalenderjaar. Vul daarom per gekoppelde ronde het expliciete eerste ISO-jaar in; de review toont de afgeleide jaren per rij. Je kunt voor geselecteerde rondes hetzelfde eerste ISO-jaar invullen; standaard worden alleen lege jaarvelden ingevuld. Twaalf tegenstrijdige rijankers tussen teeltschema en programma moeten handmatig worden beoordeeld en gemotiveerd geaccepteerd of uitgesloten. Onbevestigde rijen maken geen kapteelten aan. Formulecaches worden als bewijs getoond, maar niet als geverifieerde bronwaarde gebruikt. Bevestiging is expliciet en herhaalbaar; broncellen, checksums, correcties en herkomst blijven bewaard.

Voor grote voorbeelden kun je tussen pagina's bladeren en bronregels selecteren; de selectie blijft over pagina's bestaan. Filter groepscorrecties op status, reden, ronde, soort of blok, of selecteer afzonderlijke regels. Een proefberekening toont eerst hoeveel regels overeenkomen, veranderen of worden overgeslagen. Daarna kun je één gecontroleerde veldwaarde met een reden aan de selectie geven. Bestaande handmatige waarden blijven standaard behouden. Het opnieuw opbouwen van het controlevoorbeeld past de bron- en kwaliteitsvalidatie opnieuw toe; pas de aparte definitieve bevestiging maakt teelten en oogsten aan.

Dubbele programmarijen worden samen als bewijs getoond en vragen een expliciete bronrijkeuze met reden. Een afwijkend rondelabel moet met een gemotiveerde rondekoppeling worden bevestigd. In het aangeleverde programma heeft Ronde 34 zo’n afwijkend label; dit ligt buiten de 21 gekoppelde gewichtsblokken.

De programmabron is planningsbewijs. Werkelijke teeltgewichten komen uit het teeltschema; er worden geen ontbrekende oogsten of KD-metingen verzonnen. Oogstgewichten met meerdere kwaliteitsklassen worden niet gemiddeld. Historische alar en andere gehele-kapwaarden blijven aan hun bronrij gekoppeld en worden niet zonder verdeelsleutel over soorten verdeeld.

## Aanvullende Excel-belichting

Het werkboek bevat ook een `data`-blad met 209.955 tijdstempelrijen over 40 modules. Dit wordt apart gestart via **Priva-import → Aanvullende Excelbelichting**. De parser behoudt ruwe waarden en herkomst; rijen buiten tijdvolgorde worden in dezelfde dag-/moduleaggregaten verwerkt en als zodanig gemeld. Dubbele tijdstempels worden geteld en blokkeren een betrouwbare lichtsom. Ontbrekende intervallen, onvolledige dagen en zomertijd-/wintertijdsdagen krijgen kwaliteitsmeldingen.

Bij bevestiging kiest een gebruiker in de interface het Excel-datumstelsel, de tijdzone `Europe/Amsterdam`, de tijdstempelbetekenis (intervalbegin of -einde) en of het percentage aantoonbaar een lineaire dimfractie is. Onbekende betekenissen kunnen worden gearchiveerd maar leveren geen lichtsom. Een bevestigde ruwe procentreeks levert pas een mogelijke dimgewogen brandduur; een DLI vraagt daarnaast een datumgeldige, onafhankelijke PPFD-kalibratie.

Priva-uren hebben altijd voorrang: zodra voor dezelfde dag en module een Priva-urenrecord bestaat, blokkeert dit de Excel-aanvulling, ook als die Priva-waarde leeg of ongeldig is. De aanvullende kalibratie gebruikt exact label `legacy_excel_full_power_hours`, eenheid `h`, leeg kanaal en dimfactor `1`, omdat de Excel-uren al dimgewogen zijn. Bevestig via **Beheer → Bronkalibraties** de werkelijke PPFD en zelfstandige modulebijdrage; controleer dat deze bijdrage niet al in een andere bron zit. Algemene bronprioriteiten en overige app-instellingen zijn via de beheerformulieren instelbaar. Zonder deze interpretatie en kalibratie blijft de uitkomst archief-/kwaliteitsinformatie, geen gevalideerde DLI.

1. Open **Priva-import → Historische Excel** en upload het werkboek. Het origineel krijgt een checksum en blijft ongewijzigd op de eigen dataset.
2. Kies het werkblad en de werkelijke kopregel. De app toont kolomletters en de echte cellen, ook als een kolom geen naam heeft.
3. Koppel ronde, kap, soort, plantdatum, licht-uitdatum, leeg-/einddatum en werkelijk eindgewicht. Koppel waar beschikbaar begin oogst, dichtheid, bedden, takkenaantallen, uitval en historische alar met zijn eenheid. Een standaardwaarde is alleen voor toegestane optionele velden en moet bewust worden gekozen.
4. Maak een controlevoorbeeld. Tot hier worden **geen kapteelten of oogsten** aangemaakt.
5. Los ambigue rijen op door de correcte waarden met reden in te voeren, of sluit ze met reden uit. Bekijk overige pagina’s bij grotere bestanden. Eerdere koppelingen en correcties blijven in reviewversies terugvindbaar.
6. Bevestig expliciet. De hele bevestiging is één transactie. Een tweede identieke bevestiging maakt geen dubbele teelten; een andere toewijzing van een eerder bevestigde bronrij wordt een conflict, geen stil overschrijven.

Excel-datums en volledige datums in ISO of dag-maand-jaar worden ondersteund. Tweecijferige jaren, ontbrekende jaren, alleen weeknummers en rekenformules blijven ambigue. De app voert geen formules uit en neemt geen onbekende formulecache als waarneming over. Historische oogsten in de toekomst worden niet als echte eindgewichten aanvaard.

Soortcodes a/p/t/b betekenen antonov/pjotr/tutu/baltazar. De soort moet in Beheer bestaan; voor een historische soort kan de actieve status worden uitgezet. Er wordt geen Baltazar-doelgewicht verzonnen. Volledige namen van later toegevoegde soorten worden herkend als ze expliciet in Beheer bestaan, ook als ze inactief zijn. Codes g, k, w, altaj en lege codes vereisen uitsluiting of een bewezen correctie.

Bij `3a3p` ontstaan twee kapteelten van elk drie bedden. Eén kapgewicht krijgt hierbij het label historisch/toegewezen en telt niet als zuiver soortlabel. Antonov/tutu bij één kapgewicht blijft ambigue. `95/65` blijft twee afzonderlijke gewichtsklassen, zonder verzonnen gemiddelde. Gehele-kaptotalen voor aantallen, uitval en alar blijven bij de bronrij; ze worden niet zonder verdeelsleutel aan beide soorten toegekend.

Grenzen van de generieke import: 32 MiB upload, 512 MiB uitgepakte werkboekinhoud, 50.000 datarijen per gekozen blad en de eerste 256 kolommen. De aangeleverde gespecialiseerde adapters hebben eigen begrensde verwerking. Gevonden werkbladen, voorbeeldregels en beperkingen worden getoond.

De import maakt geen KD10-/KD30-tussenmetingen aan die niet in de bron staan. Historische eindgewichten kunnen direct de historische baseline ondersteunen; zij zijn zonder echte tussenmetingen geen trainingsset voor het KD-Ridge-model.
