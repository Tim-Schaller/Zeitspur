import threading

import pytest
from PIL import Image, ImageDraw

from zeitspur import timeutil, winutil
from zeitspur.capture import (BLACKED_DETAIL, EXCLUDED_TITLE, CaptureLoop, CaptureState, FrameDiff, downscale_for_diff,
                              encode_png_gray, encode_webp, frame_difference)
from zeitspur.config import Config

SIZE = (1920, 1200)
MON = {"left": 0, "top": 0, "width": 1920, "height": 1200}


def screen(color=(245, 245, 245)) -> Image.Image:
    return Image.new("RGB", SIZE, color)


def with_text_lines(img: Image.Image, lines: int, y0: int = 100) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    for i in range(lines):
        y = y0 + i * 28
        d.rectangle([40, y, 640, y + 18], fill=(20, 20, 20))  # simuliert eine Textzeile (600x18 px)
    return out


def striped(offset: int = 0) -> Image.Image:
    img = screen()
    d = ImageDraw.Draw(img)
    for y in range(0, 1200, 40):
        d.rectangle([0, (y + offset) % 1200, 1920, (y + offset) % 1200 + 12], fill=(60, 60, 60))
    return img


def test_identical_frames_are_unchanged():
    fd = FrameDiff(0.01)
    a = fd.prepare(screen())
    assert fd.ratio(1, a) == 1.0  # ohne Referenz gilt alles als neu
    fd.commit(1, a)
    changed, ratio = fd.is_changed(1, fd.prepare(screen()))
    assert not changed and ratio == 0.0


def test_cursor_blink_is_noise():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(screen()))
    blink = screen()
    ImageDraw.Draw(blink).rectangle([300, 300, 312, 324], fill=(0, 0, 0))
    changed, ratio = fd.is_changed(1, fd.prepare(blink))
    assert not changed and ratio < 0.002


def test_typing_accumulates_against_stored_frame():
    fd = FrameDiff(0.01)
    base = screen()
    fd.commit(1, fd.prepare(base))
    one_line = with_text_lines(base, 1)
    changed1, r1 = fd.is_changed(1, fd.prepare(one_line))
    assert not changed1 and 0.002 < r1 < 0.01  # eine Zeile allein liegt unter der Schwelle
    three_lines = with_text_lines(base, 3)
    changed3, r3 = fd.is_changed(1, fd.prepare(three_lines))
    assert changed3 and r3 > r1  # Referenz bleibt der gespeicherte Frame -> Aenderungen summieren sich


def test_scrolling_is_a_change():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(striped(0)))
    changed, ratio = fd.is_changed(1, fd.prepare(striped(20)))
    assert changed and ratio > 0.3


def test_resolution_change_and_reset():
    fd = FrameDiff(0.01)
    fd.commit(1, fd.prepare(screen()))
    other = Image.new("RGB", (1280, 800), (245, 245, 245))
    assert fd.ratio(1, fd.prepare(other)) == 0.0  # gleiche Downscale-Breite, gleicher Inhalt
    assert frame_difference(Image.new("L", (10, 10)), Image.new("L", (12, 10))) == 1.0
    fd.reset(1)
    assert fd.ratio(1, fd.prepare(screen())) == 1.0


def test_downscale_geometry():
    small = downscale_for_diff(screen(), 160)
    assert small.size == (160, 100) and small.mode == "L"


def test_encoders_stay_in_memory(tmp_path):
    big = Image.new("RGB", (3840, 2400), (10, 200, 10))
    webp, w, h = encode_webp(big, 1920, 75)
    assert (w, h) == (1920, 1200)
    assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP"
    png = encode_png_gray(screen())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []  # keine Bilddatei geschrieben


class FakeOcr:
    def __init__(self, accept: bool = True):
        self.jobs: list[tuple[int, bytes]] = []
        self.accept = accept

    def submit(self, entry_id, image_bytes):
        self.jobs.append((entry_id, image_bytes))
        return self.accept


