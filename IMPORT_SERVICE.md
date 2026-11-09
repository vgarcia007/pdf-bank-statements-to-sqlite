# Importdienst für Kontoauszüge

Stand: 9. November 2026

Dieses Projekt besitzt weiterhin allein die Kontoauszüge und SQLite-Datenbank.
Invoice 2 stellt eine Uploadoberfläche bereit und kommuniziert über eine interne
authentifizierte API. Es gibt keinen gemeinsamen Dateimount mit Invoice 2 und
keinen Docker-Socket. `paypalcsv2db.py` ist unverändert. `ing2db.py` erhielt am
9. November auf ausdrücklichen Betreiberwunsch eine gezielte Fußnotenerkennung
bei Beträgen (siehe unten).

## Dienste starten

Voraussetzungen: Docker-Netz `bank_import` (`docker network create --internal
bank_import`), bestehende `data/bank.sqlite3`/`metadata.yaml` sowie private
Konfiguration. UID/GID der Dienste ist 1000; Datenverzeichnis muss für diese UID
beschreibbar sein. Das Root-Verzeichnis der bisherigen Datenbank darf ihre Datei
atomar ersetzen lassen.

- `secrets/import-token`: zufälliger gemeinsamer API-Schlüssel (mindestens 32
  Zeichen), 0600. Invoice 2 verwendet denselben Wert als `BANK_IMPORT_TOKEN` und
  `BANK_IMPORT_URL=http://bank-importer:8080`. Niemals Schlüssel ausgeben/loggen.
- `data/import-service/accounts.json`: Liste von Objekten mit `type` (`ing` oder
  `paypal`) und `account` (exakte bestehende Kontobezeichnung). Keine beliebigen
  Kontonamen aus Uploads. ING-Konten verwenden seit 9. November vollständige IBANs.
- `import-control/`: beschreibbar für UID 1000, nur Importer/Viewer.

```sh
docker compose up -d importer viewer
docker compose ps
```

Das bisherige Parser-Image wird wiederverwendet. Ein Neubau erfolgt mit dem
bestehenden Dockerfile; nach einem Abhängigkeitsupdate Tests erneut ausführen.
`import_service/` wird read-only eingebunden. Die beiden laufenden Dienste sind:

- **importer**: HTTP-API auf internem Port 8080 und ein serieller Worker.
- **viewer**: Datasette auf bisherigem Host-Port 8123; ein eigener Startprozess
  öffnet Datasette nach Veröffentlichung einer neuen Datenbank erneut.

Invoice 2 liest Datasette ebenfalls über das interne Netz unter
`http://bank-datasette:8000/bank.json`. Dadurch hängt der Lesezugriff nicht vom
Host-Gateway-Routing ab; Port 8123 bleibt zusätzlich unverändert verfügbar.

`ingparser` ist nur noch ein optionaler Wartungsdienst im Profil `maintenance`
mit read-only Datenmount. Der alte Konsolencontainer mit Schreibmount wurde
entfernt. Keine direkten parallelen Konsolenimporte in die produktive Datenbank.

## Bedienung

Invoice 2: Konten → Kontoauszüge importieren. Originale ING-PDFs mit lesbarem
Text oder deutsche PayPal-CSV-Exporte, ein Konto/Format pro Auftrag, maximal 20
Dateien mit 5 MiB pro Datei und 20 MiB insgesamt. Nach Annahme läuft der Auftrag
unabhängig von Browser und Invoice 2 weiter. Keine automatische Rechnungszuordnung
nach einem Upload.

Auch über die Konsole dieselbe Warteschlange verwenden:

```sh
docker compose exec -T importer python /app/import_service/submit.py \
  --type paypal --account 'EXAKTE KONTOBEZEICHNUNG' --wait \
  /app/data/Beispiel.csv
```

Mehrere Pfade sind möglich. Ohne `--wait` wird nur die Annahme abgewartet.
Bei unklarer Verbindung zuerst die angezeigte Auftragskennung in der Historie
prüfen. Nicht automatisch einen zweiten Auftrag starten.

Identische Dateien werden mittels SHA-256 übersprungen. Die dauerhaften
Originalpfade sind inhaltsadressiert, weil der bestehende Buchungshash den
Dateipfad enthält. Mögliche Dubletten zwischen unterschiedlichen Exporten oder
gegenüber dem Bestand werden zurückgehalten (`review`). Keine stillschweigende
Löschung oder erzwungene Übernahme; Exportzeiträume fachlich prüfen.

## API

`Authorization: Bearer …`, JSON, kein öffentlich veröffentlichter Port:

- GET `/health`: Worker-Lebendigkeit, keine Zugangsdaten notwendig.
- GET `/v1/jobs`: letzte 50 Aufträge, Konten und `available`.
- GET `/v1/jobs/{id}`: einzelner Auftrag.
- POST `/v1/jobs`: `id` (32 Hexzeichen), `type`, `account`, `files` als Liste
  mit `name` und Base64-`data`. Dieselbe ID und identischer Inhalt liefern denselben
  Auftrag; abweichender Inhalt unter gleicher ID wird abgelehnt.

