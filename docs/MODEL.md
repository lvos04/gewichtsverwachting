# Prognoses, seizoen en validatie

De referentiefout van ongeveer **3,8 g per tak** is een doel uit het bouwplan; er is geen gemeten verbetering op de bedrijfsgegevens aangetoond. Zonder eerdere betrouwbare oogsten blijft het gewicht onbekend. Met betrouwbare eerdere oogsten kan een historisch soortgemiddelde worden getoond; dit is herkenbaar als baseline en is geen gevalideerd KD-model.

## Tijd van het jaar en lichttrend

De Ridge-modellen gebruiken cyclische plantweek en de astronomische daglengte en verandering daarvan op het KD-meetmoment, met alleen toen beschikbare feiten bij een historische terugtest. Daardoor worden toenemend en afnemend daglicht onderscheiden. **Beheer → Algemene instellingen** stelt de breedtegraad in; voorlopig geldt 52° N als expliciete aanname. Ook voor het resterende traject tot de geplande oogst toont de app het gemiddelde en eindpunt van de astronomische daglengte. Rond een zonnewende toont de app het keerpunt. Dit zijn berekende daglengtes, geen bewolkingsvoorspelling of fotonmetingen in de kas.

Donker, gemiddeld en licht zijn toekomstige weerscenario’s. De app verzint zonder gekalibreerd model geen extra grammen uit één uur langer licht. Externe weerproviders staan uit. Handmatige scenario’s bewaren zowel uitgiftetijd als geldig-voor-tijd en registratietijd; terugtesten gebruikt alleen informatie die toen werkelijk beschikbaar was.

## Training en vrijgave

- Er zijn afzonderlijke modellen voor KD10 en KD30, elk met soortcategorie, werkelijke meetdag, gewicht, lengte, seizoenskenmerken en plantdichtheid; KD30 voegt de tweede meting en groeisnelheid toe.
- Oogstlabels tellen pas mee zodra ze geregistreerd zijn én de datum zonder tijdstip volledig voorbij is. Een later gecertificeerd soortlabel of gewijzigde categorie mag niet als vroegere trainingskennis gelden.
- Alleen betrouwbare, standaard kapteelten met complete tussenmetingen en een eenduidig eindgewicht tellen mee. Historische teelten zonder echte tussenmetingen trainen deze modellen niet.
- Minimaal 30 complete standaardkapteelten voor het basisprofiel (in Beheer te verhogen) zijn nodig voor een experiment. Dit is geen garantie dat het model goed genoeg is.
- De evaluatie groepeert op ronde en loopt vooruit in de tijd. Op de voorspeldatum moeten trainingslabels al geregistreerd zijn. Dezelfde ronde komt nooit aan beide kanten van een train/test-splitsing. Bij overlappende rondes geldt de vroegste invoerpeildatum van alle toetsrondes voor het bevroren model.
- Features komen uit alleen de planningversie en metingen die op de peildatum gemeten én geregistreerd waren. Latere oogst, werkelijk eindweer of latere planning komen er niet in.
- Model en historische baseline worden op exact dezelfde ongeziene kapteelten vergeleken. De historische 3,8 g is apart gemarkeerd als niet automatisch vergelijkbaar met deze evaluatieset.
- Vrijgave vergt verbetering op minimaal twee opeenvolgende volledig ongeziene rondes (in Beheer te verhogen) met één vooraf getraind model. Daarna activeert een beheerder het model bewust. Bij ontbreken van bewijs blijft het experimenteel.

De 80%-marge is empirisch; het is geen gegarandeerde 80%-dekking. Dekking wordt per ongeziene ronde berekend met een marge die zonder diens testlabels is bepaald. Modelresultaten vermelden aantallen, MAE, bias, foutverdeling, uitsplitsingen en dekking. Kleine aantallen kunnen instabiel zijn; operationele geschiktheid blijft een teeltinhoudelijke beoordeling.

Elke artifact bewaart datahash, featureversie, trainings-/evaluatie-ID’s, metrics en bestandshash. Prognosemomenten en bron-ID’s worden opgeslagen. Prognoses worden dagelijks en na geregistreerde feiten opnieuw afgeleid; training start de beheerder via **Prognoses → Nieuw model toetsen**. Modelbestanden worden uitsluitend lokaal geladen uit de eigen dataset, nooit van uploads of externe bronnen.

