"""Zeitspur-Oberflaeche mit Demo-Daten - fuer die Vorschau im Browser und fuer Screenshots (README).

Baut in einem Wegwerf-Datenordner eine Demo-Datenbank (seed_demo_db) und legt Ereignisse der Plugins an - erzeugt
mit deren eigenen Zeilen-Bausteinen, also so, wie die Plugins sie wirklich speichern. Darueber laeuft die echte
Bridge. Alle Namen und Inhalte sind erfunden; die eigene Installation wird nicht beruehrt.

  .venv\\Scripts\\python tools\\demo_ui.py --preview dist\\ui-preview.html
      schreibt die fertige Seite mit eingebetteten Demo-Antworten (im Browser oeffnen; ?setup zeigt die
      Ersteinrichtung, ?plugins den Plugin-Browser)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_PLUGINS = ["outlook", "calls_local", "outlook_mail", "notifications", "browser_history", "git", "github",
                "pc_times", "wifi", "dawarich"]
# Erfundene Orte fuer die Standort-Spur (Koordinaten wie im README-Beispiel; nichts davon ist ein echter Ort)
BUERO = (52.5163, 13.3777)
ZUHAUSE = (52.4380, 13.3200)
BISTRO = (52.5225, 13.3880)
KNOWN_PLACES = ["Büro;52.5163;13.3777;200", "Zuhause;52.4380;13.3200;150"]


def _ms(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm).timestamp() * 1000)


def demo_gps(day: date) -> list[dict]:
    """Ein GPS-Tag vom Handy (Plugin Dawarich): morgens Zuhause, Fahrt ins Buero, mittags zu Fuss ins Bistro
    (der PC ist dann im Standby), abends zurueck. Als Rohpunkte - Aufenthalte und Fahrten macht das Plugin selbst."""
    from zeitspur import dawarich
    from zeitspur.location import Point

    rnd = random.Random(7)   # eigener Zufall, damit die Bildschirm-Demo daneben unveraendert bleibt
    t = lambda hh, mm: _ms(day, hh, mm)  # noqa: E731
    points: list[Point] = []

    def stay(place: tuple[float, float], start: int, end: int, every_min: int) -> None:
        for ts in range(start, end + 1, every_min * 60_000):
            points.append(Point(ts, place[0] + rnd.uniform(-2e-4, 2e-4), place[1] + rnd.uniform(-3e-4, 3e-4), 15.0))

    def move(a: tuple[float, float], b: tuple[float, float], start: int, end: int, *, drive: bool) -> None:
        # Auto: sanft an- und abfahren, am schnellsten in der Mitte (doppelter Durchschnitt); zu Fuss gleichmaessig
        n = max(2, (end - start) // 30_000)
        for i in range(n + 1):
            x = i / n
            s = x - math.sin(2 * math.pi * x) / (2 * math.pi) if drive else x
            points.append(Point(start + (end - start) * i // n, a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s, 8.0))

    evening = lambda hh, mm: _ms(day - timedelta(days=1), hh, mm)  # noqa: E731
    stay(ZUHAUSE, evening(19, 30), evening(23, 30), 30)
    stay(ZUHAUSE, t(6, 40), t(7, 29), 10)
    move(ZUHAUSE, BUERO, t(7, 30), t(7, 50), drive=True)
    stay(BUERO, t(7, 52), t(12, 5), 15)
    move(BUERO, BISTRO, t(12, 7), t(12, 19), drive=False)
    stay(BISTRO, t(12, 20), t(12, 36), 4)
    move(BISTRO, BUERO, t(12, 37), t(12, 49), drive=False)
    stay(BUERO, t(12, 50), t(17, 32), 15)
    move(BUERO, ZUHAUSE, t(17, 33), t(17, 53), drive=True)
    stay(ZUHAUSE, t(17, 55), t(22, 0), 20)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000).astimezone().isoformat()  # noqa: E731
    visits = [{"name": "Bistro am Markt", "started_at": iso(t(12, 20)), "ended_at": iso(t(12, 36))}]
    return dawarich.events_from_points(points, visits, evening(0, 0), t(23, 59))   # mit dem Vorabend


def demo_events(day: date) -> dict[str, list[dict]]:
    """Ereignisse eines Arbeitstags je Plugin - gebaut mit den Zeilen-Bausteinen der Plugins selbst."""
    from zeitspur import browser_history, calls_local, git_commits, github_activity, notifications, outlook_mail
    from zeitspur.outlook import build_event
    from zeitspur.teams_local import MicUsage

    t = lambda hh, mm: _ms(day, hh, mm)  # noqa: E731
    zoom = r"C:#Program Files#Zoom#bin#Zoom.exe"
    phone = r"C:#Programs#STARFACE#STARFACE App.exe"
    visits = [browser_history.Visit(t(10, 15) + i * 90_000, url, title, "Edge") for i, (url, title) in enumerate([
        ("https://github.com/beispiel/projekt", "beispiel/projekt"), ("https://docs.python.org/3/", "Python-Doku"),
        ("https://github.com/beispiel/projekt/pulls", "Pull Requests"), ("https://de.wikipedia.org/wiki/Zeit", "Zeit"),
        ("https://stackoverflow.com/questions/1", "SQLite WAL – Stack Overflow")])]
    return {
        "outlook": [build_event(subject="Wochenplanung Team", start_ms=t(9, 0), end_ms=t(9, 30), location="Raum B2",
                                organizer="Erika Musterfrau", attendees=["Max Mustermann", "Erika Musterfrau"],
                                meeting_status=3, entry_id="demo-1").as_row(),
                    build_event(subject="Kundentermin Beispiel GmbH", start_ms=t(14, 0), end_ms=t(15, 0),
                                location="Teams", organizer="Max Mustermann", meeting_status=1,
                                entry_id="demo-2").as_row()],
        "calls_local": [calls_local.build_row(zoom, calls_local.identify(zoom), MicUsage(zoom, t(11, 0), t(11, 40)),
                                              t(11, 40), video=True, titles=[]),
                        calls_local.build_row(phone, calls_local.identify(phone), MicUsage(phone, t(13, 10), t(13, 18)),
                                              t(13, 18), video=False, titles=[])],
        "outlook_mail": [outlook_mail.build_row(kind="received", subject="Rückfrage Rechnung 4711", ts_ms=t(8, 40),
                                                sender="Max Mustermann", recipients=["Ich"], entry_id="m1"),
                         outlook_mail.build_row(kind="sent", subject="Angebot Serverumzug", ts_ms=t(10, 5),
                                                sender="Ich", recipients=["Erika Musterfrau"], entry_id="m2"),
                         outlook_mail.build_row(kind="sent", subject="Protokoll Wochenplanung", ts_ms=t(15, 20),
                                                sender="Ich", recipients=["Team", "Erika Musterfrau"], entry_id="m3")],
        "notifications": [notifications.build_row(notifications.Toast(1, t(9, 42), "MSTeams_8wekyb3d8bbwe!MSTeams",
                                                                      ("Erika Musterfrau", "Kurze Frage zum Angebot")),
                                                  "Teams", store_text=True),
                          notifications.build_row(notifications.Toast(2, t(12, 30), "5319275A.WhatsAppDesktop_x!App",
                                                                      ("Max Mustermann", "Mittagessen um eins?")),
                                                  "WhatsApp", store_text=True)],
        "browser_history": browser_history.phases(visits),
        "git": [git_commits.commit_row("projekt", git_commits.Commit(f"{n:040x}", ts, "Max Mustermann",
                                                                    "max@beispiel.de", msg))
                for n, ts, msg in ((1, t(11, 55), "Plugin-Browser: Kacheln und Suche"),
                                   (2, t(16, 20), "Tests für die neuen Plugins"))],
        "github": [github_activity.event_row({"id": "9", "type": "PushEvent", "created_at":
                                              datetime.fromtimestamp(t(16, 25) / 1000).astimezone().isoformat(),
                                              "repo": {"name": "beispiel/projekt"},
                                              "payload": {"ref": "refs/heads/main", "size": 2}})],
        "pc_times": [{"ext_id": "pc-1", "ts_start": t(7, 55), "ts_end": t(12, 5), "subject": "PC an (Start → Standby)",
                      "category": "pc_on", "extra": "{}"},
                     {"ext_id": "pc-2", "ts_start": t(12, 50), "ts_end": t(17, 30),
                      "subject": "PC an (Standby beendet → Herunterfahren)", "category": "pc_on", "extra": "{}"}],
        "wifi": [{"ext_id": "wifi-1", "ts_start": t(7, 56), "ts_end": t(17, 30), "subject": "Büro (WLAN Firma-WLAN)",
                  "location": "Büro", "category": "wifi", "extra": json.dumps({"ssid": "Firma-WLAN", "place": "Büro"})}],
        "dawarich": demo_gps(day),
    }


class DemoApp:
    """Was die Bridge von der App braucht - ohne Aufnahme, Tray und Threads."""

    def __init__(self, cfg, storage, key, data_dir: Path):
        self.cfg, self.storage, self.key, self.data_dir = cfg, storage, key, data_dir
        self.ready = threading.Event()
        self.ready.set()
        self.first_run = False
        self.engine = None
        self.window = None
        self.event_sync = None
        self._visible = True

    def capture_state(self):
        from zeitspur.capture import STATE_LABELS, CaptureState
        return CaptureState.RECORDING, STATE_LABELS[CaptureState.RECORDING], ""

    def is_paused(self) -> bool:
        return False

    def ocr_available(self) -> bool:
        return True

    def update_status(self):
        return None

    def status_summary(self) -> dict:
        from zeitspur.app import App
        return App.status_summary(self)

    def plugin_list(self) -> list[dict]:
        from zeitspur import plugins
        return [p.describe(self.cfg) for p in plugins.PLUGINS]

    def request_event_sync(self, day) -> None:
        pass


def build_demo(work: Path, days: int = 2):
    """Demo-Datenordner, Datenbank und Bridge. Liefert (bridge, Demo-Tag)."""
    os.environ["ZEITSPUR_DATA_DIR"] = str(work)
    random.seed(4711)
    from tools.seed_demo_db import seed
    from zeitspur.config import Config, ensure_data_dir, key_path, save_config
    from zeitspur.crypto import load_or_create_key
    from zeitspur.storage import Storage
    from zeitspur.timeline_ui.bridge import Bridge

    ensure_data_dir()
    cfg = Config(installed_plugins=list(DEMO_PLUGINS), known_places=list(KNOWN_PLACES),
                 plugin_settings={"git": {"folders": [str(work)], "authors": []},
                                  "github": {"username": "beispiel"},
                                  "wifi": {"places": ["Firma-WLAN = Büro"]}})
    save_config(cfg)
    from zeitspur import dawarich

    dawarich.save_credentials("https://dawarich.beispiel.de", "demo")   # nur im Wegwerf-Ordner, gilt als eingerichtet
    key = load_or_create_key(key_path())
    storage = Storage(cfg.resolved_db_path, key)
    seed(storage, days)
    # Demo-Tag ist gestern: Die Standort-Spur reicht nie ueber "jetzt" hinaus - mit heute hingen die Bilder von
    # der Uhrzeit ab (kurz nach Mitternacht fehlte die Zeile "Orte" ganz).
    day = date.today() - timedelta(days=1)
    for source, rows in demo_events(day).items():
        storage.replace_events(source, _ms(day, 0, 0), _ms(day + timedelta(days=1), 0, 0), rows)
    return Bridge(DemoApp(cfg, storage, key, work)), day


# Ersetzt window.pywebview.api durch eingebettete Demo-Antworten. R.mode (oder ?demo=...) waehlt die Ansicht:
# "" Zeitstrahl, "detail" ein Eintrag mit Bild und Text, "plugins" Plugin-Browser mit Detailansicht,
# "setup" Ersteinrichtung mit einigen gewaehlten Plugins, "orte" der Tagesablauf der Standort-Spur.
_FAKE_API = """
<script nonce="__NONCE__">
(() => {
  const R = /*__DEMO__*/null;
  const mode = new URLSearchParams(location.search).get("demo") || R.mode || "";
  if (mode === "setup") { Object.assign(R.state, R.firstRun); delete R.state.stats; }
  window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
    switch (name) {
      case "get_state": return R.state;
      case "get_day": return R.day;
      case "list_plugins": return R.plugins;
      case "get_config": return R.config;
      case "get_entry": return R.entries[args[0]] || null;
      case "get_thumbnail": return R.thumbs[args[0]] || null;
      case "search": return [];
      case "test_plugin": return { message: "Demo: Verbindung erfolgreich." };
      default: return { ok: true, message: "Demo", removed_events: 0 };
    }
  } }) };
  const later = (ms, fn) => setTimeout(fn, ms);
  window.addEventListener("load", () => {
    if (mode === "plugins") later(300, () => {
      document.getElementById("pluginsBtn").click();
      later(400, () => {
        const tile = [...document.querySelectorAll(".plugin-tile")].find((t) => t.textContent.includes(R.focusPlugin));
        if (tile) tile.click();
      });
    });
    if (mode === "detail") later(500, () => {
      const block = [...document.querySelectorAll(".block")]
        .find((b) => b.title.startsWith(R.focusTitle) && b.title.includes(R.focusTime));
      if (!block) return;
      // mitten in den Abschnitt des vorbereiteten Eintrags klicken - die Blockmitte laege bei gerader Anzahl genau
      // auf der Grenze zweier Eintraege, und die Oberflaeche wuerde je nach Rundung den anderen oeffnen
      const r = block.getBoundingClientRect();
      const x = r.left + r.width * (R.focusIndex + 0.5) / R.focusCount;
      block.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: x, clientY: r.top + 6 }));
      later(600, () => document.getElementById("detail").scrollIntoView({ block: "end" }));
    });
    if (mode === "orte") later(500, () => document.getElementById("mapBtn").click());
    if (mode === "setup") later(600, () => {
      for (const p of document.querySelectorAll(".pick")) {
        if (R.pickPlugins.some((n) => p.textContent.includes(n))) p.querySelector("input").click();
      }
    });
  });
})();
</script>
"""
MODES = ("", "detail", "plugins", "setup", "update", "orte")


def changelog_sections() -> list[tuple[str, str]]:
    """(Version, Text) je Abschnitt aus CHANGELOG.md, neuester zuerst - so, wie das Release ihn als Notizen mitnimmt."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = []
    for part in text.split("\n## ")[1:]:
        version, _, body = part.partition("\n")
        sections.append((version.strip(), body.strip()))
    return sections


