# Änderungen

Jeder Abschnitt `## <Version>` wird beim Veröffentlichen zu den Release-Notizen auf GitHub und zur Liste
„Neu in Zeitspur …“ im Update-Hinweis der App (tools/publish_release.ps1; dort höchstens sechs Punkte).

## 0.4.3

- Private Fenster schwärzen statt Aufnahme anhalten: Fenster auf der Ausschlussliste (InPrivate/Inkognito,
  Passwortmanager, Banking) werden im Bild schwarz übermalt – liegen sie hinter anderen Fenstern, nur der Teil, der
  hervorschaut. Der Rest des Bildschirms wird weiter aufgenommen, der Fenstertitel nicht gespeichert. Bisher konnte
  ein offenes, maximiertes privates Fenster die Aufnahme aller Monitore stundenlang stoppen, obwohl das Symbol
  „Aufnahme läuft“ zeigte.
- Neues Plugin „Langdock“: Zeitspur lässt sich auch in Langdock fragen. Ein eigenes Relay nimmt die Fragen an,
  Zeitspur verbindet sich von sich aus dorthin – der PC bleibt von außen unerreichbar. Screenshots nur mit
  ausdrücklicher Freigabe; im Tray-Menü lässt sich der Zugang jederzeit ausschalten.
- Tag exportieren: Der Knopf „Export“ kopiert den gezeigten Tag als Markdown in die Zwischenablage oder speichert
  ihn als Datei – etwa für einen KI-Chat ohne MCP-Anbindung. Den erkannten Bildschirmtext gibt es nur auf Wunsch dazu.
- Texterkennung schont den PC: Nach jedem Bild legt Zeitspur eine Pause ein, sodass die Erkennung dauerhaft nur noch
  etwa ein Drittel eines Prozessorkerns belegt (`ocr_throttle`); im Akkubetrieb ruht sie ganz
  (`ocr_pause_on_battery`). Außerdem weniger fast gleiche Bilder: ein neues Bild erst ab 2,5 % geänderten
  Bildpunkten (`change_threshold`, bisher 1,5 %; eine eigene Einstellung bleibt unverändert).
- Outlook-Plugins starten das klassische Outlook nicht mehr selbst. Bisher öffnete jeder Abgleich ein unsichtbares
  Outlook, wenn es nicht lief – mit vollem Postfach-Abgleich und großen Protokolldateien.
- Karte: Der Startpunkt (Breite/Länge) ist ab Werk leer statt mit Koordinaten vorbelegt; ohne Angabe zeigt die
  Karte weiter ganz Deutschland. Die alte Vorgabe wird aus bestehenden Einstellungen entfernt.

## 0.4.1

- Mehrere Monitore: Zeitspur merkt sofort, wenn Monitore an- oder abgesteckt werden (etwa an der Dockingstation).
  Bisher nahm es bis zum nächsten Neustart nur die Monitore auf, die beim Start angeschlossen waren.

## 0.4.0

- Standort-Spur: Die Zeile „Orte“ zeigt den Tag als lückenlose Leiste – „08:20–16:40 Büro, 25 min Fahrt nach
  Hause …“. Sie führt WLAN-Netze, GPS vom Handy (Plugin für die eigene Dawarich-Instanz) und die Ortung von
  Windows (neues Plugin) zusammen; Erschlossenes ist schraffiert. Ein Klick öffnet den Tagesablauf mit Karte,
  dort lassen sich Orte benennen – auch Claude erzählt den Tag dann so.
- Plugin-Browser (Knopf „Plugins“ oben rechts): alle Plugins mit Suche und Kategorien, und je Plugin, was es
  erfasst, was gespeichert wird und wie man es einrichtet. Auswählen geht schon in der Ersteinrichtung – nichts
  ist vorausgewählt.
- Neue Plugins ohne Konto: Gespräche in allen Apps (Zoom, Slack, Webex, STARFACE, WhatsApp, Meetings im
  Browser …), Windows-Benachrichtigungen, Browser-Verlauf, Outlook-Mails, Git-Commits, PC-Zeiten und WLAN-Netze
  als Ortshinweis.
- Neue Online-Plugins: Kalender per ICS-Link (Google, iCloud, neues Outlook, Nextcloud …) und GitHub.
- Zeitstrahl mit eigenen Zeilen für Gespräche, Mails, Mitteilungen, Web, Entwicklung und PC; Claude bekommt zu
  jedem Ereignis die Details.
- Sicherheit: strengere Schutzregeln für die Oberfläche, Zugangsdaten gehen bei Weiterleitungen nie an fremde
  Server, keine Weiterleitung auf interne Adressen, Updates nie auf eine ältere Version, und die Ausschlussliste
  greift für jedes sichtbare Fenster, nicht nur das vorderste.
- Der Update-Hinweis nennt die wichtigsten Neuerungen und verlinkt alle Änderungen auf GitHub.

## 0.3.2

- Claude Desktop aus dem Microsoft Store: Die Registrierung findet die richtige Konfigurationsdatei und wartet,
  bis Claude Desktop beendet ist – sonst würde es den Eintrag wieder überschreiben.

## 0.3.1

- Erste öffentliche Version auf GitHub.

## 0.3.0

- Automatische Updates: Zeitspur prüft regelmäßig, ob es eine neue Version gibt, lädt sie im Hintergrund,
  prüft ihre digitale Signatur und installiert sie, sobald der PC ein paar Minuten nicht benutzt wird. Danach
  startet es von selbst wieder. Abschaltbar unter Einstellungen → Updates – dann erscheint nur ein Hinweis mit
  „Jetzt aktualisieren“.
- Einstellungen → Updates: automatische Installation an/aus und wie lange der PC vorher unbenutzt sein muss
  (Standard 5 Minuten). Oben in den Einstellungen stehen die installierte Version, das Ergebnis der letzten
  Prüfung und „Nach Updates suchen“ – ebenso im Tray-Menü.
- Lizenz: MIT mit Commons Clause; die Lizenzen aller mitgelieferten Komponenten stehen in
  THIRD-PARTY-NOTICES.txt im Programmordner.

## 0.2.0

- Fehlt die Microsoft Edge WebView2-Runtime, installiert das Setup sie selbst und zeigt dabei den Fortschritt.
- Beim Start erscheint sofort ein Startfenster mit Laufbalken, bis der Zeitstrahl geladen ist – hell oder
  dunkel wie Windows.
- Die letzte Setup-Seite und die Ersteinrichtung sagen, was gerade passiert.

## 0.1.0

- Erste Version: Bildschirmaufnahme mit Texterkennung, verschlüsselte Datenbank, Zeitstrahl mit Volltextsuche,
  MCP-Server für Claude und Plugins für Outlook, Teams und Standort-Historie.
