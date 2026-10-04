# Zeitspur

Zeitspur ist ein lokaler, verschlüsselter Aktivitätsverlauf für Windows – nach dem Vorbild von „Windows Recall“:
Ein Tray-Dienst macht in regelmäßigen Abständen Screenshots aller Monitore, erkennt den sichtbaren Text per OCR
und speichert alles in **einer** AES-256-verschlüsselten SQLCipher-Datenbank. Ein Zeitstrahl-Fenster zeigt, was
wann geöffnet war; ein lokaler MCP-Server erlaubt Fragen wie „Was habe ich letzten Mittwoch um 12 Uhr gemacht?“
direkt in Claude. Nach 14 Tagen (konfigurierbar) werden Einträge automatisch gelöscht.

Alles bleibt auf dem Rechner. Es gibt keinen Netzwerkdienst, keinen offenen Port und keine Cloud.
Ausnahmen sind die Plugins, die Sie ausdrücklich hinzufügen: Outlook (lokal per COM), Teams und die Standort-Historie (beide zu Ihren eigenen Servern bzw. Ihrem Mandanten), die standardmäßig ausgeschaltete Karte, die Kacheln von OpenStreetMap lädt, und die [Update-Prüfung](#updates) der Release-Ausgabe: Sie fragt bei GitHub nach, ob es eine neue Version gibt – von Ihren Daten wird dabei nichts übertragen.

## Inhalt

1. [Funktionsweise](#funktionsweise)
2. [Installation](#installation)
3. [Ersteinrichtung](#ersteinrichtung)
4. [Bedienung: Tray-Menü und Zeitstrahl](#bedienung-tray-menü-und-zeitstrahl)
5. [Konfiguration](#konfiguration)
6. [Claude anbinden (MCP-Server)](#claude-anbinden-mcp-server)
7. [Datenschutz und Sicherheit](#datenschutz-und-sicherheit)
8. [Performance und Speicherbedarf](#performance-und-speicherbedarf)
9. [Updates](#updates)
10. [Deinstallation](#deinstallation)
11. [Fehlerbehebung](#fehlerbehebung)
12. [Entwicklung und Build](#entwicklung-und-build)
13. [Lizenz](#lizenz)

## Funktionsweise

| Komponente | Datei | Aufgabe |
|---|---|---|
| Hintergrunddienst | `Zeitspur.exe` | Tray-Symbol, Screenshots, OCR, Aufräumen, Zeitstrahl-Fenster |
| MCP-Server | `Zeitspur.exe --mcp` (eigener Prozess) | wird von Claude bei Bedarf gestartet, liest die Datenbank nur |
| Datenbank | `%LOCALAPPDATA%\Zeitspur\zeitspur.db` | SQLCipher (AES-256), enthält Metadaten, OCR-Text (FTS5-Index) und die Screenshots als BLOBs |
| Schlüssel | `%LOCALAPPDATA%\Zeitspur\key.bin` | zufälliger 256-Bit-Schlüssel, per Windows-DPAPI an Ihr Benutzerkonto gebunden |
| Konfiguration | `%LOCALAPPDATA%\Zeitspur\config.yaml` | alle Einstellungen, siehe unten |
| Protokolle | `%LOCALAPPDATA%\Zeitspur\logs\` | `service.log`, `mcp.log` (ohne Bildschirmtexte oder Fenstertitel) |

Ablauf einer Aufnahme: alle *N* Sekunden (Standard 5) wird jeder Monitor fotografiert. Hat sich das Bild gegenüber
dem zuletzt gespeicherten Frame kaum verändert (Standard: weniger als 1,5 % der Bildpunkte), wird nur die Enddauer
des laufenden Eintrags verlängert. Neue Frames werden verkleinert, als WebP komprimiert, verschlüsselt gespeichert
und an eine OCR-Warteschlange übergeben. Die Texterkennung läuft mit niedriger Priorität in einem eigenen
Thread und übergibt das Bild ausschließlich im Arbeitsspeicher an das mitgelieferte `tesseract.exe`
(stdin/stdout) – es liegt zu keinem Zeitpunkt eine unverschlüsselte Bild- oder Temp-Datei auf der Platte.

Nicht aufgenommen wird bei gesperrtem Bildschirm, nach 3 Minuten ohne Maus-/Tastatureingaben, bei manueller
Pause, bei zu wenig freiem Speicherplatz sowie wenn das Vordergrundfenster auf der Ausschlussliste steht.

## Installation

Voraussetzungen: Windows 10 (1809) oder 11, 64 Bit. Keine Adminrechte nötig. Fehlt die Microsoft Edge
WebView2-Runtime (auf Windows 11 normalerweise vorhanden, nicht aber z. B. in der Windows Sandbox), lädt das
Setup sie von Microsoft nach – mit eigener Fortschrittsseite und ebenfalls ohne Adminrechte.

1. Das Setup der neuesten Version herunterladen: <https://github.com/Tim-Schaller/Zeitspur/releases/latest>
   (`ZeitspurSetup-<Version>.exe`) und starten. Die Installation erfolgt pro Benutzer nach
   `%LOCALAPPDATA%\Programs\Zeitspur\`. Danach hält sich Zeitspur selbst aktuell (siehe [Updates](#updates)).
2. Optionen im Setup:
   * **Autostart** (vorausgewählt): legt den Wert `Zeitspur` unter
     `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` an, der `Zeitspur.exe --autostart` startet.
     Lässt sich jederzeit in der App umschalten (siehe [Automatisch mit Windows starten](#automatisch-mit-windows-starten)).
   * **MCP-Server in Claude Desktop registrieren** (optional): ergänzt `%APPDATA%\Claude\claude_desktop_config.json`
     um den Eintrag `Zeitspur` (Sicherungskopie `.bak`). Siehe auch [Claude anbinden](#claude-anbinden-mcp-server).
3. Nach dem Setup startet Zeitspur: Sofort erscheint ein kleines Startfenster mit laufendem Balken (der erste
   Start dauert einige Sekunden), danach das Fenster mit der Ersteinrichtung.

Startmenü-Einträge: „Zeitspur“ (öffnet den Zeitstrahl) und „Zeitspur deinstallieren“.

Hinweis: Die Programmdateien sind nicht signiert. Windows SmartScreen kann beim ersten Start des Setups warnen
(„Weitere Informationen“ → „Trotzdem ausführen“).

## Ersteinrichtung

Beim ersten Start öffnet sich das Fenster mit einem Formular: Aufnahmeintervall, Aufbewahrungsdauer,
Speicherort der Datenbank, Bildbreite/Qualität, OCR-Sprachen und Ausschlusslisten. Mit „Speichern und
starten“ wird die Konfiguration geschrieben, der DPAPI-Schlüssel erzeugt und die Datenbank angelegt.
Danach beginnt die Aufnahme; das Fenster kann geschlossen werden – der Dienst läuft im Tray weiter.

Alle Werte lassen sich später über das Zahnrad-Symbol im Fenster oder direkt in `config.yaml` ändern.

### Automatisch mit Windows starten

Der Autostart lässt sich jederzeit umschalten – **ohne Neuinstallation und ohne Adminrechte**:

* **Tray-Menü → „Mit Windows starten“** (Häkchen zeigt den Zustand), oder
* **Einstellungen (⚙) → Abschnitt „Start“ → „Automatisch mit Windows starten“**.

Beides wirkt sofort. Technisch wird der Wert `Zeitspur` unter
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` gesetzt bzw. entfernt – nur für Ihr Benutzerkonto.
Beim Anmelden startet dann `Zeitspur.exe --autostart`: Das Fenster bleibt versteckt, nur das
Tray-Symbol erscheint, und Aufnahme und Sync setzen kurz verzögert ein, damit die Anmeldung nicht ausgebremst wird.

Der Zustand steht bewusst **nicht** in `config.yaml`, sondern in der Registry: Denselben Eintrag setzt auch der
Installer, und Windows selbst zeigt ihn im Task-Manager unter „Autostart“, wo er ebenfalls abgeschaltet werden
kann. Eine zweite Kopie in der Konfiguration würde nur auseinanderlaufen. Zeigt der Eintrag nach einer
Neuinstallation in einen anderen Ordner auf eine alte Programmdatei, korrigiert Zeitspur ihn beim nächsten
Start automatisch.

## Bedienung: Tray-Menü und Zeitstrahl

**Tray-Symbol**: ein Ring („Zeitachse“) mit Aufnahmepunkt. Die **Farbe zeigt den Zustand** (grün Aufnahme, gelb
pausiert, grau gesperrt/inaktiv, blau ausgeschlossene App, rot Fehler/kein Speicherplatz), und **während der
Aufnahme läuft das helle Ringsegment um** – steht es still, wird gerade nichts aufgezeichnet. In jedem anderen
Zustand bleibt das Symbol bewusst ruhig. Die Einzelbilder sind vorberechnet und werden in genau der Größe
gezeichnet, in der Windows das Symbol anzeigt; die Animation kostet keine messbare Rechenzeit.

Rechtsklick öffnet das Menü:

| Eintrag | Wirkung |
|---|---|
| Zeitstrahl öffnen | zeigt das Fenster (auch Doppelklick auf das Symbol) |
| Aufnahme pausieren | Privacy-Pause; sofort wirksam, Häkchen zeigt den Zustand |
| Aktuelle App ausschließen | trägt den Prozess des aktiven Fensters in die Ausschlussliste ein |
| Letzte 15 Minuten löschen | entfernt alle Einträge der letzten 15 Minuten inklusive Bilder |
| Status / Statistik | Zustand, Einträge heute, Datenbankgröße, offene OCR-Jobs |
| Mit Windows starten | schaltet den Autostart um (Häkchen = aktiv), sofort wirksam |
| Log-Ordner öffnen | Explorer im Protokollordner |
| Datenbank komprimieren | vollständiges VACUUM (nur nötig, wenn Sie Platz sofort zurückhaben wollen; das tägliche Aufräumen gibt Platz ohnehin frei) |
| Beenden | stoppt Aufnahme, OCR und Wartung, schließt die Datenbank sauber |

**Zeitstrahl-Fenster**

* Kopfzeile: Datum (◀ ▶, Kalender, „Heute“), Volltextsuche, Statusanzeige, Pause-Schalter, Einstellungen (⚙).
* Zeitstrahl: 24-Stunden-Achse, je Monitor eine Spur. Aufeinanderfolgende Aufnahmen derselben App mit
  gleichem Fenstertitel bilden einen farbigen **Aktivitätsblock**. Beschriftet wird er mit dem
  **Fenstertitel** (was Sie getan haben), das Programm steht klein darunter.
  Die Farbe kennzeichnet das Programm und stammt aus **dessen eigenem Symbol** – Explorer gold,
  Teams blaulila, Claude dunkelorange. Zeitspur liest das Symbol aus der EXE, bestimmt die
  kräftigste Farbe darin und bringt sie in ein einheitliches Helligkeitsband, damit die Beschriftung
  lesbar bleibt. Für Programme ohne farbiges Symbol (reine Graustufen-Symbole) greift eine feste
  Palette, danach ein neutraler Grauton.

  Markenfarben ähneln sich allerdings oft – unter den Microsoft-Programmen sind gleich vier Blautöne.
  Deshalb prüft Zeitspur jede neue Farbe gegen die bereits vergebenen und weicht bei Bedarf aus:
  zuerst über die Helligkeit (helleres Blau bleibt Blau), erst danach mit einer kleinen Drehung des
  Farbtons. Gemessen wird in OKLab, zusätzlich unter simulierter Rot-Grün-Schwäche; Ziel ist ein
  Abstand ΔE ≥ 12. Die meistgenutzten Programme kommen zuerst dran und behalten ihre Farbe daher
  unverändert. Mausrad zoomt um den Mauszeiger,
  Ziehen verschiebt den Ausschnitt, Doppelklick zeigt den ganzen Tag. Ein weißer Punkt markiert Blöcke,
  deren Texterkennung noch läuft; die rote Linie ist „jetzt“.
* Klick auf einen Block öffnet die Details: Screenshot (wird erst jetzt aus der Datenbank entschlüsselt und
  direkt ans Fenster übergeben), App, Fenstertitel, Zeitraum, erkannter Text. Mit ◀ ▶ (oder Pfeiltasten)
  blättern Sie durch die Einzelbilder, „Diesen Eintrag löschen“ entfernt genau diese Aufnahme.
* Suche: ab zwei Zeichen wird der OCR-Text und der Fenstertitel aller Tage durchsucht (Präfixsuche,
  Umlaute egal: „muller“ findet „Müller“, Phrasen in Anführungszeichen). Ein Klick auf einen Treffer springt
  zum passenden Tag und Block; Treffer werden im Text markiert.

Das Fenster nutzt die WebView2-Runtime lokal (kein HTTP-Server, kein Port); Bilder werden als Daten-URIs
übergeben und landen nicht im Browser-Cache auf der Platte.

## Konfiguration

Datei: `%LOCALAPPDATA%\Zeitspur\config.yaml` (YAML). Änderungen über das Einstellungsfenster wirken sofort,
mit Ausnahme der gekennzeichneten Werte; manuelle Änderungen an der Datei greifen beim nächsten Start.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `capture_interval_seconds` | `5` | Abstand zwischen zwei Aufnahmeversuchen (2–60 s) |
| `idle_pause_minutes` | `3` | Pause ohne Eingaben nach so vielen Minuten |
| `retention_days` | `14` | Einträge älter als so viele Tage werden täglich gelöscht |
| `max_image_width` | `1920` | Screenshots werden auf diese Breite verkleinert (640–7680) |
| `webp_quality` | `75` | WebP-Qualität (30–95) |
| `db_path` | `%LOCALAPPDATA%/Zeitspur/zeitspur.db` | Speicherort der Datenbank (Neustart nötig) |
| `excluded_process_regex` | `KeePass.*`, `.*Banking.*` | Prozessnamen (Regex, Groß-/Kleinschreibung egal), bei denen nicht aufgenommen wird |
| `excluded_title_regex` | `.*Inkognito.*`, `.*Private Browsing.*`, `.*InPrivate.*`, `.*Privates Fenster.*` | Fenstertitel-Muster, bei denen nicht aufgenommen wird |
| `ocr_language` | `deu+eng` | Tesseract-Sprachen (Neustart nötig; Sprachdateien liegen in `tesseract\tessdata`) |
| `change_threshold` | `0.015` | Anteil geänderter Bildpunkte, ab dem ein Frame als neu gilt (0.01 = 1 %) |
| `max_block_minutes` | `10` | spätestens danach wird auch ohne Änderung ein neues Bild gespeichert |
| `ocr_psm` | `3` | Tesseract-Seitensegmentierung (3 automatisch, 11 verstreuter Text; Neustart nötig) |
| `ocr_min_confidence` | `40` | Wörter mit geringerer Konfidenz (%) werden verworfen (Neustart nötig) |
| `ocr_queue_max` | `20` | maximale Anzahl wartender OCR-Jobs im Speicher; Überschuss wird später nachgeholt |
| `max_db_size_gb` | `20` | Größendeckel; darüber werden die ältesten Tage zuerst gelöscht |
| `min_free_disk_gb` | `2` | unterhalb dieses freien Speichers pausiert die Aufnahme |
| `log_level` | `INFO` | `DEBUG` protokolliert deutlich mehr (Neustart nötig) |
| `installed_plugins` | `[]` | hinzugefügte Plugins (`outlook`, `teams`, `teams_local`, `dawarich`); verwaltet über Einstellungen → Plugins (siehe Plugins) |
| `teams_user_id` | `""` | Teams-Plugin: Azure-AD-Objekt-Id des Nutzers → nur **eigene** Anrufe (leer ⇒ Teams-Sync wird übersprungen) |
| `teams_user_names` | `[]` | Teams-Plugin: Anzeigenamen als Rückfall, falls in einem Anruf keine Objekt-Id, aber ein Name steht |
| `map_enabled` | `false` | Karte im Zeitstrahl; **einzige** Stelle, die Daten aus dem Internet lädt |
| `map_tile_url` | OSM | Kachel-Adresse (https, mit `{z}/{x}/{y}`); auf eigenen Server umbiegbar |
| `map_home_lat` / `map_home_lon` | Mitte Deutschlands | Startpunkt der Karte, wenn nichts anzuzeigen ist |
| `map_home_zoom` | `6` | Zoomstufe des Startpunkts (1–19) |
| `map_home_label` | `""` | Name des Startpunkts (etwa „Büro“); mit Namen erscheint er als Bezugspunkt und zählt als bekannter Ort |
| `known_places` | `[]` | weitere benannte Orte `Name;Breite;Länge[;Radius]`, je Zeile |
| `events_sync_minutes` | `15` | Intervall der Termin-/Anruf-Synchronisierung |
| `events_window_days` | `14` | Tage rückwärts/vorwärts, die synchronisiert werden (passt zur Aufbewahrung) |

Die Ausschlusslisten prüfen nur das **Vordergrundfenster**. Ein Passwortmanager, der auf einem zweiten Monitor
sichtbar ist, während Sie in einem anderen Programm arbeiten, wird trotzdem aufgenommen – nutzen Sie dafür die
Pause oder „Letzte 15 Minuten löschen“.

## Claude anbinden (MCP-Server)

Der MCP-Server (stdio-Transport) steckt in derselben Programmdatei und wird mit
`Zeitspur.exe --mcp` gestartet. Er läuft nicht dauerhaft, sondern wird von Claude bei Bedarf
gestartet, öffnet die Datenbank **nur lesend** und entschlüsselt den Schlüssel wie der Dienst per DPAPI
(funktioniert daher nur unter Ihrem Windows-Konto). Der `--mcp`-Prozess ist unabhängig vom laufenden
Tray-Dienst; beide dürfen gleichzeitig laufen.

> Hintergrund: Ursprünglich war eine separate `ZeitspurMCP.exe` vorgesehen. Auf dem Entwicklungsrechner
> hat der Virenscanner diese unsignierte EXE als Fehlalarm entfernt, weshalb der Server in die
> Dienst-EXE integriert wurde (Details unter „Entwicklung und Build“). Wer die separate EXE möchte, baut mit
> `$env:ZEITSPUR_BUILD_MCP_EXE=1`; sie wird dann ebenfalls unterstützt.

**Claude Desktop** – entweder die Setup-Option „MCP-Server in Claude Desktop registrieren“ wählen, später

```bash
"%LOCALAPPDATA%\Programs\Zeitspur\Zeitspur.exe" --mcp --register-claude-desktop
```

ausführen oder `%APPDATA%\Claude\claude_desktop_config.json` von Hand ergänzen (Pfad anpassen):

```json
{
  "mcpServers": {
    "Zeitspur": {
      "command": "C:\\Users\\<Benutzer>\\AppData\\Local\\Programs\\Zeitspur\\Zeitspur.exe",
      "args": ["--mcp"]
    }
  }
}
```

Claude Desktop danach neu starten.

**Claude Code** (PowerShell):

```bash
claude mcp add Zeitspur -- "$env:LOCALAPPDATA\Programs\Zeitspur\Zeitspur.exe" --mcp
```

`Zeitspur.exe --mcp --print-config` zeigt beide Schnipsel mit dem tatsächlichen Pfad an
(in einer Konsole ausführen; als Fenster-Programm öffnet es sonst ein Meldungsfenster).

Bereitgestellte Werkzeuge:

| Werkzeug | Zweck |
|---|---|
| `get_time_context()` | aktuelle Zeit, Zeitzone, Wochentage der letzten 7 Tage, Umfang der Daten – löst „gestern“, „letzten Mittwoch“ auf |
| `search_activity(query, from_date?, to_date?, limit=20)` | Volltextsuche über OCR-Text und Fenstertitel, zeitlich einschränkbar |
| `get_activity_at(timestamp, window_minutes=15)` | alle Einträge rund um einen Zeitpunkt, gruppiert in Aktivitätsblöcke, mit Text |
| `list_active_apps(day)` | Tagesübersicht: welche Programme/Fenster wie lange aktiv waren |
| `get_entry(entry_id)` | vollständiger Text und Metadaten eines Eintrags |
| `get_screenshot(entry_id, max_width=1280)` | Screenshot als JPEG (im Speicher entschlüsselt) |
| `get_activity_at(...)` → `location` | wo der Nutzer war (benannter Ort oder Koordinaten) – für „Wo war ich um 9 Uhr?“ |
| `get_calendar(day)` | Outlook-Termine und Teams-Anrufe eines Tages (Betreff, Zeit, Dauer, Ort, Teilnehmer) |

`get_activity_at` und `list_active_apps` enthalten die passenden Termine/Anrufe zusätzlich unter dem Schlüssel
`calendar`, sodass Claude Bildschirmaktivität direkt einer Besprechung oder einem Anruf zuordnen kann.

Beispielfragen an Claude: „Was habe ich gestern um 12 Uhr gemacht?“, „Wann hatte ich zuletzt das Angebot für
Kunde XY offen?“, „Welche Programme habe ich am Dienstag am längsten benutzt?“, „Zeig mir den Screenshot zu
Eintrag 4711.“ Zeitstempel aus `get_activity_at` lassen sich in derselben Unterhaltung mit anderen Quellen
(z. B. einem Plaud-Connector) abgleichen.

## Plugins: Outlook, Teams und Standort

Termine, Anrufe und Orte kommen aus **Plugins**. Alle drei sind im Programm enthalten, aber keines ist aktiv,
bevor Sie es hinzufügen: **Einstellungen (⚙) → Plugins → Hinzufügen**. Jedes Plugin bringt dort seine eigene
Einrichtung mit (Zugangsdaten, Einstellungen, „Speichern & testen“, „Verbindung testen“).

**Entfernen** räumt vollständig auf: Die gespeicherten Zugangsdaten und alle Ereignisse dieses Plugins werden
gelöscht. Fügen Sie es später wieder hinzu, holt die nächste Synchronisierung die Ereignisse des Sync-Fensters
aus der Quelle zurück. Bildschirmaufnahme, Texterkennung, Datenbank und Zeitstrahl sind keine Plugins.

Wer von einer älteren Version kommt, verliert nichts: Die früheren Schalter `outlook_enabled`, `teams_enabled`
und `dawarich_enabled` werden beim ersten Start in `installed_plugins` übernommen.

Plugins lassen sich bewusst **nicht** aus fremden Dateien nachladen: Ein Plugin läuft im selben Prozess, der den
Datenbankschlüssel hält, und könnte alles lesen. Ein neues Plugin wird in `zeitspur/plugins.py` eingetragen;
die Einstellungsseite baut sich aus seiner Selbstbeschreibung, ohne dass die Oberfläche angepasst werden muss.

Termine und Anrufe erscheinen als eigene Marker-Spur „Termine“ oben im Zeitstrahl und stehen über den
MCP-Server (`get_calendar` sowie das Feld `calendar` in `get_activity_at`/`list_active_apps`) bereit. Beide
Quellen werden verschlüsselt in derselben Datenbank gespeichert und nach `retention_days` mitgelöscht. Die
Synchronisierung läuft alle `events_sync_minutes` für ein Fenster von ±`events_window_days` um heute; beim
Blättern in einen weiter zurückliegenden Tag wird dieser bei Bedarf zusätzlich nachgeladen.

### Outlook-Kalender

Plugin `outlook`. Zeitspur liest die Termine über das lokale COM-Objekt
`Outlook.Application` aus dem **klassischen** Outlook (Outlook Object Model), das denselben Postfach-Account
nutzt wie das neue Outlook. Es ist keine zusätzliche Anmeldung nötig. Voraussetzung ist ein installiertes
klassisches Outlook (Microsoft 365 Apps / Office). Läuft nur das neue Outlook (`olk.exe`) ohne klassische
Installation, steht COM nicht zur Verfügung und die Kalenderspur bleibt leer.

Zu jedem Termin werden Betreff, Zeitraum, Ort, Organisator und Teilnehmer gespeichert; Besprechungen und
einfache Termine werden unterschieden. Ist kein klassisches Outlook installiert, lässt sich das Plugin nicht
hinzufügen; die Einstellungsseite nennt dann den Grund.

### Teams-Anrufe (Microsoft Graph)

Plugin `teams`. Anrufdauer und Teilnehmer kommen aus `communications/callRecords` der Microsoft-Graph-API.
Das erfordert eine einmalige Einrichtung durch einen Administrator Ihres Microsoft-365-Tenants:

1. Im Azure-Portal (Microsoft Entra ID) unter „App-Registrierungen“ eine neue App anlegen (Single Tenant).
2. Unter „API-Berechtigungen“ die **Anwendungsberechtigung** (nicht delegiert) `CallRecords.Read.All`
   für Microsoft Graph hinzufügen und **Administratorzustimmung erteilen**.
3. Unter „Zertifikate & Geheimnisse“ ein Client-Secret erzeugen.
4. Tenant-Id (Verzeichnis-Id), Client-Id (Anwendungs-Id) und das Client-Secret notieren.

In Zeitspur dann: Einstellungen (⚙) → Plugins → Microsoft Teams → „Hinzufügen“, die drei Werte und Ihre
Objekt-Id eintragen und „Speichern & testen“ wählen. Das Client-Secret
wird per DPAPI geschützt in `%LOCALAPPDATA%\Zeitspur\teams_credentials.bin` abgelegt, nicht im Klartext.
Der Client-Credentials-Flow (App-only) benötigt keine Benutzeranmeldung.
Die Eingabefelder für Geheimnisse werden nie vorbefüllt; der Knopf **„Anzeigen“** holt die gespeicherten
Werte wieder hervor, falls Sie sie anderswo verloren haben. Das ist kein Sicherheitsverlust: Die Daten
liegen DPAPI-geschützt und an Ihr Windows-Konto gebunden – wer an der entsperrten Sitzung sitzt, käme
ohnehin an sie heran. Ohne diesen Weg wären sie nur schreibbar und dauerhaft unerreichbar. Microsoft hält `callRecords` nur
etwa 30 Tage vor; Zeitspur fragt daher höchstens die letzten 30 Tage ab. Ohne erteilten Admin-Consent
schlägt der Verbindungstest mit einer Graph-Fehlermeldung fehl.

**Nur die eigenen Anrufe.** `CallRecords.Read.All` liefert grundsätzlich *alle* Anrufe des gesamten Tenants.
Damit Zeitspur ausschließlich Ihre eigenen ein- und ausgehenden Anrufe zeigt, muss Ihre **Azure-AD-Objekt-Id**
in `teams_user_id` hinterlegt sein (Entra-Portal → Benutzer → Ihr Profil → „Objekt-ID“). Diese Id lässt sich mit
der reinen `CallRecords.Read.All`-Berechtigung **nicht** automatisch aus der E-Mail-Adresse ermitteln (der
Verzeichnis-Lesezugriff `GET /users` erfordert eine zusätzliche Berechtigung und liefert sonst `403`). Als
Rückfall dient `teams_user_names` (eine Liste von Anzeigenamen wie `"Mustermann, Max"`), falls einmal keine
Objekt-Id, aber ein Name in den Anrufdaten steht. **Ist keine Identität konfiguriert, synchronisiert Zeitspur
bewusst gar keine Teams-Anrufe** (statt den ganzen Tenant einzulesen).

Pro Anruf werden Beginn, Dauer, Typ, Richtung und der Gesprächspartner gespeichert. Der Betreff nennt die
Richtung und den Gegenpart, z. B. „Teams-Anruf (ausgehend) mit Musterfrau, Erika“ oder „Teams-Anruf (eingehend)
mit Beispiel, Bernd“; reine Telefonate zeigen die Rufnummer. Der eigene Name wird dabei nie als Gesprächspartner
angezeigt. Die Teilnehmer liefert die Sammelabfrage nicht mit — Zeitspur lädt sie je Anruf einzeln nach
(`$expand=participants_v2`) und merkt sich die Zuordnung „mein Anruf?“ dauerhaft, damit nicht bei jedem Sync
hunderte Einzelabrufe anfallen. Lässt sich ein Teilnehmer im Verzeichnis nicht auflösen (Microsoft trägt dann
die Objekt-Id als Anzeigename ein), erscheint der Anruf ohne Namen statt mit einer kryptischen Id.

### Teams-Gespräche lokal (ohne Entra-App)

Plugin `teams_local` – die Alternative, wenn keine App-Registrierung möglich oder gewünscht ist. Microsoft
gibt die Anrufliste nur an eine registrierte App mit Admin-Zustimmung heraus (für `callRecords` gibt es
keine delegierte Berechtigung). Windows protokolliert aber für jede App, wann sie das Mikrofon benutzt –
dieselbe Quelle wie das Mikrofon-Symbol in der Taskleiste. Benutzt Teams das Mikrofon, läuft ein Anruf
oder eine Besprechung.

* **Nur dieser PC.** Gespräche am Handy (Teams-App) oder am Tischtelefon erfasst das Plugin nicht – dort
  benutzt Teams ein anderes Mikrofon. Die kennt nur Microsoft Graph, also das Plugin `teams` mit
  Entra-App. Wer die Registrierung hat, ist mit `teams` vollständiger bedient; `teams_local` ist die
  Lösung für Umgebungen ohne sie.
* **Keine Einrichtung:** keine Zugangsdaten, kein Admin, keine Netzverbindung. Hinzufügen genügt.
* **Genaue Zeiten**, auch für Anrufe, die nur geklingelt haben.
* **Gegenüber nur, wenn Teams es zeigt:** Während des Gesprächs werden die Titel aller Teams-Fenster
  gelesen (nicht nur des vordersten). Steht dort „Anruf von …“ oder hat die Besprechung ein eigenes
  Fenster, wird das übernommen – sonst heißt der Eintrag schlicht „Teams-Gespräch“. Ist die Aufnahme
  pausiert, werden keine Fenstertitel gelesen.
* **Erst ab dem Hinzufügen:** Windows merkt sich nur die jeweils letzte Nutzung. Zeitspur sieht alle
  5 Sekunden nach und schreibt selbst mit; rückwirkend gibt es nichts außer dieser letzten Nutzung.
* Nutzungen unter 5 Sekunden (Gerätetest, Fehlklick) zählen nicht als Gespräch.
* Noch nicht selbst geprüft: ob ein **stummgeschalteter** Teilnehmer weiter als „Mikrofon in Benutzung“
  gilt. Teams hält das Gerät beim Stummschalten in der Regel offen, dann zählt es.

Beide Teams-Plugins lassen sich gleichzeitig verwenden; ein Gespräch erscheint dann zweimal – einmal mit
Gesprächspartnern aus Microsoft Graph, einmal mit den Zeiten vom Mikrofon.

### Standort-Historie (Dawarich)

Plugin `dawarich`. Zeitspur liest aus einer **eigenen** Dawarich-Instanz zwei schreibgeschützte
Endpunkte: `/api/v1/visits` (erkannte Aufenthalte) und `/api/v1/tracks` (Fahrten als GeoJSON). Beides
erscheint als Marker auf dem Zeitstrahl und über den MCP-Server — damit lässt sich Bildschirmaktivität
einem Ort zuordnen („Was habe ich gemacht, als ich in Hamburg war?“).

Einrichtung: Einstellungen (⚙) → Plugins → Standort-Historie (Dawarich) → „Hinzufügen“, Basis-Adresse und
Token eintragen und „Speichern & testen“ wählen.

**Sicherheit**

* Nur **https**. `http://` wird abgelehnt — der Token ginge sonst im Klartext durchs Netz.
* Adresse und Token liegen DPAPI-geschützt in `%LOCALAPPDATA%\Zeitspur\dawarich_credentials.bin`,
  **nicht** in der `config.yaml`.
* Der Token wandert ausschließlich in den `Authorization`-Header — nie in die URL (URLs landen in
  Proxy-Logs), nie in Protokolle und nie in Fehlermeldungen.
* Es wird ausschließlich gelesen; die Schnittstelle kennt kein POST/PUT/DELETE.

**Was gespeichert wird**

| Art | Betreff | Ort |
|---|---|---|
| Aufenthalt | Name des Ortes, sonst `Aufenthalt (52.51627, 13.37770)` | Koordinaten |
| Fahrt | `Fahrt - 26,4 km` (nur wenn plausibel) | `52.51627, 13.37770 nach 52.39146, 13.06684` |

**Eigenheiten, die bewusst so behandelt werden**

* **Ortsnamen fehlen derzeit meist** (Dawarich ohne eingerichtete Ortsauflösung). Dann nennt Zeitspur
  ehrlich die Koordinaten, statt eine Adresse zu raten.
* Aufenthalte mit Status `declined` hat der Nutzer verworfen — sie werden übersprungen.
* Die Einheit von `distance` ist seitens Dawarich **nicht zugesichert**. Meter werden angenommen, aber nur
  übernommen, wenn das Ergebnis plausibel ist (höchstens 2000 km und 400 km/h). Sonst erscheint die Fahrt
  ohne Entfernung — lieber keine Angabe als eine falsche. Der Rohwert steht weiterhin in `extra`.
* Die Dauer wird aus Start und Ende gerechnet, nicht aus dem `duration`-Feld — dessen Einheit ist ebenfalls
  unbestätigt.
* Aufenthalte filtert der Server nach `started_at`. Ein Aufenthalt, der vor dem Fenster begann und
  hineinreicht, fiele damit weg; deshalb fragt Zeitspur mit **7 Tagen Vorlauf** ab und filtert selbst
  auf Überlappung.
* GeoJSON liefert `[Längengrad, Breitengrad]` — genau umgekehrt zur üblichen Schreibweise; das wird beim
  Einlesen gedreht.
* Höchstens **60 Anfragen pro Minute**: Zeitspur fragt große Zeiträume am Stück ab statt viele kleine.
* Ist der Dawarich-Server nicht erreichbar (502/504 oder TLS-Fehler), meldet der Sync das und **stoppt die
  anderen Quellen nicht** — Outlook und Teams laufen weiter.

**Wenn der Abruf fehlschlägt — erst hier nachsehen**

* **Die Adresse im Browser öffnen.** Lädt `https://<Ihr-Server>/` dort nicht, liegt das Problem beim Server
  oder im Netz, nicht bei Zeitspur.
* **Eine private Adresse (etwa `10.x.x.x`) im DNS ist nicht zwingend ein Fehler.** Manche Netze lösen denselben
  Namen intern anders auf als von außen (*Split-Horizon-DNS*). Zeitspur verlangt trotzdem immer `https://` –
  über `http://` ginge der Token im Klartext durchs Netz.
* **Ein TLS-Fehler ist nichts, was Zeitspur reparieren könnte.** Dann fehlt dem Server gerade ein
  gültiges Zertifikat; das gehört auf dem Server behoben. Ein Umweg über HTTP oder eine abgeschaltete
  Zertifikatsprüfung kommt **nicht** in Frage – der Token wäre sonst mitlesbar. Ein Test
  (`test_certificate_verification_is_never_disabled`) hält das fest.
* **Ein leeres Ergebnis heißt „nichts aufgezeichnet“, nicht „Fehler“.** Vor der ersten längeren Fahrt
  liefern beide Endpunkte leere Listen.

### Karte (OpenStreetMap)

**Standardmäßig aus – und das ist die einzige Stelle, an der Zeitspur Daten aus dem Internet holt.**
Einschalten unter Einstellungen (⚙) → „Karte anzeigen (lädt Kacheln aus dem Netz)“.

Ist sie an und enthält der Tag Aufenthalte oder Fahrten, erscheint oben der Knopf **„Karte“**. Er öffnet
eine Karte unter dem Zeitstrahl: Aufenthalte als Punkte, Fahrten als Linie mit Start- und Zielmarkierung.
Ein Klick auf einen Eintrag in der Spur **„Orte“** springt auf der Karte direkt dorthin.

**Was das kostet, ehrlich gesagt:** Kartenkacheln werden pro Bildausschnitt von `tile.openstreetmap.org`
geladen. Der Kachel-Server erfährt dadurch, **welche Gegenden Sie sich ansehen**. Ihre Aufenthalte selbst
werden nicht übertragen – aber aus den abgerufenen Kacheln lässt sich ableiten, wohin Sie schauen. Deshalb
ist die Karte abschaltbar und im Auslieferungszustand aus. Solange sie aus bleibt, gilt das Versprechen
„alles bleibt auf dem Rechner“ unverändert.

Technisch:

* **Leaflet ist mitgeliefert**, es wird kein CDN angesprochen. Die Programmbibliothek selbst lädt nichts
  nach; erst die geöffnete Karte holt Kacheln.
* Die Kachel-Adresse steht in `map_tile_url` und lässt sich auf einen **eigenen Kachel-Server** umbiegen –
  dann verlässt auch dafür nichts Ihr Netz. Sie muss `https` sein und `{z}/{x}/{y}` enthalten.
* Die Namensnennung „© OpenStreetMap-Mitwirkende“ wird eingeblendet; das ist bei OSM-Daten (ODbL) Pflicht.
* **Startpunkt:** Ohne Aufenthalte öffnet die Karte beim eingestellten Startpunkt – ab Werk mit Blick auf ganz
  Deutschland. Über `map_home_lat`, `map_home_lon`, `map_home_zoom` und `map_home_label` lässt sich ein eigener
  Bezugspunkt setzen, etwa das Büro; mit Namen erscheint er als blauer Punkt. Sobald der Tag Aufenthalte oder
  Fahrten enthält, rückt die Karte stattdessen auf diese Daten.
* Für die Linie speichert Zeitspur die Strecke vereinfacht (Douglas-Peucker, höchstens 500 Punkte je
  Fahrt, Abweichung unter ~10 m). Eine Fahrt mit 4000 Rohpunkten belegt so rund 10 KB statt ein Vielfaches.

**Ortsnamen statt Koordinaten:** Die Beschriftung kommt aus dem `name`-Feld von Dawarich. Solange dort keine
Ortsauflösung eingerichtet ist, bleibt sie leer und Zeitspur zeigt die Koordinaten – bewusst, statt eine
Adresse zu raten. Richten Sie die Auflösung in Dawarich ein (z. B. Photon), erscheinen Straße, Ort und oft
auch der Name des Betriebs automatisch; Zeitspur braucht dafür keine Änderung und fragt keinen fremden
Dienst.

### Bekannte Orte: aus Koordinaten werden Namen

Damit Claude „Du warst im Büro“ sagen kann statt „Koordinaten 52.51627, 13.37770“, lassen sich Orte
benennen. Ein benannter Kartenstartpunkt (`map_home_label`) zählt automatisch als bekannter Ort; weitere trägt
man in den Einstellungen unter **„Bekannte Orte (je Zeile)“** ein:

```
Büro;52.5163;13.3777;200
Kunde Beispiel AG;53.5503;9.9920;300
```

Format: `Name;Breite;Länge` und optional `;Radius in Metern` (Standard 150 m). Der Abstand wird als echte
Entfernung gerechnet (Haversine), nicht als Koordinatendifferenz – sonst wäre der Radius abhängig vom
Breitengrad. Liegt ein Aufenthalt im Radius mehrerer Orte, gewinnt der nächstgelegene.

Wirkung an drei Stellen:

* **Zeitstrahl:** Der Aufenthalt heißt „Büro“ statt der Koordinaten.
* **MCP:** `get_activity_at` liefert zusätzlich ein Feld **`location`** mit `place` (benannter Ort oder
  `null`), `coordinates`, `kind` („Aufenthalt“/„Fahrt“), Zeitraum und `covers_timestamp`.
* **Antworten von Claude:** Die Server-Anweisung sagt ausdrücklich, Ort, Termin und Bildschirmarbeit zu
  **einer** Aussage zu verbinden, etwa: „Du warst im Büro und hast in Visual Studio am Beispielprojekt
  gearbeitet; laut Outlook lief parallel der Kundentermin mit der Beispiel AG.“

**Ehrlich bleibt es trotzdem:** Ist der Ort nicht bekannt, steht `place: null` und es werden nur die
Koordinaten genannt – geraten wird nichts. Liegen gar keine Standortdaten vor, fehlt `location` ganz und
Claude sagt nichts über den Ort. Ein unbrauchbarer Eintrag in der Liste wird übersprungen (mit Protokoll-
Hinweis) und verhindert nicht den Start; beim Speichern über die Einstellungen wird er dagegen sofort
mit einer Fehlermeldung abgelehnt.

### Plaud

Für Plaud ist kein Code in Zeitspur nötig: Claude ruft in derselben Unterhaltung den Zeitspur-MCP und
den vorhandenen Plaud-MCP-Connector auf und gleicht die Zeitstempel ab. Fragen Sie Claude z. B. „Was lief
gestern um 14 Uhr auf dem Bildschirm und gibt es dazu eine Plaud-Aufnahme?“.

## Datenschutz und Sicherheit

* **Verschlüsselung:** Die gesamte Datenbank (Metadaten, OCR-Text, FTS-Index, Screenshots) ist mit SQLCipher
  (AES-256) verschlüsselt; auch die WAL-Datei ist verschlüsselt. Der Schlüssel wird beim ersten Start zufällig
  erzeugt und mit `CryptProtectData` (Scope: aktueller Benutzer) in `key.bin` abgelegt.
* **Kein Klartext auf der Platte:** keine Bilddateien, keine Temp-Dateien (Tesseract liest aus stdin,
  SQLite arbeitet mit `temp_store=MEMORY`), keine Bildschirmtexte oder Fenstertitel in den Protokollen,
  WebView2 läuft im privaten Modus ohne Crash-Dumps, der Datenordner ist von der Windows-Suche ausgenommen.
* **Was der Schutz leistet:** Andere Windows-Benutzer, Diebstahl der Festplatte oder Kopien der Datei können
  die Daten nicht lesen. **Was er nicht leistet:** Jedes Programm, das unter Ihrem Konto läuft, kann `key.bin`
  ebenfalls entschlüsseln – Zeitspur schützt nicht vor Schadsoftware in Ihrer eigenen Sitzung.
* **Sicheres Löschen:** Gelöschte Einträge (Detail-Löschen, „Letzte 15 Minuten“, Retention) geben ihre – stets
  verschlüsselten – Datenbankseiten genullt frei (`secure_delete=ON`), sodass sie nicht aus der Datei
  rekonstruiert werden können.
* **Bildschirmtext als Modell-Eingabe:** Über den MCP-Server gelangt der erkannte Bildschirmtext zu Claude.
  Enthält ein aufgenommener Screenshot manipulativen Text, wird dieser Teil der Eingabe an das Modell
  (indirekte Prompt-Injektion). Das ist dem Prinzip „Bildschirmverlauf abfragbar machen“ inhärent; behandeln
  Sie Antworten, die auf fremdem Bildschirminhalt beruhen, mit der üblichen Vorsicht.
* **Kontrolle:** Ausschlusslisten, Pause im Tray, „Aktuelle App ausschließen“, „Letzte 15 Minuten löschen“,
  Einzeleinträge im Detailfenster löschen, automatische Löschung nach `retention_days`.
* **Updates nur signiert:** Die Release-Ausgabe installiert nur Updates mit gültiger Signatur des Herausgebers
  und nur neuere Versionen (siehe [Updates](#updates)).
* **Wiederherstellbarkeit:** Wird das Windows-Passwort durch einen Administrator zurückgesetzt, kann DPAPI den
  Schlüssel nicht mehr entschlüsseln. Zeitspur bietet dann an, die Datenbank zurückzusetzen (alte Dateien
  werden umbenannt, nicht gelöscht).

## Performance und Speicherbedarf

Der Dienst läuft mit reduzierter Prozesspriorität; OCR und Wartung laufen im Hintergrundmodus, Tesseract auf
einem Kern (`OMP_THREAD_LIMIT=1`). Richtwerte: unter 2 % CPU im Leerlauf, OCR-Spitzen unter 15 % eines Kerns.

CPU-Last prüfen: Task-Manager → Details → `Zeitspur.exe` und `tesseract.exe`, oder in PowerShell

```bash
Get-Process Zeitspur, tesseract -ErrorAction SilentlyContinue | Select-Object Name, CPU, @{n='MB';e={[math]::Round($_.WorkingSet64/1MB)}}
```

Gemessen mit `tools\perf_probe.py` (gepackte EXE, 115 s aktive Arbeit mit RDP-Sitzung und Chatfenster, 8 logische
Kerne): Dienstprozess Ø 3,5 % eines Kerns (0,4 % der Maschine, Spitzen bis 49 % beim Komprimieren eines neuen
Bildes), Texterkennung Ø 16 % eines Kerns (8 neue Bilder, je etwa 2,3 s CPU auf einem Kern mit niedriger
Priorität), WebView2 des versteckten Fensters Ø 3 % eines Kerns; zusammen Ø 2,8 % der Maschine, 227 MB RAM.
Ohne Eingaben pausiert die Aufnahme, dann fällt die Last auf nahezu null.

Stellschrauben, wenn die Last zu hoch ist: `capture_interval_seconds` erhöhen (10–15 s), `max_image_width`
auf 1280 senken (weniger OCR- und Speicheraufwand), `change_threshold` leicht erhöhen (0.02–0.03), `ocr_psm`
belassen (3) oder auf 11 stellen, wenn viel verstreuter Text auf dem Bildschirm ist.

Speicherbedarf: Ein 1920×1200-Screenshot belegt als WebP (Qualität 75) etwa 150–300 KB. Bei aktiver Arbeit
entstehen nach der Duplikat-Erkennung grob 300–600 Bilder pro Stunde, also 6–17 GB für 14 Tage à 8 Stunden.
Wer weniger Platz opfern möchte, senkt `max_image_width` (1280 ≈ halber Bedarf), `webp_quality` oder
`retention_days`; `max_db_size_gb` begrenzt die Datei in jedem Fall. Gelöschter Platz wird täglich über
inkrementelles Vacuum an das Dateisystem zurückgegeben; `tools\dbstat.py` (Quelltext) zeigt Größe und Statistik.

## Updates

Die Release-Ausgabe (das Setup von GitHub) hält sich selbst aktuell. Fünf Minuten nach dem Start und danach
alle 12 Stunden holt sie die Datei `latest.json` des neuesten GitHub-Releases. Ist dort eine neuere Version
eingetragen, passiert Folgendes:

* **Automatische Updates an (Standard):** Das neue Setup wird im Hintergrund geladen und geprüft. Sobald Maus und
  Tastatur eine Weile unbenutzt sind – Einstellungen → Updates → „Installieren nach Leerlauf von (Minuten)“,
  Standard 5 –, installiert es sich: Ein kleines Fenster zeigt den Fortschritt, Zeitspur wird beendet, ersetzt
  und meldet sich mit dem Startfenster zurück – mit Hauptfenster, wenn es vorher offen war, sonst im Infobereich.
  Eine Meldung bestätigt die neue Version. Daten und Einstellungen bleiben unverändert.
* **Automatische Updates aus** (Einstellungen → Updates): Im Zeitstrahl erscheint ein Hinweis „Zeitspur X ist
  verfügbar“ mit „Was ist neu?“ und **Jetzt aktualisieren**; zusätzlich einmal eine Meldung im Infobereich.
* Jederzeit von Hand: Tray-Menü → **Nach Updates suchen** oder oben in den Einstellungen, wo auch die installierte
  Version und das Ergebnis der letzten Prüfung stehen.

**Sicherheit:** Jedes Update ist mit dem Release-Schlüssel des Herausgebers signiert (Ed25519). Zeitspur trägt
nur den öffentlichen Teil in sich und installiert ausschließlich Setups, deren Signatur, Größe und SHA-256 stimmen –
und nur eine **neuere** Version, nie eine ältere. Auch wer die Download-Quelle übernähme, könnte so keinen fremden
Code verteilen. Übertragen wird bei der Prüfung nur eine gewöhnliche HTTPS-Anfrage an github.com (mit der
Programmversion als Kennung), nichts aus Ihrer Datenbank.

Scheitert ein Update, läuft die bisherige Version weiter; dieselbe Version wird dann 24 Stunden lang nicht erneut
automatisch versucht. Das Protokoll des Setups liegt in `%LOCALAPPDATA%\Zeitspur\updates\`.

Der selbst gebaute Programmstand (`build.ps1` ohne `-Release`) und der Quelltextbetrieb aktualisieren sich nie
selbst – ein Release ersetzte dort sonst Funktionen, die er nicht enthält.

## Deinstallation

Systemsteuerung → Apps → „Zeitspur“ oder Startmenü → „Zeitspur deinstallieren“. Der Deinstaller beendet
den laufenden Dienst, entfernt Programmdateien, Startmenü-Einträge und den Autostart-Wert. Anschließend fragt
er, ob auch `%LOCALAPPDATA%\Zeitspur` (Datenbank, Schlüssel, Konfiguration, Protokolle) gelöscht werden soll –
**Standard: Nein**. Behalten Sie die Daten, werden sie bei einer Neuinstallation weiterverwendet.

Der Eintrag in `claude_desktop_config.json` wird nicht automatisch entfernt (Datei gehört Claude Desktop);
löschen Sie den Block `Zeitspur` bei Bedarf von Hand.

Haben Sie den Speicherort der Datenbank über die Einstellungen auf einen eigenen Ordner verlegt, löscht auch
die Option „Daten löschen“ nur `%LOCALAPPDATA%\Zeitspur` (Schlüssel, Konfiguration, Protokolle). Die
verlegte `zeitspur.db` (samt `-wal`/`-shm`) am eigenen Ort müssen Sie dann selbst entfernen. Sie ist ohne den
gelöschten Schlüssel nicht mehr lesbar, belegt aber weiter Speicherplatz.

## Fehlerbehebung

| Symptom | Ursache / Abhilfe |
|---|---|
| Fenster öffnet sich nicht, Meldung zur WebView2-Runtime | WebView2 fehlt (das Setup konnte sie nicht laden, etwa ohne Internet): In der Meldung „Ja“ öffnet Microsofts Download-Seite; sonst <https://developer.microsoft.com/microsoft-edge/webview2/consumer/> aufrufen und installieren |
| Nach dem Start erscheint nur das kleine Startfenster | Der erste Start nach Installation oder Update kann auf langsamen PCs oder mit aktivem Virenscanner deutlich länger dauern. Spätestens nach 45 s erscheint das Hauptfenster trotzdem; wie lange der Start gedauert hat, steht in `logs\service.log` („Oberflaeche geladen, … s nach Programmstart“) |
| Banner „Texterkennung nicht verfügbar“ | Ordner `tesseract\` neben der EXE fehlt oder ist unvollständig – Setup erneut ausführen |
| Dialog „Datenbank kann nicht geöffnet werden“ beim Start | Schlüssel passt nicht (Passwort-Reset, kopierte Dateien). „Zurücksetzen“ legt eine neue Datenbank an, alte Dateien erhalten das Suffix `.unreadable-<Zeit>` |
| Tray zeigt „Inaktiv“ oder „Bildschirm gesperrt“ | gewollt: keine Aufnahme ohne Eingaben bzw. bei Sperre; `idle_pause_minutes` anpassen |
| Tray zeigt „Zu wenig Speicherplatz“ | freien Platz schaffen oder `min_free_disk_gb` senken |
| Claude findet den Server nicht | Pfad in der Konfiguration prüfen (`Zeitspur.exe --mcp --print-config`), Claude Desktop neu starten, `logs\mcp.log` ansehen |
| `Zeitspur.exe --mcp` ohne Claude gestartet zeigt nur einen Hinweis | erwartet: der Server wird von Claude über stdio gestartet |
| Virenscanner meldet die EXE | unsignierte PyInstaller-Programme sind ein bekannter Fehlalarm-Fall; Datei aus der Quarantäne holen bzw. Ausnahme durch die IT eintragen lassen; Ursache und Abhilfe (Code-Signatur) siehe Entwicklung |
| Zwei Instanzen? | nicht möglich: ein zweiter Start zeigt nur das Fenster der laufenden Instanz |

Protokolle: `%LOCALAPPDATA%\Zeitspur\logs\service.log` (Dienst) und `mcp.log` (MCP-Server); `log_level: DEBUG`
liefert Details zur Frame-Erkennung.

## Entwicklung und Build

Voraussetzungen: Python 3.13 (64 Bit), PowerShell 7. Adminrechte werden nicht benötigt.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
pwsh -File tools\fetch_tesseract.ps1      # laedt Tesseract 5.5.3 + Sprachdaten (SHA-256 geprueft), baut installer\tesseract-portable
pwsh -File tools\install_innosetup.ps1    # Inno Setup 6 pro Benutzer (fuer den Installer)
.venv\Scripts\python -m pytest -q          # Tests
.\run_dev.ps1 -Show -Debug                 # Dienst aus dem Quelltext starten
.\build.ps1                                # Tests, PyInstaller, Tesseract-Kopie, Installer -> dist\ZeitspurSetup.exe
$env:ZEITSPUR_BUILD_MCP_EXE = "1"; .\build.ps1   # zusaetzlich separate ZeitspurMCP.exe bauen
.\build.ps1 -Release                       # Release-Ausgabe fuer andere -> dist-release\ZeitspurSetup.exe
```

**Release-Ausgabe:** `build.ps1 -Release` packt Funktionen, die noch nicht fertig sind, gar nicht erst ein –
derzeit die Standort-Historie (Dawarich) samt Karte und bekannten Orten. Das Programm erkennt selbst, was fehlt
(`zeitspur/edition.py`), und blendet es aus; ein Wächter bricht den Build ab, falls doch etwas davon im Paket
steckt. Die Ausgabe landet in eigenen Ordnern (`dist-release`, `build-release`), der normale Build bleibt
unberührt. GitHub-Releases werden aus der Release-Ausgabe gebaut.

### Release veröffentlichen

Ein Release besteht aus dem Setup und der signierten `latest.json`, aus der sich installierte Release-Ausgaben
selbst aktualisieren (siehe [Updates](#updates)). Ablauf:

1. **Einmalig:** Release-Schlüssel erzeugen – `python tools\release_key.py init` legt ihn DPAPI-geschützt unter
   `%USERPROFILE%\.zeitspur\` ab und nennt den öffentlichen Teil, der in `zeitspur/updater.py`
   (`PUBLIC_KEY`) gehört. Danach **sofort sichern:** `python tools\release_key.py backup --out <Datei>` fragt ein
   Passwort ab und schreibt eine verschlüsselte Kopie; Datei und Passwort gehören in den Passwortmanager, nie ins
   Repo. Auf einem neuen PC: `restore --in <Datei>`. Ist der Schlüssel verloren, müssen installierte Versionen
   einmal von Hand auf eine Version mit neuem Schlüssel aktualisiert werden.
2. Version in `zeitspur/__init__.py` erhöhen (einzige Stelle; `build.ps1` reicht sie an den Installer
   weiter) und die Änderungen in `CHANGELOG.md` unter `## <Version>` eintragen – daraus entstehen die
   Release-Notizen und der Text „Was ist neu?“ in der App.
3. Committen und pushen, dann `pwsh -File tools\publish_release.ps1`: baut die Release-Ausgabe (mit Tests),
   signiert `latest.json`, prüft Signatur und Setup wie ein Client und legt den GitHub-Release
   `v<Version>` mit beiden Dateien an. `-DryRun` baut und signiert nur.

Zum Testen in der Windows Sandbox lässt sich ein eigener Update-Kanal setzen: `ZEITSPUR_UPDATE_URL` (etwa
`file:///C:/ZeitspurTest/channel/latest.json`) und `ZEITSPUR_UPDATE_TIMING` mit „Start,Intervall“ in Sekunden
(optional „,Leerlauf“ – sonst gilt die Einstellung). Die Signaturprüfung gilt auch dann.
`tools\sandbox_test.ps1 -UpdateTest` richtet das komplett ein.

Hinweis zu Virenscannern: Unsignierte PyInstaller-EXEs, die den komprimierten Python-Code als Overlay tragen,
wurden auf dem Entwicklungsrechner vom Virenscanner wenige Sekunden nach dem Build gelöscht – sobald die EXE
sowohl den Zeitspur-Code (Screenshots, DPAPI, Fenstertitel) als auch den HTTP-Stack des MCP-SDKs enthielt.
Die Spec baut deshalb mit `noarchive=True`: Alle Module liegen als einzelne Dateien in `_internal\`, die EXE
besteht nur aus dem Bootloader (~0,3 MB) und blieb unbeanstandet. `build.ps1` prüft nach dem Build, ob die EXE
noch existiert. Nachhaltige Abhilfe ist eine Code-Signatur (`signtool sign` mit einem Code-Signing-Zertifikat)
oder eine Ausnahme im Virenscanner.

Nützliche Werkzeuge in `tools\`: `seed_demo_db.py` (Demo-Datenbank mit synthetischen Einträgen, mit
`ZEITSPUR_DATA_DIR` in einen separaten Ordner), `dbstat.py` (Statistik/Suche), `ui_smoke.py` (startet den
Dienst, macht Fenster-Screenshots, beendet ihn und prüft das Log), `mcp_smoke.py` (spricht den MCP-Server über
stdio an wie ein Claude-Client), `trim_tesseract.py` (entfernt nicht benötigte DLLs aus dem Tesseract-Bundle),
`sandbox_test.ps1` (spielt die Installation in der Windows Sandbox durch – frisches Windows, so wie ein Kollege
sie erlebt; `-Dev` für den eigenen Build, `-NoStart` nur vorbereiten).

Projektstruktur:

```
zeitspur/
  config.py        Konfiguration laden/validieren/speichern
  crypto.py        DPAPI-Schluessel, SQLCipher-Verbindung mit allen Sicherheits-PRAGMAs
  storage.py       Schema, Schreib-/Lesemethoden, FTS5-Suche, Wartung
  capture.py       Screenshot-Loop, Aenderungserkennung, Leerlauf/Sperre, Ausschlusslisten
  ocr.py           Tesseract ueber stdin/stdout (nur im Speicher)
  ocr_worker.py    OCR-Warteschlange mit Nachholen aus der Datenbank
  cleanup.py       Retention, Groessendeckel, inkrementelles Vacuum
  app.py           Lebenszyklus (Threads, Fenster, Tray, Beenden)
  tray.py          pystray-Menue und Zustandsfarben
  splash.py        Startfenster mit Laufbalken, bis der Zeitstrahl geladen ist
  timeline_ui/     Bridge (js_api) und die eingebettete HTML/CSS/JS-Oberflaeche
  service_main.py  Einstieg Zeitspur.exe
  mcp_server.py    MCP-Server (Zeitspur.exe --mcp, optional ZeitspurMCP.exe)
tests/             pytest-Suite (Krypto, Storage, Frame-Diff, OCR, Cleanup, Bridge, MCP)
installer/         installer.iss (Inno Setup), tesseract-portable/ (generiert)
zeitspur.spec   PyInstaller: zwei EXEs, gemeinsamer _internal-Ordner
```

Abweichungen zur ursprünglichen Spezifikation (mit Begründung): `sqlcipher3-wheels` statt `sqlcipher3-binary`
(einziges Paket mit Wheels für Python 3.13), direkter Tesseract-Aufruf statt `pytesseract` (das intern
Temp-Dateien schreibt), `mcp` 2.x mit `MCPServer` (Nachfolger von `FastMCP`), Pixel-Differenz statt reinem
Perceptual-Hash (erkennt Tippen und Scrollen), inkrementelles Vacuum statt täglichem Voll-VACUUM (gibt Platz
frei, ohne die Datei komplett neu zu schreiben).

## Lizenz

Zeitspur steht unter der **MIT-Lizenz mit Commons-Clause-Zusatz** (siehe `LICENSE`): Nutzen, Ändern und
Weitergeben ist erlaubt, **Verkaufen nicht** – also kein kostenpflichtiges Produkt und keine bezahlte
Dienstleistung, deren Wert im Wesentlichen aus Zeitspur stammt. Der Quelltext ist damit offen einsehbar, die
Lizenz ist aber keine Open-Source-Lizenz im Sinne der OSI.

Mitgelieferte Drittkomponenten behalten ihre Lizenzen. `build.ps1` legt sie samt vollständigen Lizenztexten als
`THIRD-PARTY-NOTICES.txt` in den Programmordner (erzeugt von `tools\third_party_notices.py`). Im Wesentlichen:
Python (PSF), Tesseract OCR und die Sprachdaten `tessdata_fast` (Apache 2.0), SQLCipher über `sqlcipher3-wheels`
(zlib), pywebview (BSD-3), pythonnet (MIT), mss (MIT), Pillow (MIT-CMU), psutil (BSD-3), PyYAML (MIT), pywin32
(PSF), MCP-SDK (MIT), cryptography (Apache 2.0 oder BSD-3), **pystray (LGPL-3.0)** – es liegt unverändert als
eigene Dateien unter `_internal\pystray` und lässt sich dort ersetzen – sowie im eigenen Build Leaflet (BSD-2,
`zeitspur/timeline_ui/vendor/LICENSE-leaflet.txt`).

## Projektmodule (Phase 2)

Neu hinzugekommen sind `zeitspur/outlook.py` (COM-Zugriff auf den Outlook-Kalender), `zeitspur/teams.py`
(Microsoft-Graph-`callRecords` und DPAPI-geschützte Zugangsdaten) und `zeitspur/events_sync.py` (periodische
und bedarfsgesteuerte Synchronisierung in die Tabelle `calendar_events`). Datenbankschema v2 ergänzt diese
Tabelle; bestehende v1-Datenbanken werden beim ersten Start automatisch migriert.