def demo_update() -> dict:
    """Ein angebotenes Update fuer die Vorschau, wie es Nutzer der Vorgaengerversion sehen: die Version des obersten
    CHANGELOG-Abschnitts mit dessen Notizen."""
    from zeitspur import updater
    (version, notes), (previous, _) = changelog_sections()[:2]
    return {"status": "ready", "current": previous, "version": version, "notes": notes, "error": "",
            "progress": 1.0, "checked_ms": 0, "auto": True, "idle_s": 300, "release_url": updater.release_page(version)}


def _demo_payload(bridge, day: date) -> dict:
    """Alle Antworten, die die Oberflaeche braucht - einmal von der echten Bridge geholt."""
    demo = {"state": bridge.get_state(), "day": bridge.get_day(day.isoformat()), "plugins": bridge.list_plugins(),
            "config": bridge.get_config(), "entries": {}, "thumbs": {},
            "focusPlugin": "Gespräche in allen Apps", "pickPlugins": ["Outlook-Kalender", "Gespräche in allen Apps",
                                                                    "PC-Zeiten", "Browser-Verlauf"]}
    # So, wie andere Zeitspur bekommen: die Release-Ausgabe mit Update-Dienst (die Karte ist ab Werk aus)
    demo["state"]["features"] = {**demo["state"]["features"], "updates": True}
    demo["state"]["map_enabled"] = False
    demo["state"]["today"] = day.isoformat()   # die Oberflaeche startet mit diesem Tag (Datumsfeld passend zur Ansicht)
    # Ersteinrichtung wie beim ersten Start: noch keine Datenbank, die Aufnahme laeuft noch nicht
    from zeitspur.capture import STATE_LABELS, CaptureState
    demo["firstRun"] = {"first_run": True, "ready": False, "capture_state": CaptureState.STARTING.value,
                        "capture_label": STATE_LABELS[CaptureState.STARTING], "capture_detail": ""}
    # Fuer die Detailansicht: der laengste Bildschirm-Block; angeklickt wird seine Mitte
    blocks = [b for lane in demo["day"]["lanes"] for b in lane["blocks"] if b.get("window_title")]
    block = max(blocks, key=lambda b: b["ts_end"] - b["ts_start"])
    index = block["count"] // 2 if block["count"] > 1 else 0
    entry_id = block["ids"][min(index, len(block["ids"]) - 1)]
    demo["entries"][entry_id] = bridge.get_entry(entry_id)
    demo["thumbs"][entry_id] = bridge.get_thumbnail(entry_id, 900)
    hm = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%H:%M")  # noqa: E731
    demo["focusIndex"], demo["focusCount"] = index, max(1, block["count"])
    demo["focusTitle"] = block["window_title"]
    demo["focusTime"] = f"{hm(block['ts_start'])} – {hm(block['ts_end'])}"   # wie im Tooltip des Blocks
    return demo


