"""Lebenszyklus des Dienstprozesses: Datenbank, Worker-Threads, Fenster, Tray, sauberes Beenden.

Thread-Modell:
- MainThread: pywebview-Schleife (webview.start). Genau ein Fenster fuer die gesamte Laufzeit,
  Schliessen versteckt es nur - ausser `quitting` ist gesetzt.
- bootstrap (von webview.start gestartet): Tray-Thread, IPC-Thread, Datenbank oeffnen, Worker starten.
- Tray/IPC/js_api-Callbacks setzen nur Flags oder rufen thread-sichere App-Methoden auf; joinen nie.
- shutdown() laeuft im MainThread, nachdem webview.start() zurueckgekehrt ist.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import fields
from datetime import date
from pathlib import Path

from . import __version__, edition, plugins, timeutil, winutil
from .capture import STATE_LABELS, CaptureLoop, CaptureState
from .cleanup import CleanupReport, MaintenanceJob
from .config import Config, key_path, logs_dir, save_config
from .crypto import KeyProtectionError, WrongKeyError, load_or_create_key
from .events_sync import EventSyncThread, PluginObserverThread
from .ocr import TesseractEngine
from .ocr_worker import OcrWorker
from .storage import Storage
from .tray import TrayIcon

log = logging.getLogger(__name__)

RESTART_REQUIRED_FIELDS = {"db_path", "ocr_language", "ocr_psm", "ocr_min_confidence", "ocr_queue_max", "log_level"}
MB_YESNO, MB_ICONERROR, MB_ICONWARNING, IDYES = 0x4, 0x10, 0x30, 6
REVEAL_TIMEOUT_S = 45.0   # spaetestens dann erscheint das Fenster, auch wenn seine Seite nicht laedt


def _seconds_since_process_start() -> float | None:
    """Dauer seit Prozessstart - fuers Protokoll, damit sich langsame Starts auf anderen PCs nachvollziehen lassen."""
    try:
        import psutil

        return time.time() - psutil.Process().create_time()
    except Exception:
        return None


class App:
    def __init__(self, cfg: Config, data_dir: Path, *, first_run: bool, autostart: bool = False):
        self.cfg = cfg
        self.data_dir = data_dir
        self.first_run = first_run
        self.autostart = autostart
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.window_loaded = threading.Event()
        self.quitting = False
        self.storage: Storage | None = None
        self.key: bytes | None = None
        self.engine: TesseractEngine | None = None
        self.capture: CaptureLoop | None = None
        self.ocr_worker: OcrWorker | None = None
        self.maintenance: MaintenanceJob | None = None
        self.event_sync: EventSyncThread | None = None
        self.plugin_observer: PluginObserverThread | None = None
        self.updater = None             # updater.Updater - nur im Release-Build (edition.UPDATE_CHANNEL)
        self.tray: TrayIcon | None = None
        self.window = None
        self._visible = False
        self._splash = None             # Startfenster (service_main), bis das Fenster seinen Inhalt zeigt
        self._reveal_pending = False
        self._reveal_window = True
        self._reveal_lock = threading.Lock()
        self._reveal_timer: threading.Timer | None = None
        self._threads: list[threading.Thread] = []
        self._worker_lock = threading.Lock()  # schuetzt (Neu-)Start von Worker-Threads gegen parallele js_api-Aufrufe
        self._last_notice: dict[str, float] = {}
        from .timeline_ui.bridge import Bridge  # spaeter Import (Bridge braucht App-Typ nur fuer Typing)

        self.bridge = Bridge(self)

    # ------------------------------------------------------------------ Fenster
    def on_closing(self) -> bool:
        """pywebview-closing-Handler: False = Schliessen abbrechen (nur verstecken)."""
        if self.quitting:
            return True
        self.hide_window()
        return False

    def on_loaded(self) -> None:
        if not self.window_loaded.is_set():
            elapsed = _seconds_since_process_start()
            if elapsed is not None:
                log.info("Oberflaeche geladen, %.1f s nach Programmstart", elapsed)
        self.window_loaded.set()
        if not self._reveal() and self._visible:
            self._run_js("window.zeitspur && window.zeitspur.onShown()")

    def reveal_when_loaded(self, splash, timeout_s: float = REVEAL_TIMEOUT_S, show_window: bool = True) -> None:
        """Das Fenster wurde versteckt erzeugt und erscheint erst, wenn seine Seite geladen ist - bis dahin
        steht das Startfenster. Sonst stuende beim Start sekundenlang ein leeres weisses Fenster da, waehrend
        WebView2 hochfaehrt. Laedt die Seite nicht, erscheint das Fenster nach timeout_s trotzdem: Niemand
        soll vor einem Startfenster sitzen, das nie verschwindet.

        show_window=False (Neustart nach einem Update im Leerlauf): Das Startfenster zeigt nur, dass Zeitspur
        wieder da ist, und verschwindet dann; das Programm laeuft im Infobereich weiter wie vorher."""
        self._splash = splash
        self._reveal_window = show_window
        self._reveal_pending = True

        def fallback() -> None:
            if self._reveal():
                log.warning("Oberflaeche nach %.0f s nicht geladen - Startfenster geschlossen%s", timeout_s,
                            ", Fenster trotzdem angezeigt" if show_window else "")

        self._reveal_timer = threading.Timer(timeout_s, fallback)
        self._reveal_timer.name = "fenster-fallback"
        self._reveal_timer.daemon = True
        self._reveal_timer.start()

    def _reveal(self) -> bool:
        """Zeigt das Fenster und schliesst das Startfenster - genau einmal. True, wenn es jetzt geschehen ist."""
        with self._reveal_lock:
            if not self._reveal_pending:
                return False
            self._reveal_pending = False
        if self._reveal_timer is not None:
            self._reveal_timer.cancel()
        if not self.quitting and self._reveal_window:
            self.show_window()   # erst das Hauptfenster, dann das Startfenster weg - so entsteht keine Luecke
        splash, self._splash = self._splash, None
        if splash is not None:
            splash.close()
        return True

    def show_window(self) -> None:
        """Holt das Fenster zurueck - egal ob es versteckt (Tray) oder minimiert ist.

        show() und restore() werden einzeln abgesichert: frueher lagen beide in einem try, ein Fehler
        in show() hat restore() mitverschluckt und ein minimiertes Fenster blieb unten.
        """
        if self.window is None:
            return
        for name in ("show", "restore"):
            try:
                getattr(self.window, name)()
            except Exception:
                log.debug("Fenster: %s() fehlgeschlagen", name, exc_info=True)
        # Nach show()/restore() ist das Fenster nicht mehr geparkt - erst jetzt laesst sich pruefen,
        # ob es (z. B. nach einem Monitorwechsel) ausserhalb aller Bildschirme liegt.
        try:
            winutil.ensure_window_on_screen("Zeitspur")
        except Exception:
            log.debug("Fensterposition nicht pruefbar", exc_info=True)
        self._visible = True
        self._run_js("window.zeitspur && window.zeitspur.onShown()")

    def hide_window(self) -> None:
        if self.window is None:
            return
        self._visible = False
        try:
            self.window.hide()
            self._run_js("window.zeitspur && window.zeitspur.onHidden()")
        except Exception:
            log.debug("Fenster konnte nicht versteckt werden", exc_info=True)

    def _run_js(self, script: str) -> None:
        """Fuehrt JavaScript im Fenster aus - grundsaetzlich aus einem Nebenthread.

        Wichtig: pywebview fuehrt run_js synchron aus (evaluate_js wartet mit semaphore.acquire() auf
        eine Rueckmeldung, die WebView2 ueber den GUI-Thread zustellt). Wird das AUF dem GUI-Thread
        aufgerufen, wartet dieser auf sich selbst und haengt dauerhaft. Genau das passierte im
        closing-Handler, den pywebview inline auf dem GUI-Thread ausfuehrt (Event(..., should_lock=True)):
        Nach einmaligem Schliessen war der GUI-Thread tot, jedes spaetere show() lief ins Leere und das
        Fenster liess sich nie wieder oeffnen. Deshalb nie direkt aufrufen.
        """
        if self.window is None or not self.window_loaded.is_set():
            return

        def _call() -> None:
            try:
                self.window.run_js(script)
            except Exception:
                log.debug("run_js fehlgeschlagen", exc_info=True)

        threading.Thread(target=_call, name="run-js", daemon=True).start()

    # ------------------------------------------------------------------ Start
    def bootstrap(self) -> None:
        """Wird von webview.start(func=...) in einem eigenen Thread ausgefuehrt."""
        try:
            self.tray = TrayIcon(self)
            self._start_thread(self.tray.run, "tray")
            self._start_thread(self._ipc_loop, "ipc")
            if self.first_run:
                log.info("Ersteinrichtung: warte auf Eingaben im Fenster")
                return
            if self.open_storage():
                self.start_workers()
                self.ready.set()
                self._run_js("window.zeitspur && window.zeitspur.refresh()")
            else:
                self.request_quit()
        except Exception:
            log.exception("Start fehlgeschlagen")
            self.request_quit()

    def _start_thread(self, target, name: str) -> threading.Thread:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)
        return t

    def open_storage(self) -> bool:
        try:
            key = load_or_create_key(key_path())
            storage = Storage(self.cfg.resolved_db_path, key)
        except (KeyProtectionError, WrongKeyError) as e:
            log.error("Datenbank nicht lesbar: %s", e)
            # Unbeaufsichtigt (Autostart) darf hier KEIN modaler Dialog stehenbleiben - sonst wartet er
            # ungesehen und es wird stundenlang nichts aufgezeichnet. Dann lieber selbst heilen:
            # die unlesbare Datei wird nur umbenannt, nie geloescht.
            keep_key = isinstance(e, WrongKeyError)
            return self._offer_database_reset(str(e), ask=not self.autostart, keep_key=keep_key)
        except Exception as e:
            log.exception("Datenbank konnte nicht geoeffnet werden")
            winutil.message_box(f"Die Datenbank konnte nicht geöffnet werden:\n{e}", flags=MB_ICONERROR)
            return False
        self.key, self.storage = key, storage
        log.info("Datenbank geoeffnet: %s (%d Eintraege)", storage.db_path, storage.stats()["entries"])
        return True

    def _offer_database_reset(self, reason: str, *, ask: bool = True, keep_key: bool = True) -> bool:
        """Legt die unlesbare Datenbank beiseite und beginnt neu.

        `ask=False` heilt ohne Rueckfrage (Autostart). `keep_key=True` behaelt key.bin: Bei einem
        Schluessel-/Datei-Konflikt ist der Schluessel selbst noch in Ordnung und wird fuer spaetere
        Rettungsversuche an Sicherungen gebraucht - nur wenn der Schluessel unbrauchbar ist, fliegt er mit.
        """
        text = ("Die verschlüsselte Datenbank kann mit dem gespeicherten Schlüssel nicht geöffnet werden.\n\n"
                f"Grund: {reason}\n\n"
                "Das passiert z. B. nach einem Zurücksetzen des Windows-Passworts durch einen Administrator "
                "(DPAPI-Schlüssel verloren) oder wenn Datei und Schlüssel nicht zusammenpassen.\n\n"
                "Soll eine neue, leere Datenbank angelegt werden? Die alte Datei wird umbenannt "
                f"({self.cfg.resolved_db_path.name}.unreadable-<Zeit>), nicht gelöscht.")
        if ask and winutil.message_box(text, flags=MB_YESNO | MB_ICONERROR) != IDYES:
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        db = self.cfg.resolved_db_path
        doomed = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
        if not keep_key:
            doomed.append(key_path())
        for p in doomed:
            if p.exists():
                target = p.with_name(f"{p.name}.unreadable-{stamp}")
                try:
                    os.replace(p, target)
                except OSError:
                    log.exception("Konnte %s nicht umbenennen", p)
                    return False
        log.warning("Datenbank zurueckgesetzt (Suffix unreadable-%s, Schluessel %s)", stamp,
                    "behalten" if keep_key else "ebenfalls beiseite gelegt")
        self._notify_later("Die Datenbank war nicht lesbar und wurde beiseite gelegt "
                           f"({db.name}.unreadable-{stamp}). Die Aufnahme laeuft mit einer neuen Datei weiter.")
        try:
            self.key = load_or_create_key(key_path())
            self.storage = Storage(db, self.key)
            return True
        except Exception:
            log.exception("Neuanlage der Datenbank fehlgeschlagen")
            return False

    def _notify_later(self, message: str) -> None:
        """Meldung ueber das Tray - ohne den Start aufzuhalten, falls das Symbol noch nicht steht."""
        def _run() -> None:
            try:
                if self.tray is not None:
                    self.tray.ready.wait(20)
                    self.tray.notify(message)
            except Exception:
                log.debug("Hinweis konnte nicht angezeigt werden", exc_info=True)

        threading.Thread(target=_run, name="notify", daemon=True).start()

    def start_workers(self) -> None:
        assert self.storage is not None
        cfg, storage = self.cfg, self.storage
        self.engine = TesseractEngine(lang=cfg.ocr_language, psm=cfg.ocr_psm, min_confidence=cfg.ocr_min_confidence)
        if self.engine.available():
            missing = self.engine.missing_languages()
            if missing:
                log.warning("Tesseract-Sprachdaten fehlen: %s", ", ".join(missing))
            log.info("OCR: %s (%s)", self.engine.version() or "tesseract", cfg.ocr_language)
            self.ocr_worker = OcrWorker(self.engine, storage, self.stop_event, maxsize=cfg.ocr_queue_max)
            self.ocr_worker.start()
        else:
            log.error("tesseract.exe nicht gefunden - Aufnahme laeuft ohne Texterkennung")
        self.capture = CaptureLoop(cfg, storage, self.ocr_worker, self.stop_event, on_state=self._on_capture_state,
                                   start_delay=20.0 if self.autostart else 2.0)
        self.capture.start()
        self.maintenance = MaintenanceJob(cfg, storage, self.stop_event,
                                          on_report=self._on_cleanup_report)
        self.maintenance.start()
        # Laeuft immer, auch ohne Plugin: Plugins lassen sich zur Laufzeit hinzufuegen.
        self.event_sync = EventSyncThread(cfg, storage, self.stop_event, initial_delay_s=25.0 if self.autostart else 8.0)
        self.event_sync.start()
        self.plugin_observer = PluginObserverThread(cfg, storage, self.stop_event, paused=self.is_paused)
        self.plugin_observer.start()
        aktiv = [p.name for p in plugins.installed(cfg)]
        log.info("Ereignis-Plugins: %s", ", ".join(aktiv) if aktiv else "keine installiert")
        if edition.UPDATE_CHANNEL:
            self._start_updater()

    def _start_updater(self) -> None:
        from . import updater

        result = updater.consume_pending(self.data_dir)
        if result is not None:
            outcome, version, _show = result   # ob das Fenster wiederkommt, hat service_main schon beim Start entschieden
            if outcome == "ok":
                log.info("Update auf %s abgeschlossen", version)
                message = f"Zeitspur wurde auf {version} aktualisiert."
            else:
                log.error("Update auf %s fehlgeschlagen - Protokoll: %s", version, self.data_dir / "updates")
                message = (f"Das Update auf {version} ist fehlgeschlagen. Zeitspur läuft mit {__version__} "
                           "weiter; Details stehen im Protokoll.")
            # Das Tray-Symbol braucht nach dem Start einen Moment, sonst geht die Meldung verloren
            timer = threading.Timer(10.0, self._notify_tray, args=(message,))
            timer.daemon = True
            timer.start()
        self.updater = updater.Updater(self.cfg, self.data_dir, self.stop_event, notify=self._notify_tray,
                                       window_visible=lambda: self._visible)
        self.updater.start()

    def _notify_tray(self, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(message)

    def update_status(self) -> dict | None:
        return self.updater.snapshot() if self.updater is not None else None

    def check_updates(self, notify: bool = True) -> bool:
        """Sofort nach Updates suchen. Aus dem Tray kommt das Ergebnis als Meldung; die Einstellungsseite zeigt
        es selbst an (notify=False)."""
        if self.updater is None:
            return False
        self.updater.check_now(manual=notify)
        return True

    def update_now(self) -> bool:
        """Verfuegbares Update sofort laden und installieren (Knopf im Zeitstrahl)."""
        if self.updater is None:
            return False
        self.updater.install_now()
        return True

    def complete_first_run(self, cfg: Config) -> bool:
        """Von der Bridge nach dem Ersteinrichtungs-Formular aufgerufen."""
        if not self.first_run:
            log.warning("complete_first_run erneut aufgerufen, obwohl bereits eingerichtet - ignoriert")
            return True
        save_config(cfg)
        self._apply_in_place(cfg)
        if not self.open_storage():
            return False
        self.first_run = False
        self.start_workers()
        self.ready.set()
        log.info("Ersteinrichtung abgeschlossen")
        return True

    # ------------------------------------------------------------------ IPC / Beenden
    def _ipc_loop(self) -> None:
        handles = [winutil.create_event(winutil.EVENT_SHOW), winutil.create_event(winutil.EVENT_QUIT)]
        try:
            while not self.stop_event.is_set():
                idx = winutil.wait_for_events(handles, 500)
                if idx == 0:
                    log.info("Anzeige-Signal einer zweiten Instanz empfangen")
                    self.show_window()
                elif idx == 1:
                    log.info("Beenden-Signal empfangen")
                    self.request_quit()
        finally:
            for h in handles:
                winutil.close_handle(h)

    def request_quit(self) -> None:
        """Aus beliebigem Thread aufrufbar: beendet die GUI-Schleife; Aufraeumen erfolgt in shutdown()."""
        if self.quitting:
            return
        self.quitting = True
        self.stop_event.set()
        log.info("Beenden angefordert")
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                log.debug("window.destroy fehlgeschlagen", exc_info=True)

    def shutdown(self) -> None:
        """Im MainThread nach Rueckkehr von webview.start()."""
        self.quitting = True
        self.stop_event.set()
        # Wartende Threads sofort aufwecken, damit sie stop_event sehen (sonst bis zu Intervall-Laenge Haenger)
        for job in (self.maintenance, self.event_sync, self.updater):
            if job is not None:
                job.wake()
        if self.ocr_worker is not None:
            self.ocr_worker.stop()
        if self.storage is not None and self.maintenance is not None and self.maintenance.is_alive():
            self.storage.interrupt()
        # Kurz joinen. Der Normalfall (Threads warten) endet dank wake() sofort; steckt der Events-Thread in
        # einem HTTP-/COM-Aufruf, laeuft er ins Timeout, danach schuetzt StorageClosed vor use-after-close und
        # der Daemon-Thread endet beim Prozessende ohnehin.
        for worker in (self.capture, self.ocr_worker, self.maintenance, self.event_sync, self.plugin_observer):
            if worker is not None and worker.is_alive():
                worker.join(timeout=8.0)
                if worker.is_alive():
                    log.debug("Thread %s laeuft noch (Daemon, endet beim Prozessende)", worker.name)
        if self.updater is not None:
            self.updater.join(timeout=3.0)   # wartet allenfalls auf einen laufenden Download-Block
        if self.tray is not None:
            self.tray.stop()
        for t in self._threads:
            t.join(timeout=3.0)
        self.bridge._close()
        if self.storage is not None:
            try:
                self.storage.close()
            except Exception:
                log.debug("Storage close fehlgeschlagen", exc_info=True)
        log.info("Zeitspur beendet")

    # ------------------------------------------------------------------ Aktionen
    def is_paused(self) -> bool:
        return bool(self.capture and self.capture.paused)

    def set_paused(self, paused: bool) -> None:
        if self.capture is None:
            return
        self.capture.set_paused(paused)
        if not paused:
            self.capture.request_reset()
        log.info("Aufnahme %s", "pausiert" if paused else "fortgesetzt")

    def toggle_paused(self) -> None:
        self.set_paused(not self.is_paused())

    # ---- Phase 2: Integrationen ------------------------------------------
    def _ensure_event_sync(self) -> None:
        """Startet den Ereignis-Sync-Thread, falls noch keiner laeuft, und stoesst einen Lauf an.
        Der Lock verhindert, dass zwei parallele Aufrufe zwei Threads starten (verwaister Daemon)."""
        with self._worker_lock:
            if self.storage is None or self.stop_event.is_set():
                return
            if self.event_sync is not None and self.event_sync.is_alive():
                self.event_sync.trigger()
                return
            self.event_sync = EventSyncThread(self.cfg, self.storage, self.stop_event, initial_delay_s=1.0)
            self.event_sync.start()

    def request_event_sync(self, day) -> None:
        if self.event_sync is not None and self.event_sync.is_alive():
            self.event_sync.request_day(day)

    # ---- Ereignis-Plugins --------------------------------------------------
    def plugin_list(self) -> list[dict]:
        return [p.describe(self.cfg) for p in plugins.PLUGINS]

    def add_plugin(self, plugin_id: str) -> dict:
        plugin = plugins.get(plugin_id)
        if plugin.id not in self.cfg.installed_plugins:
            self.cfg.installed_plugins = [*self.cfg.installed_plugins, plugin.id]
            save_config(self.cfg)
            log.info("Plugin hinzugefuegt: %s", plugin.name)
        self._ensure_event_sync()
        return plugin.describe(self.cfg)

    def remove_plugin(self, plugin_id: str) -> dict:
        """Entfernt ein Plugin vollstaendig: Zugangsdaten, seine Ereignisse und eigene Caches.
        Erneutes Hinzufuegen holt die Ereignisse aus der Quelle zurueck (rollierendes Fenster)."""
        plugin = plugins.get(plugin_id)
        self.cfg.installed_plugins = [p for p in self.cfg.installed_plugins if p != plugin.id]
        save_config(self.cfg)
        plugin.clear_credentials()
        removed = 0
        if self.storage is not None:
            removed = self.storage.delete_events_of_source(plugin.id)
            plugin.on_remove(self.storage)
        log.info("Plugin entfernt: %s (%d Ereignisse geloescht)", plugin.name, removed)
        return {"removed_events": removed}

    def save_plugin_credentials(self, plugin_id: str, values: dict[str, str]) -> str:
        plugin = plugins.get(plugin_id)
        plugin.save_credentials(values)
        log.info("Zugangsdaten gespeichert: %s", plugin.name)  # bewusst ohne Werte
        message = plugin.test_connection(self.cfg)
        self._ensure_event_sync()
        return message

    def reveal_plugin_credentials(self, plugin_id: str) -> dict[str, str]:
        return plugins.get(plugin_id).reveal_credentials()

    def clear_plugin_credentials(self, plugin_id: str) -> bool:
        return plugins.get(plugin_id).clear_credentials()

    def test_plugin(self, plugin_id: str) -> str:
        return plugins.get(plugin_id).test_connection(self.cfg)

    def exclude_current_app(self) -> str | None:
        fg = winutil.foreground_window()
        if fg is None or not fg.process_name or self.capture is None:
            return None
        self.capture.add_exclusion(fg.process_name)
        try:
            save_config(self.cfg)
        except Exception:
            log.exception("Konfiguration konnte nicht gespeichert werden")
        log.info("Prozess zur Ausschlussliste hinzugefuegt")
        return fg.process_name

    def delete_recent(self, minutes: int) -> int:
        if self.storage is None:
            return 0
        now = timeutil.now_ms()
        if self.capture is not None:
            self.capture.flush_extends()
        deleted = self.storage.delete_between(now - minutes * 60_000, now + 1)
        if self.capture is not None:
            self.capture.request_reset()
        self.bridge._invalidate_cache()
        log.info("%d Eintraege der letzten %d Minuten geloescht", deleted, minutes)
        return deleted

    def delete_entry(self, entry_id: int) -> int:
        if self.storage is None:
            return 0
        deleted = self.storage.delete_entry(entry_id)
        if self.capture is not None:
            self.capture.request_reset()
        self.bridge._invalidate_cache(entry_id)
        return deleted

    def compact_database(self) -> str:
        if self.storage is None:
            return "Datenbank ist nicht geöffnet."
        import psutil

        size = self.storage.db_size_bytes()
        available = psutil.virtual_memory().available
        if size > 0.4 * available:
            return (f"Komprimieren übersprungen: Datenbank ({size / 1e9:.1f} GB) ist zu groß für den freien "
                    f"Arbeitsspeicher ({available / 1e9:.1f} GB). Freier Platz wird ohnehin täglich zurückgegeben.")
        started = time.monotonic()
        try:
            if self.capture is not None:
                self.capture.flush_extends()
            self.storage.vacuum_full()
            self.storage.checkpoint("TRUNCATE")
        except Exception as e:
            log.exception("VACUUM fehlgeschlagen")
            return f"Komprimieren fehlgeschlagen: {e}"
        after = self.storage.db_size_bytes()
        log.info("VACUUM: %.1f MB -> %.1f MB in %.1f s", size / 1e6, after / 1e6, time.monotonic() - started)
        return f"Datenbank komprimiert: {size / 1e6:.0f} MB → {after / 1e6:.0f} MB."

    def open_logs(self) -> None:
        try:
            os.startfile(str(logs_dir()))  # type: ignore[attr-defined]
        except OSError:
            log.debug("Log-Ordner konnte nicht geoeffnet werden", exc_info=True)

    def apply_config(self, new_cfg: Config) -> bool:
        """Uebernimmt gespeicherte Einstellungen zur Laufzeit. Rueckgabe: Neustart erforderlich?"""
        restart = any(getattr(self.cfg, f) != getattr(new_cfg, f) for f in RESTART_REQUIRED_FIELDS)
        self._apply_in_place(new_cfg)
        if self.capture is not None:
            self.capture.refresh_config()
        if self.maintenance is not None:
            self.maintenance.trigger()
        self._ensure_event_sync()  # Outlook/Teams evtl. gerade erst aktiviert
        if self.event_sync is not None and self.event_sync.is_alive():
            self.event_sync.request_day(date.today())  # sofort einmal nachziehen
        log.info("Konfiguration uebernommen%s", " (Neustart erforderlich)" if restart else "")
        return restart

    def _apply_in_place(self, new_cfg: Config) -> None:
        for f in fields(Config):
            setattr(self.cfg, f.name, getattr(new_cfg, f.name))

    # ------------------------------------------------------------------ Status
    def capture_state(self) -> tuple[CaptureState, str, str]:
        if self.capture is None:
            state = CaptureState.STARTING if not self.quitting else CaptureState.STOPPED
            return state, STATE_LABELS[state], ""
        return self.capture.state, STATE_LABELS.get(self.capture.state, self.capture.state.value), self.capture.state_detail

    def ocr_available(self) -> bool:
        return bool(self.engine and self.engine.available())

    def status_summary(self) -> dict:
        state, label, detail = self.capture_state()
        d: dict = {
            "ready": self.ready.is_set(), "state": state.value, "label": label, "detail": detail,
            "paused": self.is_paused(), "ocr_available": self.ocr_available(), "version": __version__,
            "entries_today": 0, "entries_total": 0, "db_size_mb": 0.0, "pending_ocr": 0,
            "oldest_ms": None, "newest_ms": None,
            "installed_plugins": list(self.cfg.installed_plugins),
            "events_last_sync_ms": self.event_sync.last_sync_ms if self.event_sync else None,
        }
        if self.storage is not None and self.ready.is_set():
            try:
                start, end = timeutil.day_bounds(date.today())
                st = self.storage.stats()
                d.update(entries_today=self.storage.count_between(start, end), entries_total=st["entries"],
                         db_size_mb=st["db_size_bytes"] / 1e6, pending_ocr=st["pending_ocr"],
                         oldest_ms=st["oldest_ms"], newest_ms=st["newest_ms"])
            except Exception:
                log.debug("Statistik fehlgeschlagen", exc_info=True)
        return d

    def _on_capture_state(self, state: CaptureState, detail: str) -> None:
        if self.tray is not None:
            self.tray.update_state(state, detail)
        if state in (CaptureState.NO_DISK, CaptureState.ERROR):
            now = time.monotonic()
            if now - self._last_notice.get(state.value, 0) > 1800:
                self._last_notice[state.value] = now
                if self.tray is not None:
                    self.tray.notify(f"{STATE_LABELS[state]}. {detail}".strip())

    def _on_cleanup_report(self, report: CleanupReport) -> None:
        self.bridge._invalidate_cache()
        if report.deleted:
            log.info("Wartung: %d Eintraege entfernt, %.1f MB freigegeben", report.deleted, report.bytes_freed / 1e6)
