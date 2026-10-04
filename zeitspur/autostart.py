"""Automatischer Start mit Windows (Run-Schluessel des aktuellen Benutzers, keine Adminrechte).

Quelle der Wahrheit ist die Registry, nicht config.yaml: denselben Wert setzt optional der Installer,
und der Nutzer kann ihn im Task-Manager unter "Autostart" jederzeit abschalten. Eine Kopie in der
Konfiguration wuerde nur auseinanderlaufen.

Der Wert startet den Dienst mit --autostart (Fenster bleibt versteckt, Aufnahme startet verzoegert).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Zeitspur"


def available() -> bool:
    return sys.platform == "win32"


def target_command() -> str:
    """Kommandozeile, die beim Anmelden ausgefuehrt werden soll."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --autostart'
    # Entwicklung: pythonw (kein Konsolenfenster) mit dem Projektverzeichnis im Suchpfad.
    # Mit -c ist sys.argv[1:] == ["--autostart"], das Argument kommt also korrekt an.
    root = Path(__file__).resolve().parents[1]
    exe = Path(sys.executable)
    runner = exe.with_name("pythonw.exe")
    if not runner.exists():
        runner = exe
    # Projektpfad als vollstaendig maskiertes Python-Literal (repr) einsetzen, damit kein Pfadanteil
    # (z. B. mit Apostroph) als Code interpretiert werden kann; verbleibende " fuer die cmd-Doppelquotes escapen.
    root_literal = repr(str(root))
    code = f"import sys; sys.path.insert(0, {root_literal}); from zeitspur.service_main import run; run()"
    code_cmd = code.replace('"', '\\"')
    return f'"{runner}" -c "{code_cmd}" --autostart'


def current_command() -> str | None:
    """Aktuell hinterlegte Autostart-Kommandozeile (None = kein Autostart)."""
    if not available():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return None
    text = str(value).strip()
    return text or None


def is_enabled() -> bool:
    return current_command() is not None


def enable() -> bool:
    if not available():
        return False
    import winreg

    command = target_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as e:
        log.warning("Autostart konnte nicht aktiviert werden: %s", e)
        return False
    log.info("Autostart aktiviert: %s", command)
    return True


def disable() -> bool:
    if not available():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True  # war ohnehin nicht gesetzt
    except OSError as e:
        log.warning("Autostart konnte nicht deaktiviert werden: %s", e)
        return False
    log.info("Autostart deaktiviert")
    return True


def set_enabled(enabled: bool) -> bool:
    """Schaltet den Autostart und meldet, ob er danach tatsaechlich aktiv ist."""
    ok = enable() if enabled else disable()
    return is_enabled() if ok else is_enabled()


def refresh_if_stale() -> bool:
    """Zieht einen aktiven Autostart auf den aktuellen Programmpfad nach (nach Verschieben/Neuinstallation).

    Nur fuer die gepackte EXE sinnvoll - im Quelltextbetrieb wechselt der Interpreterpfad staendig.
    """
    if not (available() and getattr(sys, "frozen", False)):
        return False
    current = current_command()
    if current is None or current == target_command():
        return False
    log.info("Autostart zeigte auf %s - wird auf den aktuellen Pfad aktualisiert", current)
    return enable()
