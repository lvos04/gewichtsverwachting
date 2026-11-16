# Prognoses, seizoen en validatie

De referentiefout van ongeveer **3,8 g per tak** is een doel uit het bouwplan; er is geen gemeten verbetering op de bedrijfsgegevens aangetoond. Zonder eerdere betrouwbare oogsten blijft het gewicht onbekend. Met betrouwbare eerdere oogsten kan een historisch soortgemiddelde worden getoond; dit is herkenbaar als baseline en is geen gevalideerd KD-model.

## Tijd van het jaar en lichttrend

De Ridge-modellen gebruiken cyclische plantweek en de op de prognosedatum bekende astronomische daglengte en verandering daarvan. Daardoor worden toenemend en afnemend daglicht onderscheiden. **Beheer → Algemene instellingen** stelt de breedtegraad in; voorlopig geldt 52° N als expliciete aanname. Ook voor het resterende traject tot de geplande oogst toont de app het gemiddelde en eindpunt van de astronomische daglengte. Rond een zonnewende toont de app het keerpunt. Dit zijn berekende daglengtes, geen bewolkingsvoorspelling of fotonmetingen in de kas.

Donker, gemiddeld en licht zijn toekomstige weerscenario’s. De app verzint zonder gekalibreerd model geen extra grammen uit één uur langer licht. Externe weerproviders staan uit. Handmatige scenario’s bewaren zowel uitgiftetijd als geldig-voor-tijd en registratietijd; terugtesten gebruikt alleen informatie die toen werkelijk beschikbaar was.

## Training en vrijgave

- Er zijn afzonderlijke modellen voor KD10 en KD30, elk met soortcategorie, werkelijke meetdag, gewicht, lengte, seizoenskenmerken en plantdichtheid; KD30 voegt de tweede meting en groeisnelheid toe.
- Alleen betrouwbare, standaard kapteelten met complete tussenmetingen en een eenduidig eindgewicht tellen mee. Historische teelten zonder echte tussenmetingen trainen deze modellen niet.
- Minimaal 30 complete standaardkapteelten (in Beheer te verhogen) zijn nodig voor een experiment. Dit is geen garantie dat het model goed genoeg is.
- De evaluatie groepeert op ronde en loopt vooruit in de tijd. Op de voorspeldatum moeten trainingslabels al geregistreerd zijn. Dezelfde ronde komt nooit aan beide kanten van een train/test-splitsing.
- Features komen uit alleen de planningversie en metingen die op de peildatum gemeten én geregistreerd waren. Latere oogst, werkelijk eindweer of latere planning komen er niet in.
- Model en historische baseline worden op exact dezelfde ongeziene kapteelten vergeleken. De historische 3,8 g is apart gemarkeerd als niet automatisch vergelijkbaar met deze evaluatieset.
- Vrijgave vergt verbetering op minimaal twee opeenvolgende volledig ongeziene rondes (in Beheer te verhogen) met één vooraf getraind model. Daarna activeert een beheerder het model bewust. Bij ontbreken van bewijs blijft het experimenteel.

De 80%-marge is empirisch; het is geen gegarandeerde 80%-dekking. Dekking wordt per ongeziene ronde berekend met een marge die zonder diens testlabels is bepaald. Modelresultaten vermelden aantallen, MAE, bias, foutverdeling, uitsplitsingen en dekking. Kleine aantallen kunnen instabiel zijn; operationele geschiktheid blijft een teeltinhoudelijke beoordeling.

Elke artifact bewaart datahash, featureversie, trainings-/evaluatie-ID’s, metrics en bestandshash. Prognosemomenten en bron-ID’s worden opgeslagen. Prognoses worden dagelijks en na geregistreerde feiten opnieuw afgeleid; training start de beheerder via **Prognoses → Nieuw model toetsen**. Modelbestanden worden uitsluitend lokaal geladen uit de eigen dataset, nooit van uploads of externe bronnen.

## Lichtkwaliteit

Ruwe klimaat-/lichtgegevens blijven beschikbaar ook als eenheden nog onbekend zijn. Kalibraties hebben een ingangsdatum en reden. Lamp-DLI vraagt bevestigde uren, PPFD, dimfactor, kanaalbetekenis en onafhankelijke bijdragen, zodat modulegroepen niet dubbel tellen. Buitenstraling in bevestigde J/cm² kan met bevestigde transmissie slechts een gemarkeerde benadering opleveren. Dagtotalen vermenigvuldigen met een percentage open schermuren is niet toegestaan. Kanaal 2 met PAR=0, onbekende doeken en ontbrekende modules blokkeren een geverifieerde totale DLI.

Priva-klimaat- en DLI-features zijn in deze eerste release nog geen invoer van het KD-Ridge-model. Er is nog geen bewezen gewichtsrespons op toekomstscenario’s, extra licht of alar. De app registreert gebeurtenissen en controlekappen, maar een verschil met de voorspelling is geen bewijs van een causaal interventie-effect.
