"""Einstiegspunkt fuer Zeitspur.exe (und `python -m zeitspur.service_main`).

Argumente:
  --show       Zeitstrahl-Fenster direkt anzeigen (bzw. der laufenden Instanz signalisieren)
  --quit       laufende Instanz beenden
  --autostart  Start ueber den Run-Schluessel: Fenster versteckt, Aufnahme startet verzoegert
  --debug      DevTools/Debug-Logging
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__

log = logging.getLogger("zeitspur.service")


def _setup_logging(level: str, log_dir: Path) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        try:
            sys.stderr.reconfigure(errors="backslashreplace")  # Konsolen-Codepage darf das Logging nicht stoeren
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Zeitspur", description="Zeitspur Hintergrunddienst")
    p.add_argument("--show", action="store_true", help="Zeitstrahl-Fenster anzeigen")
    p.add_argument("--quit", action="store_true", help="laufende Instanz beenden")
    p.add_argument("--autostart", action="store_true", help="Start ueber Autostart (versteckt, verzoegerte Aufnahme)")
    p.add_argument("--debug", action="store_true", help="Debug-Logging und DevTools")
    p.add_argument("--version", action="store_true")
    p.add_argument("--mcp", action="store_true", help="als MCP-Server (stdio) laufen, weitere Argumente gehen an den MCP-Server")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if "--mcp" in raw_args:
        # MCP-Modus: dieselbe EXE arbeitet als stdio-MCP-Server (Alternative zu ZeitspurMCP.exe,
        # falls ein Virenscanner die separate EXE blockiert). Kein Fenster, kein Tray, keine Einzelinstanz.
        # Ueber mcp_server.run() (statt nur main()), damit derselbe harte os._exit greift wie bei der
        # separaten EXE - sonst kann ein haengender stdio-/anyio-Thread die EXE nach Verbindungsende sperren.
        from . import mcp_server

        sys.argv = [sys.argv[0]] + [a for a in raw_args if a != "--mcp"]
        mcp_server.run()  # beendet den Prozess selbst via os._exit
        return 0  # unerreichbar

    from . import winutil

    winutil.set_dpi_awareness()  # vor WinForms/mss
    args = parse_args(raw_args)
    if args.version:
        if sys.stdout:
            print(f"{APP_NAME} {__version__}")
        return 0
    if args.quit:
        return 0 if winutil.signal_event(winutil.EVENT_QUIT) else 1

    instance = winutil.SingleInstance()
    if not instance.acquire():
        # Bereits aktiv: Fenster der laufenden Instanz anzeigen
        winutil.signal_event(winutil.EVENT_SHOW)
        return 0
    try:
        return _run(args)
    finally:
        instance.release()


def _run(args: argparse.Namespace) -> int:
    """Der Dienst selbst - die Einzelinstanz ist bereits gesichert."""
    from . import winutil
    from .config import Config, ConfigError, ensure_data_dir, is_first_run, load_config, logs_dir
    from .splash import Splash

    data_dir = ensure_data_dir()
    config_error: str | None = None
    try:
        cfg = load_config()
    except ConfigError as e:
        cfg = Config()
        config_error = str(e)
    _setup_logging("DEBUG" if args.debug else cfg.log_level, logs_dir())
    log.info("%s %s startet (pid %d, autostart=%s, python %s)", APP_NAME, __version__, os.getpid(), args.autostart,
             sys.version.split()[0])
    from . import autostart

    autostart.refresh_if_stale()  # zeigt der Autostart noch auf eine alte Programmdatei, wird er nachgezogen

    # Niemals in eine Sandbox-Kopie aufnehmen: sonst entstehen zwei Datenbanken, die auseinanderdriften
    # (siehe winutil.container_shadow). Lieber gar nicht starten als still in die falsche Datei schreiben.
    shadow = winutil.container_shadow(data_dir, probe=True)
    if shadow:
        log.error("Datenordner wird in einen App-Container umgeleitet: %s -> %s", data_dir, shadow)
        winutil.message_box(
            "Zeitspur läuft in einer App-Sandbox. Schreibzugriffe auf\n\n"
            f"{data_dir}\n\nwerden von Windows nach\n\n{shadow}\n\numgeleitet. Der Dienst würde in eine "
            "Kopie aufnehmen, während die eigentliche Installation unverändert bleibt.\n\n"
            "Zeitspur wurde deshalb nicht gestartet. Bitte über die Verknüpfung im Startmenü starten.",
            flags=0x10)
        return 2
    # Ohne WebView2 bricht pywebview nicht ab, sondern weicht still auf den Internet-Explorer-Renderer aus -
    # darin laeuft der Zeitstrahl nicht. Deshalb vorher pruefen und klar sagen, was fehlt.
    if winutil.IS_WINDOWS and not winutil.webview2_version():
        log.error("WebView2-Runtime nicht gefunden - Zeitspur startet nicht")
        offer_webview2_download()
        return 1
    if config_error:
        log.error("config.yaml ungueltig, Standardwerte aktiv: %s", config_error)
        winutil.message_box(f"Die Datei config.yaml ist ungültig und wird ignoriert (Standardwerte aktiv):\n\n{config_error}",
                            flags=0x30)
    first_run = is_first_run() and config_error is None
    # Direkt nach einem Update (das Setup startet Zeitspur neu): Startfenster zeigen, damit man sieht, dass es
    # wieder da ist - und das Hauptfenster nur, wenn es vor dem Update offen war (siehe updater.Updater.install).
    from .updater import read_pending

    pending = read_pending(data_dir)
    show = bool(args.show or first_run or (pending and pending.get("show")))

    # Kommt das Fenster, sofort ein Startfenster zeigen: Bis der Zeitstrahl steht, vergehen sonst mehrere
    # Sekunden ohne jede Rueckmeldung - Python, .NET und WebView2 starten kalt, beim ersten Start prueft der
    # Virenscanner zudem tausende Dateien. Der Autostart bleibt still (ausser die Ersteinrichtung steht noch aus).
    if pending:
        splash = Splash(f"Update auf {pending['to']} – wird gestartet …",
                        hint="Einstellungen und Aufnahmen bleiben erhalten.")
    else:
        splash = Splash()
    if show or pending:
        splash.show()
    try:
        # WebView2 soll keine Crash-Dumps (mit Bildinhalten) schreiben. setdefault waere wirkungslos, falls die
        # Variable schon (system-/elternprozessseitig) gesetzt ist -> Flags erzwingen und vorhandene Argumente behalten.
        # Eigene Programmkennung: OpenStreetMap verlangt fuer Kartenkacheln eine identifizierende
        # User-Agent-Zeile. Ohne sie antwortet der Kachel-Server mit 403 "Access blocked".
        # Bewusst ohne Leerzeichen, damit die Argumentzeile nicht zerfaellt.
        _wv_flags = "--disable-breakpad --disable-crash-reporter --user-agent=Zeitspur/" + __version__
        _wv_prev = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
        if "--disable-crash-reporter" not in _wv_prev:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (_wv_prev + " " + _wv_flags).strip()

        if not pending:
            splash.set_text("Programmteile werden geladen …")
        import webview

        from .app import App
        from .timeline_ui.bridge import PAGE_BACKGROUND, build_html

        app = App(cfg, data_dir, first_run=first_run, autostart=args.autostart)
        # Mit Startfenster wird das Hauptfenster versteckt erzeugt und erst gezeigt, wenn seine Seite geladen
        # ist (App.reveal_when_loaded) - sonst stuende waehrend des WebView2-Starts ein leeres weisses Fenster da.
        reveal_later = show and splash.visible
        window = webview.create_window(
            "Zeitspur", html=build_html(), js_api=app.bridge,
            width=1280, height=860, min_size=(960, 640), hidden=not show or reveal_later, text_select=True,
            background_color=PAGE_BACKGROUND[winutil.apps_use_dark_theme()],
        )
        window.events.closing += app.on_closing
        window.events.loaded += app.on_loaded
        app.window = window
        app._visible = show and not reveal_later
        if reveal_later:
            app.reveal_when_loaded(splash)
            if not pending:
                splash.set_text("Oberfläche wird aufgebaut …")
        elif splash.visible:
            # Neustart nach einem Update im Leerlauf: Startfenster, bis alles geladen ist - dann weiter im Infobereich
            app.reveal_when_loaded(splash, show_window=False)
        try:
            webview.start(func=app.bootstrap, private_mode=True, storage_path=str(data_dir / "webview"), debug=args.debug)
        except Exception as e:
            log.exception("GUI-Schleife abgebrochen")
            splash.close()
            winutil.message_box(f"Zeitspur konnte das Fenster nicht starten:\n{e}\n\n"
                                "Ist die Microsoft Edge WebView2-Runtime installiert?", flags=0x10)
            return 1
        finally:
            app.shutdown()
        return 0
    finally:
        splash.close()   # spaetestens hier, auch bei Fehlern - sonst stuende es hinter der Fehlermeldung weiter


IDYES = 6
MB_YESNO_ERROR = 0x04 | 0x10   # MB_YESNO | MB_ICONERROR


def offer_webview2_download(message_box=None, open_url=None) -> bool:
    """Meldung "WebView2 fehlt" mit Ja/Nein - Ja oeffnet Microsofts Download-Seite. Liefert True bei Ja."""
    from . import winutil

    message_box = message_box or winutil.message_box
    if open_url is None:
        import webbrowser

        open_url = webbrowser.open
    antwort = message_box(
        "Zeitspur braucht für das Programmfenster die Microsoft Edge WebView2-Runtime – sie ist auf diesem PC "
        "nicht installiert.\n\nDie Installation dauert eine Minute und braucht keine Administratorrechte.\n\n"
        "Download-Seite von Microsoft jetzt öffnen?", flags=MB_YESNO_ERROR)
    if antwort == IDYES:
        open_url(winutil.WEBVIEW2_DOWNLOAD_URL)
        return True
    return False


def write_crash_log(prefix: str) -> Path | None:
    """Schreibt den aktuellen Traceback nach <Datenordner>/logs/crash.log - auch wenn Logging noch nicht steht."""
    import traceback

    try:
        base = os.environ.get("ZEITSPUR_DATA_DIR") or os.path.join(os.environ.get("LOCALAPPDATA", "."), APP_NAME)
        log_dir = Path(base) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "crash.log"
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"\n===== {prefix} {__version__} frozen={getattr(sys, 'frozen', False)} argv={sys.argv!r}\n")
            f.write(traceback.format_exc())
        return target
    except Exception:
        return None


def run() -> None:
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:
        write_crash_log("Zeitspur")
        logging.getLogger(__name__).exception("Unbehandelter Fehler")
        try:
            from . import winutil

            winutil.message_box("Zeitspur ist mit einem unerwarteten Fehler beendet worden. Details stehen im Protokoll "
                                "(Tray-Menü → Log-Ordner öffnen bzw. %LOCALAPPDATA%\\Zeitspur\\logs).", flags=0x10)
        except Exception:
            pass
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    run()