def _page(demo: dict, mode: str) -> str:
    from zeitspur.timeline_ui.bridge import build_html

    demo = {**demo, "mode": mode, "state": dict(demo["state"])}
    if mode == "update":
        update = demo_update()
        demo["state"]["update"] = update
        demo["state"]["version"] = update["current"]
        demo["state"]["features"] = {**demo["state"]["features"], "updates": True}
    payload = json.dumps(demo, ensure_ascii=False).replace("</", "<\\/")
    html = build_html()
    # Die Seite laesst per CSP nur Skripte mit ihrem Nonce zu - die Demo-Antworten brauchen denselben, sonst
    # blockiert der Browser sie und die Oberflaeche bleibt leer (so in 0.4.0 unbemerkt passiert).
    nonce = re.search(r'<script nonce="([^"]+)">', html)
    idx = html.rfind("<script", 0, html.rfind("window.pywebview"))   # vor dem App-Skript einfuegen
    if nonce is None or idx < 0:
        raise RuntimeError("App-Skript oder CSP-Nonce in build_html() nicht gefunden - demo_ui.py anpassen")
    fake = _FAKE_API.replace("__NONCE__", nonce.group(1)).replace("/*__DEMO__*/null", payload)
    return html[:idx] + fake + html[idx:]


def write_previews(out_dir: Path, modes=MODES) -> dict[str, Path]:
    """Je Ansicht eine Seite mit eingebetteten Demo-Antworten: ui-preview[-<ansicht>].html."""
    with tempfile.TemporaryDirectory(prefix="zeitspur-demo-") as tmp:
        bridge, day = build_demo(Path(tmp))
        demo = _demo_payload(bridge, day)
        bridge._app.storage.close()
        bridge._close()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for mode in modes:
        path = out_dir / (f"ui-preview-{mode}.html" if mode else "ui-preview.html")
        path.write_text(_page(demo, mode), encoding="utf-8")
        written[mode] = path
    return written


def write_preview(out: Path, *, with_update: bool = False) -> Path:
    mode = "update" if with_update else ""
    written = write_previews(out.parent, modes=(mode,))
    written[mode].replace(out)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--preview", type=Path, help="HTML-Datei mit eingebetteten Demo-Antworten schreiben")
    p.add_argument("--update", action="store_true", help="dazu ein angebotenes Update mit Changelog zeigen")
    args = p.parse_args()
    if args.preview:
        print(write_preview(args.preview, with_update=args.update))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
