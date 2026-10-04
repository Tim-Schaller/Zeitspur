# Änderungen

Jeder Abschnitt `## <Version>` wird beim Veröffentlichen zu den Release-Notizen auf GitHub und zum Text
„Was ist neu?“ in der App (tools/publish_release.ps1).

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
