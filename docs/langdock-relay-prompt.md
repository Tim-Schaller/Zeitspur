# Aufgabe: Eigenes Zeitspur-Relay für Langdock aufsetzen

Sprich Deutsch mit mir, kurz und klar. Bevor du etwas anlegst, änderst oder öffnest, machst du eine
Bestandsaufnahme und stellst mir die offenen Fragen gesammelt. Secrets gibst du nie im Chat aus.

## Hintergrund
- **Zeitspur** (https://github.com/Tim-Schaller/Zeitspur, ab Version 0.4.3) ist eine Windows-App: ein lokaler,
  verschlüsselter Aktivitätsverlauf mit Screenshots, Texterkennung, Fenstertiteln, Kalender und Standort. Ihr
  MCP-Server läuft nur lokal über stdio (`Zeitspur.exe --mcp`). Er hat 7 Werkzeuge: `get_time_context`,
  `get_activity_at`, `search_activity`, `list_active_apps`, `get_entry`, `get_screenshot` und `get_calendar`.
- **Langdock** kann keine lokalen MCP-Server starten. Es ruft MCP-Server aus seiner Cloud auf, per Streamable HTTP
  über eine **öffentliche** HTTPS-Adresse; private Adressen blockt es. Schlag in der Langdock-Doku nach,
  - von welcher festen IP die Anfragen kommen
    (https://docs.langdock.com/en/admin/security/static-ip-configuration, Stand Herbst 2026: `4.185.103.44`),
  - welchen Header Langdock bei Authentifizierung per API-Key schickt.
- Zeitspur bringt den Client für das Relay schon mit (Plugin „Langdock“). Ich brauche nur das **Relay**: einen kleinen
  Server, den Langdock als MCP-Server aufruft und der die Aufrufe an mein Zeitspur weiterreicht.

## Architektur
```
Langdock-Cloud ──HTTPS POST /mcp (API-Key, nur von der Langdock-IP)──▶ Relay (mein Server)
                                                                          ▲
Mein PC: Zeitspur ──ausgehende WebSocket-Verbindung wss:// /agent (Agent-Token)──┘
```
- Mein PC baut die Verbindung immer selbst nach außen auf und ist nie von außen erreichbar.
- Das Relay nimmt MCP-Aufrufe von Langdock an, reicht sie über die WebSocket-Verbindung an Zeitspur weiter und gibt
  die Antwort zurück.
- Es gibt genau einen Nutzer: mich. Durch das Relay laufen meine Bildschirmtexte, Mails, Termine und Orte – deshalb
  läuft es auf einem Server, den nur ich kontrolliere.

## Protokoll-Vertrag (fest – der Zeitspur-Client ist danach gebaut, nichts daran ändern)
Lege den Vertrag wörtlich als `PROTOKOLL.md` ins Repo.

**WebSocket `wss://<host>/agent`**, Header `Authorization: Bearer <AGENT_TOKEN>`, Textframes mit JSON:
- Falscher oder fehlender Token: Handshake mit HTTP 401 ablehnen (Zeitspur versucht es dann erst nach 10 Minuten neu).
- Client → Relay, erste Nachricht: `{"t":"hello","v":1,"init":<InitializeResult des lokalen Zeitspur-MCP>,"tools":<tools/list-Result>}`
- Relay → Client: `{"t":"welcome","v":1}` (Zeitspur wartet darauf höchstens 15 s)
- Relay → Client: `{"t":"call","rid":"<uuid>","msg":<JSON-RPC-Request von Langdock, unverändert>}`
- Client → Relay: `{"t":"reply","rid":"<uuid>","msg":<JSON-RPC-Response, gleiche id wie im Request>}`
- Relay → Client: `{"t":"notify","msg":<JSON-RPC-Notification>}`, z. B. `notifications/cancelled`
- Client → Relay (optional): `{"t":"tools_changed","tools":<tools/list-Result>}`
- WebSocket-Ping alle 20 s. Ohne Pong in 60 s gilt die Verbindung als tot.
- Es gibt nur eine Agent-Verbindung. Eine neue ersetzt die alte; die alte wird mit Code 4000 „replaced“ geschlossen.
- Maximale Nachrichtengröße 25 MB (Screenshots kommen als Base64).

**HTTP `/mcp` (MCP Streamable HTTP, Spec 2025-06-18 oder neuer):**
- Das Relay beantwortet selbst:
  - `initialize`: aus dem gecachten `init`, inklusive `instructions`, Protokollversion sauber ausgehandelt, neue
    `Mcp-Session-Id`
  - `notifications/initialized` mit 202
  - `ping`
  - `tools/list`: immer aus dem Cache, auch wenn der PC offline ist. Den Cache auf Platte halten und bei jedem
    `hello` bzw. `tools_changed` erneuern.
- Alles andere (vor allem `tools/call`) geht als `call` an den Client.
- Antworten als `application/json`. GET `/mcp` → 405. DELETE `/mcp` beendet die Session. Unbekannte Session → 404,
  dann initialisiert Langdock neu.
- Ist kein Agent verbunden, liefert `tools/call` ein Ergebnis mit `isError: true` und dem Text „Zeitspur ist gerade
  nicht verbunden (PC aus, im Ruhezustand oder offline).“ Nach 90 s ohne Antwort kommt eine Meldung zur
  Zeitüberschreitung, ebenfalls mit `isError: true`.

## Sicherheit (Pflicht)
- Gültiges TLS-Zertifikat (Let's Encrypt). Zeitspur akzeptiert nur `wss://` mit einem Zertifikat, dem Windows vertraut –
  selbstsignierte Zertifikate gehen nicht.
- `/mcp` nur von der Langdock-IP: Das prüft der Reverse-Proxy, zusätzlich der API-Key mit Vergleich in konstanter
  Zeit. Für alle anderen ist alles außer `/agent` eine 404.
- `/agent` ist aus dem ganzen Internet erreichbar, weil der PC unterwegs ist. Schutz: eigener Agent-Token und Sperre
  nach Fehlversuchen (fail2ban oder in der App).
- Zwei getrennte Secrets (Langdock-Key und Agent-Token), je mindestens 32 Byte zufällig, in
  `/etc/zeitspur-relay/relay.env` (0600). **Nie in den Chat, in Logs oder ins Repo.** Erzeuge sie direkt in die Datei
  und sag mir, wie ich sie selbst auslese.
- Protokolliert werden nur Metadaten: Zeit, Methode, Werkzeugname, Dauer, Status, Antwortgröße. Keine Argumente,
  keine Ergebnisse. Ins journald, 30 Tage.
- systemd-Härtung: eigener User, `NoNewPrivileges`, `ProtectSystem=strict` usw.
- Steht der Server bei mir zuhause: Er darf mein Heimnetz nicht erreichen (eigenes VLAN/DMZ, Firewall). Ausgehend nur
  Internet (DNS, NTP, HTTP/S für Updates und ACME), eingehend nur 443.
- Unattended Upgrades an.

## Vorgehen
1. **Bestandsaufnahme**, nur lesend:
   - Wo läufst du gerade? (VPS, Proxmox-Node, LXC, VM …) Betriebssystem, freie Ressourcen, Netzwerk
   - Gibt es schon einen Reverse-Proxy auf 443 (Caddy, Traefik, nginx, NPM, HAProxy)? Dann binden wir uns dort per
     Hostname ein, statt einen zweiten aufzumachen.
   - Bei einem Server zuhause: **öffentliche IPv4 oder DS-Lite/CGNAT?** WAN-IP des Routers mit `curl -4 ifconfig.me`
     vergleichen. Bei CGNAT stopp und frag mich nach Alternativen (kleiner VPS, Cloudflare Tunnel).
2. **Fragen an mich, gesammelt:** Hostname (z. B. `zeitspur.<meine-domain>`), DNS-Anbieter, bei einem Server zuhause
   Router-Modell und VLAN/DMZ, ob das Repo auf GitHub soll (dann privat).
3. **Umgebung:** Debian (aktuell stabil), klein reicht: 1 vCPU, 512 MB RAM, 4 GB Disk, startet nach Reboot von selbst.
   Bei Proxmox als unprivilegierter LXC mit eingeschalteter Firewall.
4. **Relay bauen:** Python 3, möglichst nur eine Abhängigkeit (aiohttp für HTTP und WebSocket). Code unter
   `/opt/zeitspur-relay` mit git, venv und systemd-Unit. Reverse-Proxy: Caddy mit automatischem Let's Encrypt, außer es
   gibt schon einen. Die echte Client-IP sauber durchreichen (`trusted_proxies`), sonst greift die IP-Sperre nicht.
5. **Fake-Agent** `tools/fake_agent.py`: verbindet sich wie Zeitspur und meldet die 7 Werkzeuge mit Dummy-Antworten.
   Damit lässt sich alles testen, bevor das echte Zeitspur verbunden wird.
6. **Tests:**
   - pytest für das Protokoll
   - curl: ohne Key 401, falsche Quell-IP 403 oder 404, unbekannte Session 404, falscher Agent-Token 401
   - Ende-zu-Ende mit Fake-Agent und einem MCP-Client (Python-`mcp`-SDK oder MCP Inspector) gegen `/mcp`
   - Ein Test von außen: Frag mich, damit ich ihn z. B. vom Handy-Hotspot aus mache.
7. **Anleitung für mich** im README:
   - **Zeitspur:** Plugins → „Langdock“ (Kategorie KI-Zugänge) → Hinzufügen. Relay-Adresse `wss://<host>/agent` und
     den Agent-Token eintragen, „Speichern & testen“. „Screenshots an Langdock geben“ bleibt aus.
     Im Tray-Menü schaltet „Langdock-Zugang“ den Zugang jederzeit ab.
   - **Langdock:** Integrations → MCP-Server hinzufügen, URL `https://<host>/mcp`, Authentifizierung per API-Key mit
     dem passenden Header, Name „Zeitspur“. **Nur für mich freigeben, nicht für den Workspace** – sonst können andere
     mit demselben Key meinen Verlauf abfragen.
   - Betrieb: Update, Secrets rotieren, typische Fehlerbilder.

## Ergebnis
- Laufender Dienst, startet nach einem Reboot von selbst (Reboot getestet)
- Repo mit Code, Tests, `PROTOKOLL.md`, Caddyfile, systemd-Unit, Installationsskript und README
- Kurzer Abschlussbericht: was läuft, was getestet ist, was offen ist und was ich selbst tun muss (DNS,
  Portweiterleitung, Zeitspur- und Langdock-Einrichtung, Secrets in den Passwort-Safe)

## Nicht tun
- Kein Docker nötig, keine Drittanbieter-Tunnel ohne meine Zustimmung
- Nichts an bestehenden Diensten, Containern, VMs oder am Router ändern, ohne mich vorher zu fragen
- Keine Secrets ausgeben, keine Inhalte von Zeitspur-Antworten protokollieren
