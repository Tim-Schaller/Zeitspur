"""Windows-spezifische Hilfen: DPI, Sperrbildschirm, Leerlauf, Vordergrundfenster, Einzelinstanz, Prioritaeten."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

MUTEX_NAME = r"Local\Zeitspur.Service"
EVENT_SHOW = r"Local\Zeitspur.Show"
EVENT_QUIT = r"Local\Zeitspur.Quit"

if IS_WINDOWS:
    import psutil
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32gui
    import win32process
    import winerror

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.OpenInputDesktop.restype = wt.HANDLE
    _user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _user32.CloseDesktop.argtypes = [wt.HANDLE]
    _user32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    _kernel32.GetTickCount.restype = wt.DWORD
    _kernel32.CreateFileW.restype = wt.HANDLE
    _kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    _kernel32.GetFinalPathNameByHandleW.restype = wt.DWORD
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]


# --------------------------------------------------------------------------- DPI

def set_dpi_awareness() -> bool:
    """Per-Monitor-V2-DPI-Awareness. Muss als Erstes im Prozess laufen (vor WinForms/mss),
    sonst liefert GDI bei Skalierung > 100 % verkleinerte, unscharfe Screenshots."""
    if not IS_WINDOWS:
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (DPI_AWARENESS_CONTEXT)-4
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return True
    except Exception:
        pass
    try:
        return bool(_user32.SetProcessDPIAware())
    except Exception:
        return False


# --------------------------------------------------------------------------- Sitzung / Leerlauf

def is_session_locked() -> bool:
    """True, wenn der sichere Desktop aktiv ist (Sperrbildschirm, UAC, Anmeldung).
    OpenInputDesktop liefert dann NULL bzw. einen Desktop, der nicht 'Default' heisst."""
    if not IS_WINDOWS:
        return False
    DESKTOP_READOBJECTS = 0x0001
    handle = _user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wt.DWORD(0)
        UOI_NAME = 2
        if _user32.GetUserObjectInformationW(handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return buf.value.lower() != "default"
        return False
    finally:
        _user32.CloseDesktop(handle)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    """Sekunden seit der letzten Maus-/Tastatureingabe (GetLastInputInfo)."""
    if not IS_WINDOWS:
        return 0.0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = _kernel32.GetTickCount()
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def on_battery() -> bool:
    """True, wenn der PC im Akkubetrieb laeuft (Netzteil getrennt). Bei Unklarheit False."""
    if not IS_WINDOWS:
        return False
    status = _SYSTEM_POWER_STATUS()
    try:
        if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
    except Exception:
        return False
    return status.ACLineStatus == 0   # 1 = Netz, 255 = unbekannt


def free_disk_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(str(p)).free


# --------------------------------------------------------------------------- WebView2-Runtime

# Kennungen, die pywebview als WebView2 akzeptiert: die Runtime selbst und die Edge-Vorschaukanaele.
WEBVIEW2_CLIENTS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # Microsoft Edge WebView2 Runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
)
WEBVIEW2_MIN_VERSION = (86, 0, 622, 0)
# Microsofts Download-Seite fuer Endnutzer (aus der WebView2-Verteilungsdoku)
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/consumer/"


def _version_tuple(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(teil) for teil in str(text).split("."))
    except ValueError:
        return (0,)


def webview2_version() -> str | None:
    """Version der installierten WebView2-Runtime - so erkannt, wie pywebview es tut -, sonst None.

    Wichtig, weil pywebview ohne WebView2 nicht etwa abbricht, sondern still auf den Internet-Explorer-
    Renderer ausweicht. Darin laeuft der Zeitstrahl nicht; man saehe nur ein leeres oder kaputtes Fenster.
    Geprueft werden die Microsoft-Schluessel pro Benutzer und pro Rechner (siehe WebView2-Verteilungsdoku).
    """
    if not IS_WINDOWS:
        return None
    import winreg

    for guid in WEBVIEW2_CLIENTS:
        for hive, pfad in ((winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
                           (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}")):
            try:
                with winreg.OpenKey(hive, pfad) as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0])
            except OSError:
                continue
            if _version_tuple(version) >= WEBVIEW2_MIN_VERSION:
                return version
    return None


def https_context():
    """TLS-Kontext, der Zertifikate prueft wie Windows selbst (truststore -> Windows-Zertifikatspruefung).

    Python allein liest nur die Stammzertifikate, die schon im Zertifikatsspeicher liegen. Windows laedt fehlende
    aber erst bei Bedarf nach (automatische Aktualisierung der Stammzertifikate). Auf einem frischen PC - so in der
    Windows Sandbox am 03.10.2026 - scheiterte die Pruefung deshalb mit "unable to get local issuer certificate",
    obwohl Edge dieselbe Seite oeffnet. Geprueft wird weiterhin vollstaendig: Kette und Hostname, wie im Browser.
    """
    import ssl

    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:   # ohne truststore: Pythons eigene, ebenso strenge Pruefung (nur ohne Nachladen)
        return ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def apps_use_dark_theme() -> bool:
    """Windows-Einstellung "Standard-App-Modus: Dunkel". Danach richtet sich auch prefers-color-scheme in WebView2."""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False   # Wert fehlt: Windows nimmt hell an


def set_clipboard_text(text: str, attempts: int = 10) -> None:
    """Text in die Zwischenablage. Haelt gerade ein anderes Programm sie offen, kurz warten und erneut versuchen."""
    import time

    import win32clipboard

    for attempt in range(attempts):
        try:
            win32clipboard.OpenClipboard()
        except pywintypes.error:
            if attempt == attempts - 1:
                raise RuntimeError("Die Zwischenablage ist gerade von einem anderen Programm belegt.") from None
            time.sleep(0.05)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return


# --------------------------------------------------------------------------- App-Container (MSIX)

_FILE_SHARE_ALL = 0x07
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # noetig, um ein Verzeichnis oeffnen zu duerfen
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def resolved_path(path: Path) -> str | None:
    """Tatsaechlicher Pfad hinter `path` (GetFinalPathNameByHandleW), None wenn nicht ermittelbar.

    Loest Verknuepfungen, Junctions und die Dateiumleitung von App-Containern auf.
    """
    if not IS_WINDOWS:
        return None
    handle = _kernel32.CreateFileW(str(path), 0, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                                   _FILE_FLAG_BACKUP_SEMANTICS, None)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        length = _kernel32.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)  # VOLUME_NAME_DOS
        if not length or length >= len(buf):
            return None
        real = buf.value
        return real[4:] if real.startswith("\\\\?\\") and not real.startswith("\\\\?\\UNC") else real
    finally:
        _kernel32.CloseHandle(handle)


def _is_container_cache(real: str, nominal: Path) -> bool:
    low = real.lower()
    if low == str(Path(nominal).absolute()).lower():
        return False
    # Nur die eindeutige Signatur der MSIX-Umleitung zaehlt; eine Junction auf ein anderes
    # Laufwerk ist eine legitime Abweichung und darf den Dienst nicht blockieren.
    return "\\packages\\" in low and "\\localcache\\" in low


def container_shadow(path: Path, *, probe: bool = False) -> str | None:
    """Gibt den echten Pfad zurueck, wenn Zugriffe auf `path` in einen App-Container umgeleitet werden.

    Laeuft der Dienst innerhalb einer MSIX-/Store-Sandbox (etwa weil er aus einer verpackten
    Anwendung heraus gestartet wurde), leitet Windows Schreibzugriffe auf %LOCALAPPDATA%
    unbemerkt nach ...\\Packages\\<Paket>\\LocalCache\\Local\\... um. Der Dienst wuerde dann in
    eine Kopie aufnehmen, waehrend die echte Installation unveraendert bleibt: zwei Datenbanken,
    die auseinanderdriften, und Zugangsdaten, die in der Kopie landen. Das ist am 18.09.2026
    genau so passiert, deshalb wird dieser Fall erkannt statt still hingenommen.

    Die Umleitung entsteht erst beim Schreiben (copy-on-write): Solange keine Kopie existiert,
    zeigt der Pfad noch auf das Original. `probe=True` legt daher kurz eine leere Datei an und
    sieht nach, wo sie tatsaechlich landet - so faellt schon der erste umgeleitete Lauf auf.
    """
    p = Path(path)
    if p.exists():
        real = resolved_path(p)
        if real and _is_container_cache(real, p):
            return real
    if not probe:
        return None
    folder = p if p.is_dir() else p.parent
    marker = folder / ".pfadprobe"
    try:
        marker.touch()
        real = resolved_path(marker)
    except OSError:
        return None
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    if real and _is_container_cache(real, marker):
        return str(Path(real).parent)
    return None


# --------------------------------------------------------------------------- Vordergrundfenster

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str
    exe_path: str | None
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


def _process_info(pid: int) -> tuple[str, str | None]:
    if pid <= 0:
        return "", None
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        try:
            exe = proc.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            exe = None
        return name, exe
    except psutil.Error:
        return "", None


def _core_window_pid(hwnd: int) -> int:
    """UWP-Apps laufen hinter ApplicationFrameHost.exe; die eigentliche App haengt als CoreWindow darunter."""
    found: list[int] = []

    def _cb(child: int, _arg) -> bool:
        try:
            if win32gui.GetClassName(child) == "Windows.UI.Core.CoreWindow":
                found.append(win32process.GetWindowThreadProcessId(child)[1])
                return False
        except pywintypes.error:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except pywintypes.error:
        pass  # Abbruch der Enumeration meldet pywin32 als Fehler
    return found[0] if found else 0


def _window_info(hwnd: int, title: str | None = None, rect: tuple | None = None) -> WindowInfo:
    """Baut eine WindowInfo zu einem Fenster (Titel/Prozess/Pfad/Rechteck, inkl. UWP-Aufloesung)."""
    if title is None:
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except pywintypes.error:
            title = ""
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    except pywintypes.error:
        pid = 0
    name, exe = _process_info(pid)
    if name.lower() == "applicationframehost.exe":
        child_pid = _core_window_pid(hwnd)
        if child_pid:
            pid = child_pid
            name, exe = _process_info(child_pid)
    if rect is None:
        try:
            rect = tuple(win32gui.GetWindowRect(hwnd))
        except pywintypes.error:
            rect = (0, 0, 0, 0)
    return WindowInfo(hwnd=hwnd, title=title, pid=pid, process_name=name, exe_path=exe, rect=rect)  # type: ignore[arg-type]


def foreground_window() -> WindowInfo | None:
    if not IS_WINDOWS:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
    except pywintypes.error:
        return None
    if not hwnd:
        return None
    return _window_info(hwnd)


DWMWA_CLOAKED = 14


def _is_cloaked(hwnd: int) -> bool:
    """True fuer per DWM ausgeblendete Fenster (z. B. UWP-Apps auf einem anderen virtuellen Desktop)."""
    try:
        val = ctypes.c_int(0)
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)) == 0:
            return val.value != 0
    except Exception:
        pass
    return False


def _rect_overlap(rect: tuple, monitor: dict) -> int:
    left = max(rect[0], monitor["left"])
    top = max(rect[1], monitor["top"])
    right = min(rect[2], monitor["left"] + monitor["width"])
    bottom = min(rect[3], monitor["top"] + monitor["height"])
    return max(0, right - left) * max(0, bottom - top)


def assign_top_windows(order, monitors, resolve):
    """Ordnet die (in Z-Reihenfolge, oberstes zuerst) uebergebenen Fenster je Monitor zu.

    Jedes Fenster beansprucht den Monitor mit der groessten Ueberlappung; sobald ein Monitor ein
    Fenster hat, ist er vergeben. So bekommt jeder Monitor sein oberstes tatsaechlich sichtbares Fenster.
    `resolve(hwnd, rect)` liefert die WindowInfo (spaet aufgerufen, damit nur Treffer aufgeloest werden).
    """
    result: dict[int, WindowInfo] = {}
    remaining = {m["monitor_id"] for m in monitors}
    for hwnd, rect in order:
        if not remaining:
            break
        best_id, best_area = None, 0
        for mon in monitors:
            if mon["monitor_id"] not in remaining:
                continue
            area = _rect_overlap(rect, mon)
            if area > best_area:
                best_area, best_id = area, mon["monitor_id"]
        if best_id is not None and best_area > 0:
            result[best_id] = resolve(hwnd, rect)
            remaining.discard(best_id)
    return result


def top_window_per_monitor(monitors: list[dict]) -> dict[int, WindowInfo]:
    """Je Monitor (dict mit monitor_id/left/top/width/height) das oberste sichtbare, betitelte Fenster."""
    if not IS_WINDOWS or not monitors:
        return {}
    order: list[tuple[int, tuple]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd) or _is_cloaked(hwnd):
                return True
            rect = tuple(win32gui.GetWindowRect(hwnd))
        except pywintypes.error:
            return True
        if rect[2] - rect[0] > 0 and rect[3] - rect[1] > 0:
            order.append((hwnd, rect))
        return True

    try:
        win32gui.EnumWindows(_cb, None)  # liefert Fenster in Z-Reihenfolge (oberstes zuerst)
    except pywintypes.error:
        pass
    return assign_top_windows(order, monitors, lambda hwnd, rect: _window_info(hwnd, rect=rect))


GA_ROOTOWNER = 3


def subtract_rects(rect: tuple, holes) -> list[tuple[int, int, int, int]]:
    """Rechteck minus Loecher als Liste sich nicht ueberlappender Rechtecke (links, oben, rechts, unten)."""
    pieces = [tuple(rect)]
    for hl, ht, hr, hb in holes:
        rest = []
        for l, t, r, b in pieces:
            if hl >= r or hr <= l or ht >= b or hb <= t:     # Loch trifft dieses Stueck nicht
                rest.append((l, t, r, b))
                continue
            if t < ht:
                rest.append((l, t, r, ht))                    # Streifen oberhalb
            if hb < b:
                rest.append((l, hb, r, b))                    # Streifen unterhalb
            top, bottom = max(t, ht), min(b, hb)
            if l < hl:
                rest.append((l, top, hl, bottom))             # links daneben
            if hr < r:
                rest.append((hr, top, r, bottom))             # rechts daneben
        pieces = rest
        if not pieces:
            break
    return pieces


def select_excluded_rects(windows, is_excluded, resolve, cover=None) -> list[tuple[int, int, int, int]]:
    """Rechtecke, die geschwaerzt werden muessen.

    `windows`: (hwnd, rect, title, root_owner) je sichtbarem Fenster, oberstes zuerst (Z-Reihenfolge).
    Geschwaerzt wird ein Fenster, das selbst ausgeschlossen ist, und jedes Fenster, dessen Besitzer ausgeschlossen
    ist - Menues, Vorschlagslisten der Adresszeile und Dialoge wie "Speichern unter" sind eigene Fenster, oft ohne
    Titel, gehoeren aber zum privaten Fenster. `resolve(hwnd, title, rect)` liefert die WindowInfo (title/rect
    None = selbst ermitteln).

    Liegt ein ausgeschlossenes Fenster hinter anderen, wird nur geschwaerzt, was davon hervorschaut:
    `cover(hwnd, title)` liefert die Flaeche, die ein Fenster DARUEBER sicher undurchsichtig verdeckt, oder None
    (dann verdeckt es vorsichtshalber nichts). Ohne `cover` wird das ganze Rechteck geschwaerzt.
    """
    verdict: dict[int, bool] = {}
    covers: dict[int, tuple | None] = {}

    def excluded(hwnd: int, title: str | None, rect: tuple | None) -> bool:
        if hwnd not in verdict:
            verdict[hwnd] = bool(is_excluded(resolve(hwnd, title, rect)))
        return verdict[hwnd]

    def covered_by(hwnd: int, title: str) -> tuple | None:
        if hwnd not in covers:
            covers[hwnd] = cover(hwnd, title) if cover else None
        return covers[hwnd]

    result: list[tuple[int, int, int, int]] = []
    for i, (hwnd, rect, title, root) in enumerate(windows):
        if excluded(hwnd, title, rect) or (root and root != hwnd and excluded(root, None, None)):
            holes = [c for h, _r, t, _o in windows[:i] if (c := covered_by(h, t)) is not None]
            result.extend(subtract_rects(rect, holes))
    return result


WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2           # Mica: undurchsichtig, zeigt nur das Hintergrundbild, nie Fenster dahinter
DWMSBT_TRANSIENTWINDOW = 3      # Acryl: zeigt verschwommen, was dahinter liegt
DWMSBT_TABBEDWINDOW = 4         # Mica Alt: undurchsichtig wie Mica
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2
COVER_MARGIN_96 = 10            # abgerundete Ecke (8 px) plus Rand, bei 96 dpi; waechst mit der Skalierung


def _cover_rect(hwnd: int, title: str) -> tuple[int, int, int, int] | None:
    """Flaeche, die dieses Fenster sicher undurchsichtig verdeckt - oder None, wenn das nicht sicher ist.

    Vorsichtig: Nur betitelte, normale Fenster zaehlen. Durchklickbare Fenster, Werkzeugfenster/Flyouts, Acryl-
    Hintergrund und Fenster mit eigener Form (SetWindowRgn) verdecken nichts. Fenster mit WS_EX_LAYERED koennen
    Pixel durchsichtig machen und zaehlen nur mit Mica-Hintergrund (der ist undurchsichtig - so zeichnen z. B.
    Claude und 1Password) und ohne eingestellte Gesamt-Transparenz.
    Genommen wird der sichtbare Rahmen (ohne die unsichtbaren Raender von GetWindowRect), nach innen verkleinert um
    die abgerundeten Ecken - am Rand des Fensters davor bleibt so ein schmaler Streifen schwarz.
    Nicht erkennbar ist ein Fenster, das sich selbst durchsichtig zeichnet (z. B. ein Terminal mit Deckkraft < 100 %).
    """
    if not title:
        return None
    try:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex_style & (WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW):
            return None
        dwm = ctypes.windll.dwmapi
        backdrop = ctypes.c_int(0)
        if dwm.DwmGetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop),
                                     ctypes.sizeof(backdrop)) != 0:
            backdrop.value = 0
        if backdrop.value == DWMSBT_TRANSIENTWINDOW:
            return None
        if ex_style & WS_EX_LAYERED:
            if backdrop.value not in (DWMSBT_MAINWINDOW, DWMSBT_TABBEDWINDOW):
                return None
            key, alpha, flags = wt.DWORD(0), ctypes.c_ubyte(255), wt.DWORD(0)
            if _user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)):
                if flags.value & LWA_COLORKEY or (flags.value & LWA_ALPHA and alpha.value < 255):
                    return None
        gdi = ctypes.windll.gdi32
        rgn = gdi.CreateRectRgn(0, 0, 0, 0)
        try:
            if _user32.GetWindowRgn(hwnd, rgn) != 0:       # 0 = ERROR = keine eigene Fensterform
                return None
        finally:
            gdi.DeleteObject(rgn)
        frame = wt.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(frame),
                                     ctypes.sizeof(frame)) != 0:
            return None
        dpi = _user32.GetDpiForWindow(hwnd) or 96
    except (pywintypes.error, OSError, AttributeError):
        return None
    m = -(-COVER_MARGIN_96 * dpi // 96)                    # aufrunden
    rect = (frame.left + m, frame.top + m, frame.right - m, frame.bottom - m)
    return rect if rect[2] > rect[0] and rect[3] > rect[1] else None


def excluded_window_rects(is_excluded) -> list[tuple[int, int, int, int]]:
    """Bildschirmrechtecke, die geschwaerzt werden muessen: alle sichtbaren Fenster, die `is_excluded(WindowInfo)`
    erfuellen, samt ihrer Popups - ohne die Teile, die Fenster davor sicher verdecken (siehe _cover_rect).

    Ein verdecktes Fenster zaehlt also mit, soweit es hervorschaut; minimierte und per DWM ausgeblendete Fenster
    (andere virtuelle Desktops) sind nicht zu sehen und fehlen. GetWindowRect schliesst den unsichtbaren Rahmen
    ein - am eigenen Rand wird also eher etwas zu viel geschwaerzt als zu wenig.
    """
    if not IS_WINDOWS:
        return []
    windows: list[tuple[int, tuple, str, int]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd) or _is_cloaked(hwnd):
                return True
            rect = tuple(win32gui.GetWindowRect(hwnd))
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            root = win32gui.GetAncestor(hwnd, GA_ROOTOWNER) or 0
        except pywintypes.error:
            return True
        windows.append((hwnd, rect, title, root))
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except pywintypes.error:
        pass
    return select_excluded_rects(windows, is_excluded,
                                 lambda hwnd, title, rect: _window_info(hwnd, title=title, rect=rect),
                                 cover=_cover_rect)


def monitor_layout() -> tuple[tuple[int, int, int, int], ...]:
    """Lage aller Monitore (links, oben, rechts, unten) in der Reihenfolge, in der Windows sie meldet - wie mss.

    Aendert sich beim An- und Abstecken (Dockingstation), bei Aufloesung, Skalierung oder Anordnung. Kostet nur
    einen Systemaufruf und laesst sich deshalb vor jeder Aufnahme pruefen."""
    if not IS_WINDOWS:
        return ()
    try:
        return tuple(tuple(int(v) for v in rect) for _monitor, _dc, rect in win32api.EnumDisplayMonitors(None, None))
    except pywintypes.error:
        return ()


def point_in_monitor(point: tuple[int, int], monitor: dict) -> bool:
    x, y = point
    return (monitor["left"] <= x < monitor["left"] + monitor["width"]
            and monitor["top"] <= y < monitor["top"] + monitor["height"])


# --------------------------------------------------------------------------- Prioritaeten

def lower_process_priority() -> None:
    if not IS_WINDOWS:
        return
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        log.debug("Prozesspriorität konnte nicht gesenkt werden", exc_info=True)


THREAD_MODE_BACKGROUND_BEGIN = 0x00010000


def set_thread_low_priority() -> None:
    """Senkt nur die CPU-Prioritaet des aktuellen Threads - ohne Hintergrundmodus.

    Fuer rechenlastige Arbeit (OCR) ist das die richtige Stufe. THREAD_MODE_BACKGROUND_BEGIN
    drosselt zusaetzlich Speicher und E/A und parkt den Thread auf Hybrid-Prozessoren dauerhaft
    auf den Sparkernen. Gemessen am 21.09.2026 auf einem Core Ultra 7 258V (4 Leistungs-,
    4 Sparkerne): dieselbe Seite braucht im Hintergrundmodus 11,5 s statt 4,1 s. Unter Last lief
    sie damit in den OCR-Timeout, wurde verworfen und erneut versucht - ein Kern war dauerhaft
    belegt, ohne dass ein Ergebnis entstand.
    """
    if not IS_WINDOWS:
        return
    try:
        win32process.SetThreadPriority(win32api.GetCurrentThread(), win32process.THREAD_PRIORITY_LOWEST)
    except Exception:
        log.debug("Threadpriorität konnte nicht gesenkt werden", exc_info=True)


def set_thread_background_mode() -> None:
    """Senkt CPU-, I/O- und Speicherprioritaet des aktuellen Threads (fuer die Wartung).

    Nur fuer E/A-lastige Arbeit geeignet, die beliebig lange dauern darf - fuer Rechenarbeit
    siehe set_thread_low_priority().
    """
    if not IS_WINDOWS:
        return
    try:
        handle = win32api.GetCurrentThread()
        if not win32process.SetThreadPriority(handle, THREAD_MODE_BACKGROUND_BEGIN):
            win32process.SetThreadPriority(handle, win32process.THREAD_PRIORITY_LOWEST)
    except Exception:
        try:
            win32process.SetThreadPriority(win32api.GetCurrentThread(), win32process.THREAD_PRIORITY_LOWEST)
        except Exception:
            log.debug("Threadpriorität konnte nicht gesenkt werden", exc_info=True)


# --------------------------------------------------------------------------- Einzelinstanz / IPC

class SingleInstance:
    """Benannter Mutex; die zweite Instanz erkennt ERROR_ALREADY_EXISTS."""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        if not IS_WINDOWS:
            return True
        handle = win32event.CreateMutex(None, False, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                win32api.CloseHandle(self._handle)
            finally:
                self._handle = None


def create_event(name: str):
    """Auto-Reset-Event (erzeugt oder oeffnet ein vorhandenes)."""
    return win32event.CreateEvent(None, False, False, name)


def signal_event(name: str) -> bool:
    """Setzt ein vorhandenes benanntes Event. False, wenn es (noch) nicht existiert."""
    if not IS_WINDOWS:
        return False
    try:
        handle = win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, name)
    except pywintypes.error:
        return False
    try:
        win32event.SetEvent(handle)
        return True
    finally:
        win32api.CloseHandle(handle)


def wait_for_events(handles: list, timeout_ms: int) -> int | None:
    """Wartet auf eines der Events; liefert dessen Index oder None bei Timeout."""
    result = win32event.WaitForMultipleObjects(handles, False, timeout_ms)
    if win32event.WAIT_OBJECT_0 <= result < win32event.WAIT_OBJECT_0 + len(handles):
        return result - win32event.WAIT_OBJECT_0
    return None


def close_handle(handle) -> None:
    try:
        win32api.CloseHandle(handle)
    except Exception:
        pass


def message_box(text: str, title: str = "Zeitspur", flags: int = 0x40) -> int:
    """Einfache MessageBox (MB_ICONINFORMATION=0x40, MB_ICONERROR=0x10, MB_YESNO=0x4)."""
    if not IS_WINDOWS:
        return 0
    return _user32.MessageBoxW(None, text, title, flags | 0x00040000)  # MB_TOPMOST


def ensure_window_on_screen(title: str) -> bool:
    """Holt ein Fenster zurueck, das ausserhalb aller Bildschirme liegt (z. B. nach Monitorwechsel).

    Sonst laesst es sich weder sehen noch greifen - und WebView2 haelt die Seite fuer unsichtbar und
    drosselt ihre Timer, sodass der Zeitstrahl nicht mehr nachlaedt. Rueckgabe: wurde verschoben?
    """
    try:
        import win32api
        import win32con
        import win32gui
    except Exception:  # pragma: no cover - ohne pywin32 nicht moeglich
        return False

    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.GetWindowText(hwnd) == title:
            found.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return False
    if not found:
        return False
    hwnd = found[0]
    try:
        # Versteckte und minimierte Fenster parkt Windows selbst weit ausserhalb (z. B. -32000);
        # das ist keine falsche Position und darf nicht "korrigiert" werden.
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = max(200, right - left), max(200, bottom - top)
        # Liegt ein nennenswerter Teil auf irgendeinem Monitor? Dann ist alles in Ordnung.
        for mon in win32api.EnumDisplayMonitors():
            mx1, my1, mx2, my2 = win32api.GetMonitorInfo(mon[0])["Work"]
            if min(right, mx2) - max(left, mx1) > 80 and min(bottom, my2) - max(top, my1) > 40:
                return False
        work = win32api.GetMonitorInfo(
            win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))["Work"]
        x = work[0] + max(0, ((work[2] - work[0]) - width) // 2)
        y = work[1] + max(0, ((work[3] - work[1]) - height) // 2)
        win32gui.SetWindowPos(hwnd, 0, x, y, width, height,
                              win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        log.info("Fenster lag ausserhalb der Bildschirme und wurde zurueckgeholt (%d,%d)", x, y)
        return True
    except Exception:
        log.debug("Fensterposition konnte nicht geprueft werden", exc_info=True)
        return False