def make_loop(storage, monkeypatch, cfg: Config | None = None):
    cfg = cfg or Config(capture_interval_seconds=5, change_threshold=0.01)
    stop = threading.Event()
    states: list[CaptureState] = []
    ocr = FakeOcr()
    loop = CaptureLoop(cfg, storage, ocr, stop, on_state=lambda s, d: states.append(s), start_delay=0)
    frames = {"img": screen()}
    fg = {"info": winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", r"C:\Office\WINWORD.EXE", (100, 100, 900, 700))}
    # Kopie: die Aufnahme schwaerzt das Bild an Ort und Stelle, wie bei einem echten, jedes Mal neuen Bildschirmfoto
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, MON, frames["img"].copy())])
    monkeypatch.setattr(loop, "current_foreground", lambda: fg["info"])
    monkeypatch.setattr(winutil, "excluded_window_rects", lambda is_ex: [])   # nie die echten Fenster des Testrechners
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    return loop, frames, fg, clock, states, ocr


def entries(storage):
    return storage.entries_between(0, 10 ** 15)


def test_capture_loop_stores_extends_and_splits(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.tick()
    rows = entries(storage)
    assert len(rows) == 1 and rows[0]["process_name"] == "WINWORD.EXE" and rows[0]["window_title"] == "Dokument - Word"
    assert rows[0]["width"] == 1920 and loop.state == CaptureState.RECORDING
    assert len(ocr.jobs) == 1 and ocr.jobs[0][1][:8] == b"\x89PNG\r\n\x1a\n"
    assert storage.get_image(rows[0]["id"])[:4] == b"RIFF"
    # unveraenderter Frame -> kein neuer Eintrag, ts_end wird (gepuffert) verlaengert
    clock["ms"] += 5000
    loop.tick()
    assert len(entries(storage)) == 1
    loop.flush_extends()
    assert storage.get_entry(rows[0]["id"])["ts_end"] == clock["ms"]
    # Inhalt aendert sich -> neuer Eintrag
    clock["ms"] += 5000
    frames["img"] = striped(0)
    loop.tick()
    assert len(entries(storage)) == 2
    # gleicher Inhalt, aber anderes Vordergrundfenster -> neuer Eintrag
    clock["ms"] += 5000
    fg["info"] = winutil.WindowInfo(2, "Posteingang - Outlook", 200, "OUTLOOK.EXE", None, (100, 100, 900, 700))
    loop.tick()
    rows = entries(storage)
    assert len(rows) == 3 and rows[-1]["process_name"] == "OUTLOOK.EXE"
    # Keyframe nach max_block_minutes
    clock["ms"] += loop.cfg.max_block_ms
    loop.tick()
    assert len(entries(storage)) == 4
    assert loop.entries_created == 4 and loop.frames_seen == 5


def stored_image(storage, entry_id) -> Image.Image:
    import io

    return Image.open(io.BytesIO(storage.get_image(entry_id))).convert("L")


def is_black(img: Image.Image, xy) -> bool:
    return img.getpixel(xy) < 20     # WebP ist verlustbehaftet: "schwarz" heisst nahe 0


def test_capture_loop_states(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.tick()
    assert loop.state == CaptureState.RECORDING
    # Deny-Liste (Prozess): KEINE Pause mehr - das Fenster wird geschwaerzt, der Rest weiter aufgenommen
    fg["info"] = winutil.WindowInfo(3, "Datenbank.kdbx - KeePass", 300, "KeePass.exe", None, (0, 0, 800, 600))
    clock["ms"] += 5000
    loop.tick()
    rows = entries(storage)
    assert loop.state == CaptureState.RECORDING and loop.state_detail == BLACKED_DETAIL and len(rows) == 2
    assert rows[-1]["process_name"] == "KeePass.exe" and rows[-1]["window_title"] == EXCLUDED_TITLE
    img = stored_image(storage, rows[-1]["id"])
    assert is_black(img, (400, 300)) and is_black(img, (799, 599)) and not is_black(img, (1000, 800))
    # Die Texterkennung bekommt dasselbe geschwaerzte Bild
    ocr_img = Image.open(__import__("io").BytesIO(ocr.jobs[-1][1]))
    assert ocr.jobs[-1][0] == rows[-1]["id"] and ocr_img.getpixel((400, 300)) == 0
    # Deny-Liste (Titel): der private Seitentitel landet nirgends
    fg["info"] = winutil.WindowInfo(4, "Bank - Inkognito - Firefox", 301, "firefox.exe", None, (0, 0, 800, 600))
    clock["ms"] += 5000
    loop.tick()
    rows = entries(storage)
    assert loop.state == CaptureState.RECORDING and len(rows) == 3
    assert rows[-1]["process_name"] == "firefox.exe" and rows[-1]["window_title"] == EXCLUDED_TITLE
    assert not any("Bank" in r["window_title"] or "kdbx" in r["window_title"] for r in rows)
    # zurueck -> normaler Eintrag, nichts mehr geschwaerzt
    fg["info"] = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (0, 0, 800, 600))
    clock["ms"] += 5000
    loop.tick()
    rows = entries(storage)
    assert loop.state == CaptureState.RECORDING and loop.state_detail == "" and len(rows) == 4
    assert not is_black(stored_image(storage, rows[-1]["id"]), (400, 300))
    # Pause
    loop.set_paused(True)
    loop.tick()
    assert loop.state == CaptureState.PAUSED and len(entries(storage)) == 4
    loop.set_paused(False)
    # gesperrt / inaktiv / kein Platz
    monkeypatch.setattr(winutil, "is_session_locked", lambda: True)
    loop.tick()
    assert loop.state == CaptureState.LOCKED
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 10_000.0)
    loop.tick()
    assert loop.state == CaptureState.IDLE
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 0)
    loop.tick()
    assert loop.state == CaptureState.NO_DISK
    assert len(entries(storage)) == 4
    assert CaptureState.RECORDING in states and CaptureState.EXCLUDED not in states


