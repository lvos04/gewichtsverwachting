# Back-up en herstel

In **Beheer** kan een beheerder een volledige back-up maken en downloaden. De app gebruikt SQLite's backup-API, controleert de integriteit en voegt originelen, scans, modellen en een SHA256-manifest toe. Authenticatiegegevens maken deel uit van de volledige back-up; behandel dit bestand als privé. De afzonderlijke bedrijfs-CSV-export bevat geen wachtwoordhashes of sessies.

Automatische lokale back-ups worden dagelijks gemaakt zolang de app draait. Back-ups staan in `DATA_DIR/backups` en worden niet opnieuw in back-ups opgenomen. Bewaar kopieën buiten de server. Voor de meest volledige momentopname: wacht tot uploads/importen en trainingen afgerond zijn voordat u handmatig back-upt.

Herstellen gebeurt bewust offline naar een **nieuwe lege map**, zonder een draaiende database te overschrijven:

```bash
DATA_DIR=/tijdelijke/werkmap python -m app.backups restore /pad/chrysant-backup.zip /pad/chrysant-herstel
```

Daarbij controleert de app alle checksums, SQLite-integriteit en verwijzingen. Een foutieve back-up wordt niet over de doelmap gezet.

1. Maak vóór een update een volledige back-up en noteer de bijpassende imagetag.
2. Herstel die back-up naar een tijdelijke lege dataset/map.
3. Start dezelfde imagetag met `DATA_DIR`/mount naar die herstelmap en een andere poort. Gebruik tijdelijk geen externe tunnel naar deze proefinstantie.
4. Controleer accounts, teelten, vijf takken per meting, foto's en importstatus. Controleer of een herstartbare import de originele bestanden vindt.
5. Stop beide instanties voordat u eventueel de definitieve mount omschakelt. Start met dezelfde versie; nieuwere versies kunnen migreren.
6. Bij terugrollen naar een oudere image: herstel ook de bijpassende back-up. Een oudere image direct openen op een nieuwer schema is geen veilige rollback.

De tests voeren echte back-up en herstel naar een andere map uit en testen beschadigde inhoud. Dit vervangt geen periodieke hersteltest van de eigen productiedataset en TrueNAS-mountrechten.
