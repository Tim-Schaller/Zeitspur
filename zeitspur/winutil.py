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


def visible_windows_per_monitor(monitors: list[dict]) -> dict[int, list[WindowInfo]]:
    """Alle sichtbaren, betitelten Fenster je Monitor (oberstes zuerst).

    Anders als top_window_per_monitor wird NICHT bei einem Fenster je Monitor gestoppt: ein Fenster, das auf
    mehreren Monitoren liegt, erscheint bei jedem. So laesst sich die Ausschlussliste gegen jedes sichtbare
    Fenster pruefen, nicht nur gegen das oberste/fokussierte.
    """
    result: dict[int, list[WindowInfo]] = {m["monitor_id"]: [] for m in monitors}
    if not IS_WINDOWS or not monitors:
        return result
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
        win32gui.EnumWindows(_cb, None)
    except pywintypes.error:
        pass
    for hwnd, rect in order:                         # keine Monitor-Vergabe: ein Fenster darf auf mehreren liegen
        info = None
        for mon in monitors:
            if _rect_overlap(rect, mon) > 0:
                if info is None:
                    info = _window_info(hwnd, rect=rect)
                result[mon["monitor_id"]].append(info)
    return result


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