def test_capture_loop_runtime_exclusion(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    loop.add_exclusion("WINWORD.EXE")
    loop.tick()
    rows = entries(storage)
    assert loop.state == CaptureState.RECORDING and loop.state_detail == BLACKED_DETAIL
    assert rows[0]["window_title"] == EXCLUDED_TITLE and is_black(stored_image(storage, rows[0]["id"]), (500, 400))
    assert "WINWORD\\.EXE" in loop.cfg.excluded_process_regex


def test_excluded_window_opened_during_screenshot_is_blacked(storage, monkeypatch):
    """Erscheint ein privates Fenster erst zwischen Fensterabfrage und Bildschirmfoto, steckt es schon im Bild.
    Deshalb wird nach dem Foto noch einmal gefragt und beides geschwaerzt."""
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)
    private = winutil.WindowInfo(9, "Neuer Tab - [InPrivate] - Microsoft Edge", 900, "msedge.exe", None,
                                 (1000, 100, 1800, 900))
    calls = []

    def rects(is_ex):
        calls.append(1)
        return [private.rect] if len(calls) > 1 and is_ex(private) else []   # erst beim zweiten Mal da

    monkeypatch.setattr(winutil, "excluded_window_rects", rects)
    loop.tick()
    img = stored_image(storage, entries(storage)[0]["id"])
    assert len(calls) == 2 and is_black(img, (1400, 500)) and not is_black(img, (500, 500))


def test_black_out_geometry():
    from zeitspur.capture import black_out

    mon = {"left": -1920, "top": 0, "width": 1920, "height": 1080}      # Monitor links vom Hauptbildschirm
    img = Image.new("RGB", (1920, 1080), (255, 255, 255))
    # Fenster ragt ueber den Rand ins Nachbarbild, ein zweites liegt ganz auf einem anderen Monitor
    assert black_out(img, mon, [(-100, 50, 300, 250), (100, 100, 900, 700)]) == 1
    assert img.getpixel((1820, 50)) == (0, 0, 0) and img.getpixel((1919, 249)) == (0, 0, 0)
    assert img.getpixel((1819, 50)) == (255, 255, 255) and img.getpixel((1820, 250)) == (255, 255, 255)
    # Bild in anderer Groesse als der Monitor (Skalierung): Rechteck wird mitskaliert und eher groesser
    half = Image.new("RGB", (960, 540), (255, 255, 255))
    assert black_out(half, mon, [(-1920, 0, -1915, 3)]) == 1
    assert half.getpixel((0, 0)) == (0, 0, 0) and half.getpixel((2, 1)) == (0, 0, 0)
    assert half.getpixel((3, 0)) == (255, 255, 255)


