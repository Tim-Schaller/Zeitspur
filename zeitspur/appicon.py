"""Kennfarbe eines Programms aus seinem Symbol.

Der Zeitstrahl faerbt Aktivitaetsbloecke nach Programm. Eine aus dem Namen erzeugte Farbe ist
beliebig; die Farbe des Programmsymbols dagegen ist die, die der Nutzer ohnehin kennt (Explorer
gold, Teams blaulila, Claude dunkelorange). Dieses Modul zieht sie aus der EXE.

Vorgehen: Symbol per ExtractIconEx laden und zweimal zeichnen - einmal auf weissem, einmal auf
schwarzem Grund. Pixel, die auf beiden Untergruenden gleich aussehen, sind deckend; alle anderen
sind durchscheinend und werden verworfen (GDI liefert beim Zeichnen keinen Alphakanal mit).
Aus den deckenden, farbigen Pixeln gewinnt ein nach Sattigung gewichtetes Farbton-Histogramm die
Kennfarbe. Graue Symbole (z. B. reine Umriss-Symbole) haben keine und liefern None.

Die Ergebnisse werden je Datei zwischengespeichert - das Zeichnen kostet zwar nur Millisekunden,
der Zeitstrahl fragt aber bei jedem Tageswechsel erneut.
"""
from __future__ import annotations

import colorsys
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

ICON_SIZE = 32
MIN_SATURATION = 0.25   # darunter ist es grau und keine Kennfarbe
MIN_VALUE = 0.15        # fast schwarz
MAX_VALUE = 0.97        # fast weiss
HUE_BINS = 24

_cache: dict[str, tuple[float, tuple[int, int, int] | None]] = {}
_lock = threading.Lock()


def _draw_on(hicon, background: int):
    """Zeichnet das Symbol auf einen einfarbigen Grund und gibt das Bild zurueck."""
    import win32gui
    import win32ui
    from PIL import Image

    screen = win32gui.GetDC(0)
    hdc = win32ui.CreateDCFromHandle(screen)
    mem = bmp = None
    try:
        mem = hdc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc, ICON_SIZE, ICON_SIZE)
        mem.SelectObject(bmp)
        mem.FillSolidRect((0, 0, ICON_SIZE, ICON_SIZE), background)
        mem.DrawIcon((0, 0), hicon)
        raw = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGBA", (ICON_SIZE, ICON_SIZE), raw, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        if bmp is not None:
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
        if mem is not None:
            try:
                mem.DeleteDC()
            except Exception:
                pass
        try:
            hdc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(0, screen)
        except Exception:
            pass


def _extract(path: str) -> tuple[int, int, int] | None:
    import win32gui

    try:
        large, small = win32gui.ExtractIconEx(path, 0)
    except Exception as e:
        log.debug("Symbol von %s nicht ladbar: %s", path, e)
        return None
    handles = (large or []) + (small or [])
    icons = large or small
    if not icons:
        return None
    try:
        on_white = _draw_on(icons[0], 0xFFFFFF).load()
        on_black = _draw_on(icons[0], 0x000000).load()
    except Exception as e:
        log.debug("Symbol von %s nicht zeichenbar: %s", path, e)
        return None
    finally:
        for h in handles:
            try:
                win32gui.DestroyIcon(h)
            except Exception:
                pass

    bins: dict[int, list[float]] = {}
    for y in range(ICON_SIZE):
        for x in range(ICON_SIZE):
            cw, cb = on_white[x, y], on_black[x, y]
            if max(abs(cw[i] - cb[i]) for i in range(3)) >= 24:
                continue  # durchscheinend: der Untergrund schlaegt durch
            r, g, b = cb
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if s < MIN_SATURATION or v < MIN_VALUE or v > MAX_VALUE:
                continue
            acc = bins.setdefault(int(h * HUE_BINS) % HUE_BINS, [0.0, 0.0, 0.0, 0.0])
            weight = s * v  # kraeftige Pixel bestimmen die Kennfarbe, blasse kaum
            acc[0] += r * weight
            acc[1] += g * weight
            acc[2] += b * weight
            acc[3] += weight
    if not bins:
        return None
    acc = bins[max(bins, key=lambda k: bins[k][3])]
    return tuple(round(acc[i] / acc[3]) for i in range(3))  # type: ignore[return-value]