Status: `queued`, `processing`, `validating`, `publishing`, `completed`, `failed`,
`review`. Nur `completed` bestätigt eine Übernahme, `inserted` deren Anzahl.
`skipped` zählt bekannte Dateien. Pro Datei bedeutet `processed` lediglich
gelesen; bei späterem Fehler des Gesamtauftrags wird nichts übernommen.

## Datensicherheit und Wiederherstellung

Jeder Auftrag importiert ausschließlich in eine konsistente SQLite-Backup-Kopie.
Dateien werden einzeln mit den bestehenden Skripten aufgerufen. Zusätzlich werden
Prozessende, Registrierung, erwartete/gespeicherte Zeilenzahl, Konten, Datenbank-
Integrität und inhaltliche Dubletten geprüft. Ein Parseraufruf hat zehn Minuten
Zeitlimit. Rohes Parser-stdout/stderr wird wegen möglicher Bankinhalte verworfen.

Vor Veröffentlichung werden ausreichender freier Speicher und unveränderter
Quellbestand geprüft. Der Dienst legt eine Sicherung und ein fsync-gesichertes
Veröffentlichungsjournal an, ersetzt die SQLite-Datei atomar und fordert eine
neue Viewer-Generation an. Der Viewer bestätigt erst nach Neustart und erfolgreicher
Abfrage des neuen Bestands. Danach wird der Auftrag dauerhaft abgeschlossen.
Scheitert die Bestätigung, wird die vorherige Datenbank wiederhergestellt.

Nach Absturz wird eine unterbrochene Veröffentlichung über das Journal bereinigt.
Bereits dauerhaft abgeschlossene Aufträge bleiben erhalten. Unterbrochene reine
Verarbeitung wird fehlgeschlagen und nicht automatisch wiederholt. Eine ungeklärte
Recovery sperrt weitere Importe. Historie bleibt abrufbar; `available=false`
kennzeichnet einen nicht laufenden Worker. Keine Journaldateien blind löschen.
Nach Ursachenbehebung `docker compose restart importer` ausführen.

Private Ablage, vollständig außerhalb von Git:

- `data/import-service/jobs/`: vollständige dauerhafte Auftragshistorie
- `data/import-service/originals/`: dauerhafte Originaldateien
- `data/import-service/backups/`: bis zu zehn Rückfallsicherungen nach erfolgreichem Lauf
- `data/import-service/publication.json`: nur während Veröffentlichung/Recovery
- `data/import-service/accounts.json`: Kontenfreigaben
- `import-control/`, `secrets/`

Originale/Historie werden derzeit nicht automatisch gelöscht. Speicher überwachen.
Lokale Rückfallsicherungen sind kein externes Backup: Bankdatenbank, private
Ablage, Metadaten, Schlüssel und Projektdateien gemeinsam separat sichern.
Das Invoice-2-Vollbackup umfasst dieses eigenständige Projekt nicht.

## Tests

```sh
docker compose run --rm --no-deps \
  -v "$PWD/import_service:/app/import_service:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  ingparser python /app/tests/test_import_service.py
```

Alle Testdaten entstehen unter `/tmp` des Containers. `compose.test.yaml` stellt
zusätzlich einen vollständig getrennten Importer/Viewer für HTTP-/Browsertests
bereit, ohne produktive Mounts oder Host-Ports. `BANK_IMPORT_TEST_DIR` auf ein
leeres temporäres Verzeichnis setzen, zuvor mit `tests/create_fixture.py` im
Parsercontainer unter `/fixture` initialisieren. Niemals das echte `data/`
als Testverzeichnis verwenden. Testdienste nachher mit derselben Compose-Datei
und Variable wieder herunterfahren.

Die Invoice-2-Seite und deren Tests sind im dortigen `docs/BANK_IMPORTS.md`
beschrieben. Das ist Entwicklungsdokumentation, keine Laufzeitabhängigkeit.

## Korrektur verkürzter IBANs am 9. November 2026

Die ursprüngliche IBAN-Erkennung erfasste bei gruppierten deutschen IBANs nur
20 Zeichen. Der vorgeschaltete `check_pdf.py` prüft jetzt die vollständigen
22 Zeichen einschließlich Modulo 97 und exaktem Vergleich zum ausgewählten
Konto. Der ursprüngliche Parser erhält die volle Kennung mit `--account`; seine
IBAN-Erkennung bleibt unverändert. Direkte Parseraufrufe ohne dieses Argument behalten
die ursprüngliche Erkennung. Konsole über `submit.py` verwenden.