def test_select_excluded_rects_includes_popups_and_dialogs():
    """Menues, Vorschlagslisten und Dialoge eines privaten Fensters sind eigene Fenster (oft ohne Titel) - sie
    gehoeren trotzdem geschwaerzt, die eines normalen Fensters nicht."""
    infos = {
        1: winutil.WindowInfo(1, "Bank - [InPrivate] - Microsoft Edge", 10, "msedge.exe", None, (0, 0, 900, 700)),
        2: winutil.WindowInfo(2, "Posteingang - Outlook", 11, "olk.exe", None, (900, 0, 1800, 700)),
        3: winutil.WindowInfo(3, "", 10, "msedge.exe", None, (50, 40, 600, 400)),          # Vorschlagsliste
        4: winutil.WindowInfo(4, "Speichern unter", 10, "msedge.exe", None, (200, 200, 700, 600)),
        5: winutil.WindowInfo(5, "", 11, "olk.exe", None, (950, 40, 1200, 300)),            # Outlook-Menue
        6: winutil.WindowInfo(6, "", 12, "KeePass.exe", None, (10, 10, 20, 20)),            # Prozess-Ausschluss
        7: winutil.WindowInfo(7, "Geheim - [InPrivate] - Microsoft Edge", 10, "msedge.exe", None, (0, 0, 1, 1)),
        8: winutil.WindowInfo(8, "", 10, "msedge.exe", None, (5, 5, 50, 50)),               # Besitzer unsichtbar
    }
    windows = [(3, infos[3].rect, "", 1), (4, infos[4].rect, "Speichern unter", 1), (1, infos[1].rect, infos[1].title, 0),
               (5, infos[5].rect, "", 2), (2, infos[2].rect, infos[2].title, 0), (6, infos[6].rect, "", 0),
               (8, infos[8].rect, "", 7)]   # Fenster 7 selbst ist nicht in der Liste (z. B. gerade verdeckt aufgebaut)
    cfg = Config(excluded_process_regex=["KeePass.*"], excluded_title_regex=[".*InPrivate.*"])
    loop = CaptureLoop(cfg, None, None, threading.Event())
    resolved = []

    def resolve(hwnd, title, rect):
        resolved.append(hwnd)
        return infos[hwnd]

    rects = winutil.select_excluded_rects(windows, loop.is_excluded, resolve)
    assert rects == [infos[3].rect, infos[4].rect, infos[1].rect, infos[6].rect, infos[8].rect]
    assert resolved.count(1) == 1     # Ergebnis je Fenster wird nur einmal ermittelt


def area(rects) -> int:
    return sum((r - l) * (b - t) for l, t, r, b in rects)


def test_subtract_rects():
    assert winutil.subtract_rects((0, 0, 100, 100), []) == [(0, 0, 100, 100)]
    assert winutil.subtract_rects((0, 0, 100, 100), [(200, 200, 300, 300)]) == [(0, 0, 100, 100)]
    assert winutil.subtract_rects((0, 0, 100, 100), [(-10, -10, 110, 110)]) == []
    ring = winutil.subtract_rects((0, 0, 100, 100), [(20, 30, 70, 80)])      # Loch in der Mitte
    assert area(ring) == 100 * 100 - 50 * 50
    assert all(not (l < 70 and r > 20 and t < 80 and b > 30) for l, t, r, b in ring)   # nichts im Loch
    two = winutil.subtract_rects((0, 0, 100, 100), [(0, 0, 60, 100), (50, 0, 100, 40)])
    assert two == [(60, 40, 100, 100)]


def test_select_spares_parts_covered_by_windows_in_front():
    """Heutiger Fall: InPrivate maximiert HINTER dem normalen Edge-Fenster. Geschwaerzt wird nur, was hervorschaut;
    durchscheinende Fenster davor (cover None) und Fenster dahinter verdecken nichts."""
    private = winutil.WindowInfo(5, "Bank - [InPrivate] - Microsoft Edge", 10, "msedge.exe", None, (-9, -9, 3449, 1401))
    infos = {
        1: winutil.WindowInfo(1, "araCloud - Microsoft Edge", 10, "msedge.exe", None, (91, 41, 2509, 1301)),
        2: winutil.WindowInfo(2, "Overlay", 20, "overlay.exe", None, (2600, 100, 3000, 500)),     # durchscheinend
        3: winutil.WindowInfo(3, "", 30, "app.exe", None, (3100, 100, 3300, 300)),               # ohne Titel
        5: private,
        6: winutil.WindowInfo(6, "Outlook", 40, "olk.exe", None, (0, 0, 3440, 1392)),            # dahinter
    }
    covers = {1: (110, 60, 2490, 1282), 2: None, 3: None, 6: (10, 10, 3430, 1382)}
    windows = [(h, infos[h].rect, infos[h].title, 0) for h in (1, 2, 3, 5, 6)]
    loop = CaptureLoop(Config(excluded_title_regex=[".*InPrivate.*"]), None, None, threading.Event())
    asked = []

    def cover(hwnd, title):
        asked.append(hwnd)
        return covers[hwnd]

    rects = winutil.select_excluded_rects(windows, loop.is_excluded, lambda h, t, r: infos[h], cover)
    assert area(rects) == area([private.rect]) - area([covers[1]])     # nur das Edge-Fenster davor spart aus
    inside = lambda x, y: any(l <= x < r and t <= y < b for l, t, r, b in rects)
    assert not inside(1000, 700)                                         # Mitte des Edge-Fensters bleibt sichtbar
    assert inside(100, 700) and inside(2500, 700)                        # dessen Rand (Ecken) wird geschwaerzt
    assert inside(2800, 300) and inside(3200, 200)                       # unter Overlay/titellosem Fenster schwarz
    assert inside(3300, 1000) and inside(0, 0)
    assert 6 not in asked                                                # Fenster dahinter wird gar nicht befragt
    # Ohne cover (alte Aufrufer): ganzes Rechteck
    assert winutil.select_excluded_rects(windows, loop.is_excluded, lambda h, t, r: infos[h]) == [private.rect]