def icon_color(exe_path: str | None) -> tuple[int, int, int] | None:
    """Kennfarbe des Programms, oder None (kein Pfad, kein Symbol, graues Symbol).

    Zwischengespeichert je Datei und Aenderungszeit, damit ein Programmupdate eine neue Farbe ergibt.
    """
    if not exe_path:
        return None
    try:
        stamp = Path(exe_path).stat().st_mtime
    except OSError:
        return None
    with _lock:
        hit = _cache.get(exe_path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        color = _extract(exe_path)
    except Exception:
        log.debug("Kennfarbe von %s fehlgeschlagen", exe_path, exc_info=True)
        color = None
    with _lock:
        _cache[exe_path] = (stamp, color)
    return color


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- Farbaufbereitung
# Symbolfarben sind bunt gemischt: manche fast schwarz, manche neonhell. Fuer Bloecke braucht es ein
# einheitliches Band, sonst sind manche Flaechen grell und andere unlesbar. Gerechnet wird in OKLCH,
# weil dort Helligkeit und Buntheit getrennt regelbar sind.

BLOCK_L = (0.50, 0.68)      # Helligkeitsband der Bloecke
BLOCK_C = (0.10, 0.20)      # Buntheit: kraeftig genug, aber nicht grell
MIN_SEPARATION = 12.0       # OKLab-dE x100 zwischen zwei Blockfarben (normales Sehen)
MIN_SEPARATION_CVD = 7.0    # dasselbe unter Rot-Gruen-Schwaeche


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055


def to_oklab(rgb) -> tuple[float, float, float]:
    r, g, b = (_srgb_to_linear(v / 255) for v in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def from_oklab(L: float, a: float, b: float) -> tuple[int, int, int]:
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = 0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return tuple(round(255 * _linear_to_srgb(v)) for v in (r, g, bb))  # type: ignore[return-value]


def _simulate_cvd(rgb, kind: str):
    """Vienot-Naeherung fuer Deuteranopie/Protanopie - zum Pruefen, ob zwei Farben auch dann noch
    auseinanderzuhalten sind."""
    r, g, b = (_srgb_to_linear(v / 255) for v in rgb)
    L = 17.8824 * r + 43.5161 * g + 4.11935 * b
    M = 3.45565 * r + 27.1554 * g + 3.86714 * b
    S = 0.0299566 * r + 0.184309 * g + 1.46709 * b
    if kind == "deuteran":
        M = 0.494207 * L + 1.24827 * S
    else:
        L = 2.02344 * M - 2.52581 * S
    out = (0.080944 * L - 0.130504 * M + 0.116721 * S,
           -0.0102485 * L + 0.0540194 * M - 0.113615 * S,
           -0.000365294 * L - 0.00412163 * M + 0.693513 * S)
    return tuple(round(255 * _linear_to_srgb(v)) for v in out)


def distance(a, b) -> float:
    import math

    return 100 * math.dist(to_oklab(a), to_oklab(b))


def separation(a, b) -> tuple[float, float]:
    """(Abstand bei normalem Sehen, schlechtester Abstand bei Rot-Gruen-Schwaeche)."""
    cvd = min(distance(_simulate_cvd(a, "deuteran"), _simulate_cvd(b, "deuteran")),
              distance(_simulate_cvd(a, "protan"), _simulate_cvd(b, "protan")))
    return distance(a, b), cvd


def _to_lch(rgb):
    import math

    L, a, b = to_oklab(rgb)
    return L, math.hypot(a, b), math.atan2(b, a)


def _from_lch(L, C, h):
    import math

    return from_oklab(L, C * math.cos(h), C * math.sin(h))


def normalize(rgb) -> tuple[int, int, int]:
    """Symbolfarbe in das Blockband bringen - Farbton bleibt, Helligkeit und Buntheit werden gezaehmt."""
    L, C, h = _to_lch(rgb)
    return _from_lch(min(max(L, BLOCK_L[0]), BLOCK_L[1]), min(max(C, BLOCK_C[0]), BLOCK_C[1]), h)


def spread(rgb, taken) -> tuple[int, int, int]:
    """Weicht einer schon vergebenen Farbe aus, ohne den Farbcharakter aufzugeben.

    Zuerst ueber die Helligkeit (dunkleres/helleres Blau bleibt Blau), erst danach mit einer kleinen
    Drehung des Farbtons. Passt nichts, gewinnt die Variante mit dem groessten Abstand - eine
    Kennfarbe ist immer noch besser als gar keine Zuordnung.
    """
    import math

    if not taken:
        return rgb
    L, C, h = _to_lch(rgb)
    # Kandidaten nach "Wie weit weiche ich vom Symbol ab?" sortieren. Eine Drehung des Farbtons
    # zaehlt doppelt: Helligkeit aendert Blau zu hellerem Blau, der Farbton macht daraus Gruen.
    combos = [(abs(dL) / 0.27 + 2 * abs(dh) / 0.62, dL, dh)
              for dL in (0.0, 0.11, -0.11, 0.19, -0.19, 0.27, -0.27)
              for dh in (0.0, 0.22, -0.22, 0.42, -0.42, 0.62, -0.62)]
    combos.sort(key=lambda c: c[0])
    # Die schon vergebenen Farben aendern sich waehrend der Suche nicht - einmal umrechnen genuegt.
    ref = [(to_oklab(t), to_oklab(_simulate_cvd(t, "deuteran")), to_oklab(_simulate_cvd(t, "protan")))
           for t in taken]
    best, best_score = None, -1.0
    for _cost, dL, dh in combos:
        cand = _from_lch(min(max(L + dL, BLOCK_L[0] - 0.12), BLOCK_L[1] + 0.12),
                         min(max(C, BLOCK_C[0]), BLOCK_C[1]), h + dh)
        cn, cd, cp = (to_oklab(cand), to_oklab(_simulate_cvd(cand, "deuteran")),
                      to_oklab(_simulate_cvd(cand, "protan")))
        worst_n = min(100 * math.dist(cn, t[0]) for t in ref)
        worst_c = min(min(100 * math.dist(cd, t[1]), 100 * math.dist(cp, t[2])) for t in ref)
        if worst_n >= MIN_SEPARATION and worst_c >= MIN_SEPARATION_CVD:
            return cand
        score = min(worst_n, worst_c * 1.6)
        if score > best_score:
            best, best_score = cand, score
    return best or rgb


def text_on(rgb) -> str:
    """Schwarz oder Weiss - je nachdem, was auf dieser Flaeche besser lesbar ist."""
    r, g, b = (_srgb_to_linear(v / 255) for v in rgb)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1b1b1b" if (lum + 0.05) / 0.05 > 1.05 / (lum + 0.05) else "#ffffff"


def to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)