Das Wartungswerkzeug korrigiert ausschließlich das Feld `account`. IDs,
`tx_hash`, Beträge, Texte, Buchungstage, Quellen und Dateiregistrierungen bleiben
erhalten. Quellhashes nicht nachträglich neu berechnen: Angebundene Anwendungen
können bereits bestätigte Verweise darauf besitzen. Invoice 2 erhält die
Rückwärtskompatibilität seiner inhaltlichen Fingerprints in `AccountIdentifier`.

Private Dateien: `account-aliases.json` für bestätigte Alt-/Neu-Kennungen,
`account-repair-mapping.json` als ausdrücklich freigegebene Wartungszuordnung,
`repairs/{id}/` mit Datenbanksicherung/Bericht. Alles unter `data/import-service/`,
außerhalb von Git. Keine willkürliche IBAN-Ergänzung durch Präfixraten.

Das Wartungswerkzeug verweigert die Ausführung bei laufendem Worker, offenen
Aufträgen oder externer Datenbankänderung. Vorschau schreibt keine Daten.
Anwendung mit Sicherung, Vergleich jeder Zeile (nur Kontoumbenennung erlaubt),
atomarem Austausch, Konfigurationskorrektur und bestätigtem Viewer-Neustart:

```sh
docker compose stop importer
docker compose run --rm --no-deps importer python /app/import_service/repair_accounts.py
docker compose run --rm --no-deps importer python /app/import_service/repair_accounts.py --apply
docker compose up -d importer
```

`account-repair.json` ist ein eigenes Absturzjournal. Bei Unterbrechung blockiert
der Worker weitere Importe. Denselben Wartungsaufruf mit `--apply` bei angehaltenem
Importer erneut starten: Zuerst wird die Sicherung wiederhergestellt, dann die
Korrektur erneut ausgeführt. Bei regulärem Viewerfehler erfolgt die Rücknahme
sofort. Journal nicht blind löschen. Alte Sicherungen bleiben historische
Rückfallsicherungen und werden nicht umgeschrieben.

Zusatztest im Container: `python /app/tests/test_account_repair.py` (fünf Prüfungen).

## ING-Beträge mit hochgestellten Fußnotenziffern

Am 9. November 2026 fiel ein Upload wegen unterschiedlicher gelesener und
gespeicherter Buchungsanzahlen aus. Ursache war eine echte Überweisung, deren
Betrag beim PDF-Textlesen direkt mit einer hochgestellten Fußnotenziffer verklebt
wurde. Der bisherige Parser erkannte den Betrag nicht. Auch innerhalb einer
Datei konnte dadurch eine Buchung in die vorherige Beschreibung geraten.

Auf ausdrücklichen Wunsch wurde diese Erkennung direkt im ING-Parser korrigiert.
`extract_ing_page_text` nutzt die Schriftgröße der Textläufe: Nur eine nach einem
Betrag folgende deutlich kleinere Ziffer und eine eindeutig übereinstimmende
Anzahl am Zeilenende gelten als Fußnotenmarker. Die ursprünglichen PDF-Dateien
bleiben bytegleich; keine bereinigten PDF-Kopien. Normale zusätzliche Ziffern
werden nicht entfernt. Alle weiteren Parserregeln bleiben erhalten.

Ein nicht lesbarer Betrag bricht die Vorprüfung mit einer eigenen Meldung ab.
Die Mengenprüfung bleibt bestehen und nennt jetzt gelesene/gespeicherte Anzahl,
statt einen Mengenfehler pauschal als mögliche Dublette zu erklären. Echte
Dubletten und Überlappungen bleiben weiterhin geschützt.

Parserdateien werden für Importer und Konsolen-Wartungsdienst jetzt zusätzlich
read-only eingebunden; der Dockerfile kopiert sie weiterhin für Neubauten ins
Image. Nach erstmaligem Hinzufügen der Mounts nur den Importer neu erstellen.
`compose.test.yaml` verwendet dieselben Parser-Mounts.

Sechs synthetische Regressionen unter `tests/test_ing_footnotes.py` prüfen die
Erkennung, direkten CLI-Import, Original-Prüfsummen, normale Zusatzstellen,
uneindeutige Schriftbelege und Ablehnung eines weiterhin unlesbaren Betrags.

Nach einer Parserkorrektur einen fehlgeschlagenen Auftrag zuerst gegen eine
isolierte Datenbankkopie prüfen. Anschließend kann er über die normale API als
neuer Auftrag eingereicht werden. Der frühere Fehlerauftrag bleibt in der
Historie erhalten. Bestehende Bankzeilen und Original-Prüfsummen kontrollieren.

Abnahme am 7. November 2026: 20 Container-Fach-/Recoverytests, 19 Invoice-2-
HTTP-/API-Prüfungen und Desktop-/Mobilbrowserprüfung bestanden. Beide echten
Parser mit synthetischen Daten getestet, einschließlich Veröffentlichung und
Viewer-Neustart. Die automatisierten Tests verwenden synthetische Daten und
getrennte Testdatenbanken.