def test_cover_rect_is_conservative_for_real_windows():
    """Auf echtem Windows: ein Fenster ohne Titel verdeckt nie etwas; ein Ergebnis liegt immer innerhalb des
    Fensterrechtecks (sichtbarer Rahmen, nach innen verkleinert)."""
    if not winutil.IS_WINDOWS:
        pytest.skip("nur unter Windows")
    import subprocess
    import sys

    # Eigener Prozess: DPI-Bewusstsein wie im Dienst (DWM liefert immer echte Pixel, GetWindowRect nur dann auch)
    code = (
        "import win32gui\n"
        "from zeitspur import winutil\n"
        "winutil.set_dpi_awareness()\n"
        "assert winutil._cover_rect(win32gui.GetDesktopWindow(), '') is None\n"
        "found = []\n"
        "win32gui.EnumWindows(lambda h, _: found.append(h) or True, None)\n"
        "n = 0\n"
        "for hwnd in found[:300]:\n"
        "    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):\n"
        "        continue\n"
        "    c = winutil._cover_rect(hwnd, win32gui.GetWindowText(hwnd))\n"
        "    if c is None:\n"
        "        continue\n"
        "    l, t, r, b = win32gui.GetWindowRect(hwnd)\n"
        "    assert l < c[0] < c[2] < r and t < c[1] < c[3] < b, (hwnd, c, (l, t, r, b))\n"
        "    n += 1\n"
        "print('ok', n)\n")
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    res = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and res.stdout.startswith("ok"), res.stderr


def test_capture_loop_survives_screenshot_errors(storage, monkeypatch):
    loop, frames, fg, clock, states, ocr = make_loop(storage, monkeypatch)

    def boom():
        raise RuntimeError("Screenshot fehlgeschlagen: kaputt")

    monkeypatch.setattr(loop, "_screenshots", boom)
    stop = loop.stop_event
    loop.cfg.capture_interval_seconds = 2
    loop.start()
    import time

    time.sleep(0.8)
    stop.set()
    loop.join(5)
    assert not loop.is_alive()
    assert loop.last_error and "kaputt" in loop.last_error
    assert loop.state == CaptureState.STOPPED