## Lichtkwaliteit

Ruwe klimaat-/lichtgegevens blijven beschikbaar ook als eenheden nog onbekend zijn. Kalibraties hebben een ingangsdatum en reden. Lamp-DLI vraagt bevestigde uren, PPFD, dimfactor, kanaalbetekenis en onafhankelijke bijdragen, zodat modulegroepen niet dubbel tellen. Buitenstraling in bevestigde J/cm² kan met bevestigde transmissie slechts een gemarkeerde benadering opleveren. Dagtotalen vermenigvuldigen met een percentage open schermuren is niet toegestaan. Kanaal 2 met PAR=0, onbekende doeken en ontbrekende modules blokkeren een geverifieerde totale DLI.

## Invoerprofielen via Beheer

| Profiel | Extra waargenomen invoer | Gegevensdrempel |
|---|---|---|
| Basis | Geen verplichte Priva-bronnen | Vanaf het ingestelde minimum, ten minste 30 complete standaardteelten |
| Licht | Gecontroleerde cumulatieve lamp-DLI tot de KD-meting | Standaard minimaal 60 complete teelten met passende bronnen |
| Klimaat | Lamplicht, etmaaltemperatuur, RV en werkelijk gemeten watergift per m² | Standaard minimaal 60 complete teelten met passende bronnen |

CO₂ is optioneel in het klimaatprofiel en vereist betrouwbare ppm-metingen. Een natuurlijke daglichtschatting is alleen optioneel na expliciete inschakeling en bevestiging van de eenheid, transmissie en schermvoorwaarden; deze blijft herkenbaar als **schatting**. Het minimum aan gegevensdekking is instelbaar tussen 90 en 100%, standaard 100%. Een gemeten nul voor water blijft nul; een ontbrekende meting blijft onbekend. Bij toegestane gedeeltelijke dekking toont de app dat sommen alleen de bekende dagen omvatten.

Elk model bewaart zijn eigen invoerprofiel en drempels. Een latere wijziging in Beheer verandert een bestaand model niet stilzwijgend. De uitgebreide profielen moeten op precies dezelfde toetskappen zowel het historische gemiddelde als een basis-Ridge verslaan. Is de invoer voor een klimaatmodel onvolledig, dan kan alleen een werkelijk beschikbaar eenvoudiger model of historisch gemiddelde worden gebruikt; de interface vermeldt die terugval.

Het kennispeilmoment en de waarnemingsperiode zijn verschillend. De actuele tijdlijn kan alle inmiddels ontvangen gegevens tonen, maar het KD10-model blijft zijn licht-/klimaatvenster bij de KD10-meting houden. Een dagtotaal van de meetdag verschijnt dus niet om 09:00 als reeds ontvangen daglicht. Alleen afgeronde lokale dagen worden verwerkt. Dagtotalen die een exacte meetgrens kruisen worden niet willekeurig over twee fasen verdeeld. Bronversies, intrekkingen, kalibraties, koppelingen en instellingen worden op hun geregistreerde tijdstip beoordeeld.

Prognosehistorie toont de werkelijk opgeslagen uitkomst, modelversie, peildatum en bronnen. De sterkste getoonde modelcoëfficiënten zijn statistische verbanden, geen bewezen effecten van extra licht, water of remstof.

## Controlekappen en gebeurtenissen

In Prognoses kunnen aangepaste en controlekappen binnen **dezelfde ronde en soort** beschrijvend worden vergeleken. Gewichten met ambigue soorttoewijzing of meerdere klassen blijven buiten de vergelijking. Een groepsverschil vergt ten minste vier afzonderlijke controlekappen; overlappende controle-/behandelde kappen en afgekapt bronmateriaal blokkeren het verschil. Voorspelresiduen gebruiken uitsluitend werkelijk vóór de oogst opgeslagen voorspellingen met een toen bestaand model; geen achteraf gemaakte voorspelling wordt als een proef vooraf gebruikt. Middelen, doses en eenheden blijven afzonderlijk.

Er is nog geen bewezen gewichtsrespons op toekomstige weerscenario’s of ingrepen. Een verschil tussen groepen is geen bewijs van een causaal interventie-effect en leidt niet tot automatische kas- of middelenaansturing.
