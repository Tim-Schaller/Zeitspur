# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-Spec fuer Zeitspur.

Standard: EINE EXE (Zeitspur.exe) mit gemeinsamem _internal-Ordner. Sie enthaelt auch den
MCP-Server, der ueber `Zeitspur.exe --mcp` laeuft. Grund: Die separate, fensterlose
ZeitspurMCP.exe wurde auf dem Entwicklungsrechner vom Virenscanner als Fehlalarm
entfernt. Mit der Umgebungsvariable ZEITSPUR_BUILD_MCP_EXE=1 wird sie zusaetzlich gebaut.

Aufruf: .venv\\Scripts\\python -m PyInstaller --noconfirm --clean zeitspur.spec
Ergebnis: dist\\Zeitspur\\{Zeitspur.exe, [ZeitspurMCP.exe], _internal\\}
Das Tesseract-Bundle (installer\\tesseract-portable) kopiert build.ps1 anschliessend nach dist\\Zeitspur\\tesseract.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH)
ICON = str(ROOT / "assets" / "zeitspur.ico")
UI = ROOT / "zeitspur" / "timeline_ui"
BUILD_MCP_EXE = os.environ.get("ZEITSPUR_BUILD_MCP_EXE", "0") == "1"

# Release-Build (build.ps1 -Release): Funktionen, die noch nicht fertig sind, werden gar nicht erst eingepackt -
# derzeit die Standort-Historie (Dawarich) samt Kartenbibliothek. zeitspur/edition.py erkennt das zur Laufzeit.
RELEASE = os.environ.get("ZEITSPUR_EDITION", "") == "release"
RELEASE_EXCLUDES = ["zeitspur.dawarich"] if RELEASE else []

# Update-Kanal: Nur der Release-Build bringt release_channel.txt mit und aktualisiert sich selbst
# (zeitspur/edition.py, updater.py). Die Datei entsteht hier im Arbeitsordner - nie im Quelltext, sonst
# wuerden sich auch der eigene Build und der Quelltextbetrieb durch Releases ersetzen.
channel_datas = []
if RELEASE:
    channel_file = Path(workpath) / "release_channel.txt"
    channel_file.parent.mkdir(parents=True, exist_ok=True)
    channel_file.write_text("release", encoding="utf-8")
    channel_datas = [(str(channel_file), "zeitspur")]

ui_datas = [(str(UI / name), "zeitspur/timeline_ui") for name in ("index.html", "style.css", "app.js")]
# Leaflet wird mitgeliefert, damit die Karte kein CDN braucht (Kacheln kommen erst zur Laufzeit).
if not RELEASE:
    ui_datas += [(str(UI / "vendor" / n), "zeitspur/timeline_ui/vendor") for n in ("leaflet.js", "leaflet.css")]
runtime_datas = collect_data_files("pythonnet") + collect_data_files("clr_loader")
runtime_bins = collect_dynamic_libs("pythonnet") + collect_dynamic_libs("clr_loader")
mcp_datas = (collect_data_files("mcp") + collect_data_files("mcp_types") + collect_data_files("jsonschema")
             + collect_data_files("jsonschema_specifications") + collect_data_files("referencing"))
mcp_hidden = ["anyio._backends._asyncio", "pydantic_core", "mcp.server.mcpserver"] \
    + collect_submodules("mcp.server") + collect_submodules("mcp_types")

common_hidden = ["sqlcipher3.dbapi2", "PIL._webp", "win32timezone"]
# Plugins werden erst bei Bedarf importiert - fuer PyInstaller sichtbar machen. Der ICS-Kalender braucht die
# Zeitzonendaten (tzdata): Windows bringt keine IANA-Zeitzonen mit.
plugin_hidden = ["zeitspur." + m for m in ("credentials", "httpclient", "eventlog", "calls_local", "notifications",
                                           "browser_history", "ics_calendar", "outlook_mail", "git_commits",
                                           "github_activity", "pc_times", "wifi")] \
    + ["recurring_ical_events", "x_wr_timezone", "dateutil.rrule", "win32evtlog", "tzdata"] \
    + collect_submodules("icalendar")
plugin_datas = collect_data_files("tzdata") + collect_data_files("icalendar")
excludes_common = ["tkinter", "matplotlib", "numpy", "scipy", "pandas", "PyQt5", "PyQt6", "PySide2", "PySide6",
                   "gi", "cefpython3", "IPython", "jupyter", "notebook", "pytest", "PyInstaller"]

# ---------------------------------------------------------------- Dienst (inkl. MCP-Modus)
a_service = Analysis(
    [str(ROOT / "launchers" / "service_entry.py")],
    pathex=[str(ROOT)],
    binaries=runtime_bins,
    datas=ui_datas + runtime_datas + mcp_datas + channel_datas + plugin_datas,
    hiddenimports=common_hidden + mcp_hidden + plugin_hidden + ["pystray._win32", "clr", "clr_loader", "webview.platforms.winforms",
                                                "webview.platforms.edgechromium", "mss.windows", "zeitspur.mcp_server",
                                                # Phase 2: Outlook-COM (lazy importiert) fuer PyInstaller sichtbar machen
                                                "pythoncom", "pywintypes", "win32com", "win32com.client",
                                                "win32timezone", "zeitspur.outlook", "zeitspur.teams",
                                                "zeitspur.events_sync", "zeitspur.updater",
                                                "cryptography.hazmat.primitives.asymmetric.ed25519"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes_common + RELEASE_EXCLUDES,
    # noarchive: Module liegen als einzelne .pyc in _internal statt komprimiert in der EXE. Die EXE besteht
    # dann nur noch aus Bootloader + Startskript - das vermeidet den Virenscanner-Fehlalarm (siehe README).
    noarchive=True,
)
pyz_service = PYZ(a_service.pure)
exe_service = EXE(
    pyz_service, a_service.scripts, [],
    exclude_binaries=True,
    name="Zeitspur",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

collect_args = [exe_service, a_service.binaries, a_service.datas]

# ---------------------------------------------------------------- optional: separate MCP-EXE
if BUILD_MCP_EXE:
    a_mcp = Analysis(
        [str(ROOT / "launchers" / "mcp_entry.py")],
        pathex=[str(ROOT)],
        binaries=[],
        datas=mcp_datas,
        hiddenimports=common_hidden + mcp_hidden,
        hookspath=[],
        runtime_hooks=[],
        excludes=excludes_common + RELEASE_EXCLUDES + ["webview", "pystray", "mss", "clr", "clr_loader", "pythonnet"],
        noarchive=False,
    )
    pyz_mcp = PYZ(a_mcp.pure)
    exe_mcp = EXE(
        pyz_mcp, a_mcp.scripts, [],
        exclude_binaries=True,
        name="ZeitspurMCP",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=ICON,
    )
    collect_args += [exe_mcp, a_mcp.binaries, a_mcp.datas]

# ---------------------------------------------------------------- gemeinsamer Ordner
coll = COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    name="Zeitspur",
)