def two_monitor_loop(storage, monkeypatch, fg, top, visible=(), cfg=None):
    """Zwei Monitore nebeneinander; `visible` sind alle sichtbaren Fenster fuer die Ausschlusspruefung."""
    cfg = cfg or Config(capture_interval_seconds=5, change_threshold=0.01, excluded_process_regex=["KeePass.*"])
    loop = CaptureLoop(cfg, storage, None, threading.Event(), start_delay=0)
    mon1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    mon2 = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
    shot = lambda color: Image.new("RGB", (1920, 1080), color)   # so gross wie der Monitor, wie bei mss
    monkeypatch.setattr(loop, "_screenshots", lambda: [(1, mon1, shot((250, 250, 250))), (2, mon2, shot((200, 220, 240)))])
    monkeypatch.setattr(loop, "current_foreground", lambda: fg)
    monkeypatch.setattr(winutil, "top_window_per_monitor", lambda monitors: dict(top))
    monkeypatch.setattr(winutil, "excluded_window_rects", lambda is_ex: [w.rect for w in visible if is_ex(w)])
    clock = {"ms": timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 9, 0, 0))}
    monkeypatch.setattr(timeutil, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(winutil, "is_session_locked", lambda: False)
    monkeypatch.setattr(winutil, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(winutil, "free_disk_bytes", lambda p: 10 ** 12)
    return loop


WORD = winutil.WindowInfo(1, "Dokument - Word", 100, "WINWORD.EXE", None, (100, 100, 900, 700))


def test_capture_labels_each_monitor_with_its_own_window(storage, monkeypatch):
    """Auf einem Monitor ohne Vordergrundfenster wird das oberste Fenster dieses Monitors verwendet,
    nicht das Vordergrundfenster (behebt die Fehlbeschriftung mehrerer Monitore)."""
    outlook = winutil.WindowInfo(2, "Posteingang - Outlook", 200, "OUTLOOK.EXE", None, (1920, 0, 3840, 1080))
    loop = two_monitor_loop(storage, monkeypatch, WORD, {2: outlook}, [WORD, outlook])
    loop.tick()
    rows = storage.entries_between(0, 10 ** 15)
    by_mon = {r["monitor_id"]: r for r in rows}
    assert by_mon[1]["process_name"] == "WINWORD.EXE"
    assert by_mon[2]["process_name"] == "OUTLOOK.EXE" and by_mon[2]["window_title"] == "Posteingang - Outlook"
    assert loop.state_detail == ""


def test_capture_per_monitor_deny_blacks_only_that_window(storage, monkeypatch):
    """KeePass fuellt Monitor 2: der wird schwarz (Titel nicht gespeichert), Monitor 1 laeuft normal weiter."""
    keepass = winutil.WindowInfo(2, "Tresor.kdbx - KeePass", 200, "KeePass.exe", None, (1920, 0, 3840, 1080))
    loop = two_monitor_loop(storage, monkeypatch, WORD, {2: keepass}, [WORD, keepass])
    loop.tick()
    by_mon = {r["monitor_id"]: r for r in storage.entries_between(0, 10 ** 15)}
    assert set(by_mon) == {1, 2} and loop.state == CaptureState.RECORDING and loop.state_detail == BLACKED_DETAIL
    assert by_mon[2]["window_title"] == EXCLUDED_TITLE and by_mon[2]["process_name"] == "KeePass.exe"
    m2 = stored_image(storage, by_mon[2]["id"])
    assert is_black(m2, (0, 0)) and is_black(m2, (1919, 1079)) and is_black(m2, (960, 540))
    assert not is_black(stored_image(storage, by_mon[1]["id"]), (1500, 500))


def test_maximized_excluded_window_does_not_swallow_neighbour_monitor(storage, monkeypatch):
    """Heutiger Fehler (10.11.2026): Ein maximiertes Fenster ragt mit seinem unsichtbaren Rahmen ~7 px in die
    Nachbarmonitore. Frueher fiel deshalb die Aufnahme ALLER Monitore aus; jetzt wird dort nur dieser Streifen
    geschwaerzt."""
    private = winutil.WindowInfo(2, "Bank - [InPrivate] - Microsoft Edge", 200, "msedge.exe", None,
                                 (1913, -7, 3847, 1087))
    loop = two_monitor_loop(storage, monkeypatch, WORD, {2: private}, [WORD, private],
                            cfg=Config(capture_interval_seconds=5, change_threshold=0.01))
    loop.tick()
    by_mon = {r["monitor_id"]: r for r in storage.entries_between(0, 10 ** 15)}
    m1 = stored_image(storage, by_mon[1]["id"])
    assert by_mon[1]["window_title"] == "Dokument - Word"
    assert is_black(m1, (1915, 500)) and not is_black(m1, (1905, 500)) and not is_black(m1, (500, 500))
    assert by_mon[2]["window_title"] == EXCLUDED_TITLE and is_black(stored_image(storage, by_mon[2]["id"]), (960, 540))


def test_capture_deny_matches_visible_window_below_top(storage, monkeypatch):
    """Eine ausgeschlossene App, die sichtbar aber NICHT das oberste Fenster des Monitors ist, wird trotzdem
    geschwaerzt (fruehere Luecke: nur das oberste/fokussierte Fenster wurde geprueft)."""
    # Oberstes Fenster auf Monitor 2 ist ein Browser - NICHT ausgeschlossen ...
    browser = winutil.WindowInfo(2, "Seite - Firefox", 200, "firefox.exe", None, (1920, 0, 3840, 1080))
    # ... aber darunter liegt sichtbar ein ausgeschlossenes KeePass-Fenster.
    keepass = winutil.WindowInfo(3, "Tresor.kdbx - KeePass", 201, "KeePass.exe", None, (1960, 60, 3000, 900))
    loop = two_monitor_loop(storage, monkeypatch, WORD, {2: browser}, [WORD, browser, keepass])
    loop.tick()
    by_mon = {r["monitor_id"]: r for r in storage.entries_between(0, 10 ** 15)}
    m2 = stored_image(storage, by_mon[2]["id"])
    assert by_mon[2]["window_title"] == "Seite - Firefox"
    assert is_black(m2, (40, 60)) and is_black(m2, (1079, 899)) and is_black(m2, (500, 500))
    assert not is_black(m2, (1500, 500)) and not is_black(m2, (20, 20))


def test_encode_webp_bleibt_schlank_und_lesbar():
    """Der schnellere Kodiermodus darf das Bild nicht unbrauchbar oder riesig machen."""
    import io

    from PIL import Image

    from zeitspur.capture import WEBP_METHOD, encode_webp
    assert WEBP_METHOD == 2
    img = Image.new("RGB", (2400, 1400), (20, 40, 80))
    for x in range(0, 2400, 40):
        for y in range(0, 1400, 40):
            img.putpixel((x, y), (240, 240, 100))
    daten, breite, hoehe = encode_webp(img, 1920, 75)
    assert (breite, hoehe) == (1920, 1120)              # auf max_image_width skaliert
    zurueck = Image.open(io.BytesIO(daten))
    assert zurueck.size == (1920, 1120) and zurueck.format == "WEBP"
    assert len(daten) < 1920 * 1120 * 3 // 20           # deutlich kleiner als roh


def test_screenshots_lesen_die_monitore_nach_dem_andocken_neu_ein(storage, monkeypatch):
    """mss merkt sich die Monitore beim Anlegen. Nach dem Andocken blieb es deshalb bis zum naechsten Programmstart
    bei einem Monitor - aufgenommen wurde nur das Rechteck des Notebook-Bildschirms."""
    import mss

    from zeitspur import capture
    from zeitspur.capture import OpenBlock

    notebook = ((0, 0, 1920, 1200),)
    docked = ((-1920, 0, 0, 1080), (2560, 0, 4480, 1080), (0, 0, 2560, 1440))
    layout = {"now": notebook}
    created = []

    class FakeMss:
        def __init__(self):
            self.monitors = [{}] + [{"left": l, "top": t, "width": r - l, "height": b - t}
                                    for l, t, r, b in layout["now"]]
            self.closed = False
            created.append(self)

        def grab(self, mon):
            return (mon["width"] // 40, mon["height"] // 40)   # nur die Groesse zaehlt

        def close(self):
            self.closed = True

    monkeypatch.setattr(mss, "mss", FakeMss)
    monkeypatch.setattr(capture, "shot_to_image", lambda size: Image.new("RGB", size))
    monkeypatch.setattr(winutil, "monitor_layout", lambda: layout["now"])
    loop = CaptureLoop(Config(), storage, None, threading.Event(), start_delay=0)

    assert [m["width"] for _, m, _ in loop._screenshots()] == [1920]
    assert len(loop._screenshots()) == 1 and len(created) == 1          # unveraendert: nicht neu anlegen
    entry_id = storage.insert_entry(ts_start=1000, ts_end=1000, monitor_id=1, process_name="WINWORD.EXE",
                                    window_title="Dokument", exe_path=None, width=8, height=8,
                                    webp=encode_webp(Image.new("RGB", (8, 8)), 1920, 75)[0])
    loop._blocks[1] = OpenBlock(entry_id, 1000, 5000, "WINWORD.EXE", "Dokument")

    layout["now"] = docked                                              # Dockingstation: drei Monitore
    frames = loop._screenshots()
    assert [(i, m["left"], m["width"]) for i, m, _ in frames] == [(1, -1920, 1920), (2, 2560, 1920), (3, 0, 2560)]
    assert len(created) == 2 and created[0].closed
    assert loop._blocks == {}                                           # Monitor 1 ist jetzt ein anderer Bildschirm

    layout["now"] = notebook                                            # abgedockt: nichts Schwarzes weiter aufnehmen
    assert len(loop._screenshots()) == 1 and len(created) == 3 and created[1].closed
