"""Python<->JavaScript-Bruecke fuer das Zeitstrahl-Fenster (pywebview js_api).

pywebview ruft jede Methode in einem eigenen Thread auf; alle Rueckgaben sind JSON-serialisierbar.
Lesezugriffe laufen ueber eine eigene Nur-Lese-Verbindung, Schreibaktionen ueber die App (Writer).
Thumbnails werden aus dem verschluesselten BLOB im Speicher dekodiert und als Data-URI uebergeben -
es entsteht keine Bilddatei.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .. import __version__, autostart, edition, plugins, timeutil
from ..config import DB_FILE_NAME, Config, ConfigError, logs_dir, save_config
from ..storage import OCR_DONE, OCR_FAILED, OCR_PENDING, ReadOnlyStorage

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent
THUMB_CACHE_SIZE = 64
DEFAULT_THUMB_WIDTH = 800
OCR_STATUS_LABELS = {OCR_PENDING: "Texterkennung ausstehend", OCR_DONE: "Text erkannt", OCR_FAILED: "Texterkennung fehlgeschlagen"}
EVENT_CATEGORY_LABELS = {"meeting": "Besprechung", "appointment": "Termin", "call": "Anruf",
                         "visit": "Aufenthalt", "track": "Fahrt"}


DIRECTION_LABELS = {"outgoing": "ausgehend", "incoming": "eingehend"}


def _event_extra(e: dict[str, Any]) -> dict[str, Any]:
    import json as _json

    try:
        value = _json.loads(e.get("extra") or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_geo(extra: dict[str, Any]) -> dict[str, Any] | None:
    """Koordinaten fuer die Karte: entweder eine Strecke (Fahrt) oder ein Punkt (Aufenthalt)."""
    path = extra.get("path")
    if isinstance(path, list) and path:
        return {"path": path}
    lat, lon = extra.get("latitude"), extra.get("longitude")
    if lat is not None and lon is not None:
        try:
            return {"lat": float(lat), "lon": float(lon)}
        except (TypeError, ValueError):
            return None
    return None


def format_event(e: dict[str, Any], places=None) -> dict[str, Any]:
    """Rohes calendar_events-Row -> Anzeigeobjekt fuer Zeitstrahl/Detail.

    `places` benennt bekannte Koordinaten ("Buero" statt "52.51627, 13.37770"); ist der Ort
    unbekannt, bleiben die Koordinaten stehen - es wird keine Adresse geraten.
    """
    source = e.get("source") or ""
    extra = _event_extra(e)
    direction = extra.get("direction")
    subject = e.get("subject") or "(ohne Betreff)"
    place = None
    if edition.LOCATIONS and source == "dawarich" and places and extra.get("latitude") is not None:
        from ..dawarich import match_place

        hit = match_place(extra.get("latitude"), extra.get("longitude"), places)
        if hit is not None:
            place = hit.name
            if e.get("category") == "visit":
                subject = hit.name
    show = plugins.display(source)
    return {
        "geo": _event_geo(extra), "place": place, "lane": show["lane"],
        "id": e["id"], "source": source, "source_label": show["label"],
        "category": e.get("category"), "category_label": EVENT_CATEGORY_LABELS.get(e.get("category"), ""),
        "direction": direction, "direction_label": DIRECTION_LABELS.get(direction, ""),
        "ts_start": e["ts_start"], "ts_end": e["ts_end"],
        "subject": subject,
        "location": e.get("location"), "organizer": e.get("organizer"), "attendees": e.get("attendees"),
        "color": show["color"],
        "start_label": timeutil.fmt_hm(e["ts_start"]), "end_label": timeutil.fmt_hm(e["ts_end"]),
        "duration_label": timeutil.human_duration(e["ts_end"] - e["ts_start"]),
    }


# Seitenhintergrund (--bg in style.css) fuer hell/dunkel. Das Fenster bekommt ihn schon beim Erzeugen: WebView2
# malt erst, wenn das Fenster sichtbar ist - bis zum ersten Bild sieht man sonst kurz Weiss, auch im Dunkelmodus.
PAGE_BACKGROUND = {False: "#f4f5f7", True: "#14171c"}


def build_html() -> str:
    """Setzt index.html mit eingebettetem CSS/JS zusammen (kein HTTP-Server, keine externen Ressourcen)."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "style.css").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    # Die Kartenbibliothek gehoert zur Standort-Historie; im Release-Build ist sie gar nicht eingepackt.
    leaflet_css = leaflet_js = ""
    if edition.LOCATIONS:
        vendor = UI_DIR / "vendor"
        leaflet_css = (vendor / "leaflet.css").read_text(encoding="utf-8")
        leaflet_js = (vendor / "leaflet.js").read_text(encoding="utf-8")
    return (html.replace("/*__LEAFLET_CSS__*/", leaflet_css)
                .replace("/*__CSS__*/", css)
                .replace("/*__LEAFLET_JS__*/", leaflet_js)
                .replace("/*__JS__*/", js))


# Kategoriale Palette der Aktivitaetsbloecke: sechs Plaetze, danach neutral ("Sonstige").
# Nachgerechnet ueber ALLE Farbpaare (OKLab-dE x100, zusaetzlich Deuteran- und Protanopie simuliert):
# schlechtestes Paar 15.9 bei normalem Sehen (Schwelle 15) und 9.8 bei Farbsehschwaeche (Schwelle 8).
# Vorher wurden Farbtoene aus einem Namens-Hash gewuerfelt - dabei landeten Tuerkis, Hellblau und Gruen
# regelmaessig so dicht beieinander, dass sich Bloecke nicht mehr auseinanderhalten liessen. Feste
# Reihenfolge statt erzeugter Toene; die Farben tragen auf hellem wie dunklem Untergrund.
SERIES_COLORS = ("#1857b5", "#ff8c1a", "#0b6e4f", "#ff5fa2", "#7b3ff2", "#ffd21f")
# Textfarbe je Platz, nach Kontrast gewaehlt (weiss bzw. schwarz, jeweils der bessere Wert).
SERIES_TEXT = ("#ffffff", "#1b1b1b", "#ffffff", "#1b1b1b", "#ffffff", "#1b1b1b")
_series_cache: dict = {}
OTHER_COLOR, OTHER_TEXT = "#8a8d93", "#ffffff"


def assign_series(weights: dict[str, float],
                  exe_paths: dict[str, str | None] | None = None) -> dict[str, dict[str, str]]:
    """App -> Farbe und passende Textfarbe.

    Bevorzugt die Kennfarbe des Programmsymbols (Explorer gold, Teams blaulila, Claude dunkelorange) -
    die kennt der Nutzer ohnehin, das liest sich schneller als eine erfundene Farbe. Symbolfarben
    kollidieren allerdings gern (vier Blautoene unter den Microsoft-Apps), deshalb wird jede neue Farbe
    gegen die bereits vergebenen geprueft und notfalls in der Helligkeit verschoben, bevor der Farbton
    angetastet wird. Programme ohne farbiges Symbol bekommen einen Platz aus der festen Palette,
    alles darueber hinaus den neutralen Ton.

    Die Reihenfolge richtet sich nach der Nutzungsdauer des Tages (bei Gleichstand nach Namen), damit
    die wichtigsten Programme die klarsten Farben erhalten und das Ergebnis stabil bleibt.
    """
    from .. import appicon

    order = sorted(weights, key=lambda n: (-weights[n], n.lower()))
    paths = exe_paths or {}
    # Die Suche nach auseinanderliegenden Farben kostet spuerbar Zeit, das Ergebnis haengt aber nur an
    # der Reihenfolge und den Programmpfaden - beides aendert sich im Tagesverlauf kaum.
    key = (tuple(order), tuple(paths.get(n) for n in order))
    hit = _series_cache.get(key)
    if hit is not None:
        return hit
    out: dict[str, dict[str, str]] = {}
    taken: list[tuple[int, int, int]] = []
    benutzt: set[str] = set()   # schon vergebene Palettenfarben
    for name in order:
        rgb = None
        try:
            raw = appicon.icon_color(paths.get(name))
            if raw is not None:
                rgb = appicon.spread(appicon.normalize(raw), taken)
        except Exception:  # pragma: no cover - Symbol darf den Zeitstrahl nie aufhalten
            log.debug("Kennfarbe fuer %s fehlgeschlagen", name, exc_info=True)
        if rgb is None:
            # Graues oder fehlendes Symbol: aus der festen Palette den Ton nehmen, der am weitesten
            # von allem bereits Vergebenen entfernt liegt - stur der Reihe nach zu gehen stellte
            # sonst ein Palettenblau neben drei Symbolblautoene.
            frei = [c for c in SERIES_COLORS if c not in benutzt]
            if frei:
                pick = max(frei, key=lambda c: min((appicon.separation(_hex_to_rgb(c), t)[0]
                                                    for t in taken), default=999))
                benutzt.add(pick)
                rgb = appicon.spread(_hex_to_rgb(pick), taken)
            else:
                out[name] = {"color": OTHER_COLOR, "text": OTHER_TEXT}
                continue
        taken.append(rgb)
        out[name] = {"color": appicon.to_hex(rgb), "text": appicon.text_on(rgb)}
    if len(_series_cache) > 32:      # kleiner Deckel, der Zeitstrahl kennt nur wenige Zusammenstellungen
        _series_cache.clear()
    _series_cache[key] = out
    return out


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def group_blocks(entries: list[dict[str, Any]], gap_ms: int,
                 series: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Fasst aufeinanderfolgende Eintraege gleicher App/gleichen Fensters (Luecke <= gap) zu Bloecken zusammen."""
    blocks: list[dict[str, Any]] = []
    for e in entries:
        cur = blocks[-1] if blocks else None
        if (cur is not None and cur["process_name"] == e["process_name"]
                and cur["window_title"] == e["window_title"] and e["ts_start"] - cur["ts_end"] <= gap_ms):
            cur["ts_end"] = max(cur["ts_end"], e["ts_end"])
            cur["ids"].append(e["id"])
            cur["count"] += 1
            cur["ocr_pending"] += int(e.get("ocr_status") == OCR_PENDING)
        else:
            blocks.append({
                "id": e["id"], "ids": [e["id"]], "ts_start": e["ts_start"], "ts_end": max(e["ts_end"], e["ts_start"]),
                "process_name": e["process_name"], "window_title": e["window_title"],
                "count": 1,
                "ocr_pending": int(e.get("ocr_status") == OCR_PENDING),
            })
            look = (series or {}).get(e["process_name"] or e["window_title"]) or {}
            blocks[-1]["color"] = look.get("color", OTHER_COLOR)
            blocks[-1]["text_color"] = look.get("text", OTHER_TEXT)
    return blocks


def _as_bool(raw: Any) -> bool:
    """Formularwerte kommen als Strings an - bool('false') waere sonst True."""
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "ja", "on", "an")
    return bool(raw)


def coerce_config(values: dict[str, Any], base: Config | None = None) -> Config:
    """Formularwerte (Strings/Zahlen/Listen) -> validierte Config. Textareas liefern Listen als Zeilen."""
    cfg = Config() if base is None else Config(**base.to_dict())
    for f in fields(Config):
        if f.name not in values:
            continue
        raw = values[f.name]
        default = getattr(cfg, f.name)
        try:
            if isinstance(default, list):
                if isinstance(raw, str):
                    value: Any = [line.strip() for line in raw.splitlines() if line.strip()]
                else:
                    value = [str(x).strip() for x in raw if str(x).strip()]
            elif isinstance(default, bool):
                value = _as_bool(raw)
            elif isinstance(default, int):
                value = int(float(raw))
            elif isinstance(default, float):
                value = float(raw)
            else:
                value = str(raw).strip()
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{f.name}: ungültiger Wert {raw!r}") from e
        setattr(cfg, f.name, value)
    return cfg.validate()


class Bridge:
    def __init__(self, app: "App"):
        self._app = app  # Unterstrich: pywebview soll das App-Objekt nicht als API introspizieren
        self._reader: ReadOnlyStorage | None = None
        self._reader_lock = threading.RLock()
        self._thumbs: OrderedDict[tuple[int, int], dict[str, Any]] = OrderedDict()
        self._thumb_lock = threading.Lock()

    # ---------------------------------------------------------------- intern
    def _places(self) -> list:
        """Bekannte Orte aus der Konfiguration (bei jedem Aufruf frisch - Einstellungen aendern sich zur Laufzeit)."""
        if not edition.LOCATIONS:
            return []
        from ..dawarich import places_from_config

        try:
            return places_from_config(self._app.cfg)
        except Exception:
            log.debug("Bekannte Orte nicht lesbar", exc_info=True)
            return []

    def _store(self) -> ReadOnlyStorage:
        app = self._app
        if not app.ready.is_set() or app.storage is None or app.key is None:
            raise RuntimeError("Die Datenbank ist noch nicht geöffnet.")
        with self._reader_lock:
            if self._reader is None:
                self._reader = ReadOnlyStorage(app.storage.db_path, app.key)
            return self._reader

    def _close(self) -> None:
        """Interne Lifecycle-Methode (fuehrender Unterstrich -> NICHT ans Web exponiert)."""
        with self._reader_lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def _invalidate_cache(self, entry_id: int | None = None) -> None:
        with self._thumb_lock:
            if entry_id is None:
                self._thumbs.clear()
            else:
                for key in [k for k in self._thumbs if k[0] == entry_id]:
                    del self._thumbs[key]

    @staticmethod
    def _entry_labels(row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["start_label"] = timeutil.fmt_hms(row["ts_start"])
        row["end_label"] = timeutil.fmt_hms(row["ts_end"])
        row["date"] = timeutil.local_date(row["ts_start"]).isoformat()
        row["duration_label"] = timeutil.human_duration(row["ts_end"] - row["ts_start"])
        row["ocr_status_label"] = OCR_STATUS_LABELS.get(row.get("ocr_status"), "")
        return row

    # ---------------------------------------------------------------- Zustand
    def get_state(self) -> dict[str, Any]:
        app = self._app
        state, label, detail = app.capture_state()
        info: dict[str, Any] = {
            "version": __version__,
            "ready": app.ready.is_set(),
            "first_run": app.first_run,
            "capture_state": state.value,
            "capture_label": label,
            "capture_detail": detail,
            "paused": app.is_paused(),
            "tesseract_available": app.ocr_available(),
            "tesseract_version": app.engine.version() if app.engine and app.ocr_available() else "",
            "interval_ms": app.cfg.interval_ms,
            "retention_days": app.cfg.retention_days,
            "data_dir": str(app.data_dir),
            "today": date.today().isoformat(),
            "window_visible": bool(getattr(app, "_visible", True)),
            "installed_plugins": list(app.cfg.installed_plugins),
            "map_enabled": bool(app.cfg.map_enabled and edition.LOCATIONS),
            "features": edition.features(),
            "map_tile_url": app.cfg.map_tile_url,
            "map_home": {"lat": app.cfg.map_home_lat, "lon": app.cfg.map_home_lon,
                         "zoom": app.cfg.map_home_zoom, "label": app.cfg.map_home_label},
            "update": app.update_status(),   # None ohne Update-Kanal (eigener Build, Quelltext)
        }
        if info["ready"]:
            info["stats"] = app.status_summary()
        return info

    # ---------------------------------------------------------------- Updates
    def update_now(self) -> dict[str, Any] | None:
        """Knopf "Jetzt aktualisieren": laedt das angebotene Update und installiert es sofort."""
        self._app.update_now()
        return self._app.update_status()

    def check_updates(self) -> dict[str, Any] | None:
        """Knopf "Nach Updates suchen" in den Einstellungen - das Ergebnis zeigt die Seite selbst."""
        self._app.check_updates(notify=False)
        return self._app.update_status()

    def list_days(self) -> list[str]:
        return self._store().list_days()

    # ---------------------------------------------------------------- Zeitstrahl
    def get_day(self, day: str) -> dict[str, Any]:
        d = timeutil.parse_date(day)
        start, end = timeutil.day_bounds(d)
        rows = self._store().entries_between(start, end)
        per_monitor: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            per_monitor.setdefault(int(r["monitor_id"]), []).append(r)
        gap = 3 * self._app.cfg.interval_ms
        weights: dict[str, float] = {}
        exe_paths: dict[str, str | None] = {}
        for r in rows:  # Farbplaetze nach Nutzungsdauer, nicht nach Zufall
            key = r["process_name"] or r["window_title"]
            weights[key] = weights.get(key, 0) + max(0, r["ts_end"] - r["ts_start"])
            if r.get("exe_path") and not exe_paths.get(key):
                exe_paths[key] = r["exe_path"]
        series = assign_series(weights, exe_paths)
        lanes = []
        active_ms = 0
        for monitor_id in sorted(per_monitor):
            blocks = group_blocks(per_monitor[monitor_id], gap, series)
            lane_active = sum(max(0, min(b["ts_end"], end) - max(b["ts_start"], start)) for b in blocks)
            active_ms = max(active_ms, lane_active)
            lanes.append({"monitor_id": monitor_id, "blocks": blocks})
        events = [format_event(e, self._places()) for e in self._store().events_between(start, end)]
        # Termine/Anrufe des Tages bei Bedarf frisch nachziehen (z. B. beim Blaettern in vergangene Tage)
        try:
            self._app.request_event_sync(d)
        except Exception:
            log.debug("request_event_sync fehlgeschlagen", exc_info=True)
        return {
            "date": d.isoformat(), "start_ms": start, "end_ms": end, "lanes": lanes, "events": events,
            "total_entries": len(rows), "active_ms": active_ms,
            "first_ms": min((r["ts_start"] for r in rows), default=None),
            "last_ms": max((r["ts_end"] for r in rows), default=None),
        }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        store = self._store()
        row = store.get_entry(int(entry_id))
        if row is None:
            return None
        prev_id, next_id = store.neighbor_ids(int(entry_id))
        out = self._entry_labels(row)
        out["prev_id"], out["next_id"] = prev_id, next_id
        return out

    def get_thumbnail(self, entry_id: int, max_width: int = DEFAULT_THUMB_WIDTH) -> dict[str, Any] | None:
        entry_id = int(entry_id)
        max_width = max(64, min(int(max_width), 4096))
        key = (entry_id, max_width)
        with self._thumb_lock:
            cached = self._thumbs.get(key)
            if cached is not None:
                self._thumbs.move_to_end(key)
                return cached
        webp = self._store().get_image(entry_id)
        if webp is None:
            return None
        img = Image.open(io.BytesIO(webp))
        width, height = img.size
        if width <= max_width:
            data, mime = webp, "image/webp"
        else:
            img.thumbnail((max_width, max_width * 4))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=80, optimize=True)
            data, mime = buf.getvalue(), "image/jpeg"
            width, height = img.size
        result = {"entry_id": entry_id, "width": width, "height": height,
                  "data_uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
        with self._thumb_lock:
            self._thumbs[key] = result
            while len(self._thumbs) > THUMB_CACHE_SIZE:
                self._thumbs.popitem(last=False)
        return result

    def search(self, query: str, from_date: str | None = None, to_date: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        from_ms = timeutil.day_bounds(timeutil.parse_date(from_date))[0] if from_date else None
        to_ms = timeutil.day_bounds(timeutil.parse_date(to_date))[1] if to_date else None
        rows = self._store().search(query or "", from_ms=from_ms, to_ms=to_ms, limit=int(limit))
        out = []
        for r in rows:
            r = dict(r)
            r["date"] = timeutil.local_date(r["ts_start"]).isoformat()
            r["time_label"] = timeutil.fmt_hm(r["ts_start"])
            r["snippet"] = " ".join((r.get("snippet") or "").split())
            out.append(r)
        return out

    # ---------------------------------------------------------------- Aktionen
    def set_paused(self, paused: bool) -> dict[str, Any]:
        self._app.set_paused(bool(paused))
        return self.get_state()

    def delete_entry(self, entry_id: int) -> dict[str, int]:
        return {"deleted": self._app.delete_entry(int(entry_id))}

    def delete_last_minutes(self, minutes: int) -> dict[str, int]:
        return {"deleted": self._app.delete_recent(max(1, int(minutes)))}

    # ---- Ereignis-Plugins ------------------------------------------------
    def list_plugins(self) -> list[dict[str, Any]]:
        return self._app.plugin_list()

    def add_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self._app.add_plugin(str(plugin_id))

    def remove_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self._app.remove_plugin(str(plugin_id))

    def save_plugin_credentials(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        plugin = plugins.get(str(plugin_id))
        values = values if isinstance(values, dict) else {}
        clean: dict[str, str] = {}
        for f in plugin.credential_fields:
            raw = values.get(f.key)
            value = "" if raw is None else str(raw)
            if f.kind != "secret":
                value = value.strip()
            if not value.strip():
                raise ValueError(f"{f.label} ist erforderlich.")
            clean[f.key] = value
        return {"ok": True, "message": self._app.save_plugin_credentials(plugin.id, clean)}

    def test_plugin(self, plugin_id: str) -> dict[str, str]:
        return {"message": self._app.test_plugin(str(plugin_id))}

    def clear_plugin_credentials(self, plugin_id: str) -> dict[str, bool]:
        return {"cleared": self._app.clear_plugin_credentials(str(plugin_id))}

    def reveal_plugin_credentials(self, plugin_id: str) -> dict[str, Any]:
        """Gespeicherte Zugangsdaten im Klartext - nur auf ausdruecklichen Klick in den Einstellungen.

        Kein Sicherheitsverlust: Die Werte liegen DPAPI-geschuetzt und an dieses Windows-Konto gebunden;
        wer an der entsperrten Sitzung sitzt, koennte sie ohnehin entschluesseln. Ohne diesen Weg waeren
        sie dagegen nur schreibbar - wer sie woanders verliert, kaeme nicht mehr heran.
        """
        creds = self._app.reveal_plugin_credentials(str(plugin_id))
        if not creds or not any(creds.values()):
            raise ValueError("Es sind keine Zugangsdaten hinterlegt.")
        return creds

    def sync_events_now(self) -> dict[str, Any]:
        from datetime import date as _date

        self._app.request_event_sync(_date.today())
        return {"ok": True}

    @staticmethod
    def _take_autostart(values: dict[str, Any]) -> bool | None:
        """Loest den Autostart-Schalter aus den Formularwerten: er gehoert in die Registry, nicht in config.yaml."""
        if "autostart" not in values:
            return None
        return _as_bool(values.pop("autostart"))

    def get_config(self) -> dict[str, Any]:
        values = self._app.cfg.to_dict()
        values["autostart"] = autostart.is_enabled()
        return values

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        wanted = self._take_autostart(values)
        try:
            cfg = coerce_config(values, self._app.cfg)
        except ConfigError as e:
            raise ValueError(str(e)) from e
        save_config(cfg)
        restart = self._app.apply_config(cfg)
        result = {"ok": True, "restart_required": restart}
        if wanted is not None:
            result["autostart"] = autostart.set_enabled(wanted)
        return result

    def complete_setup(self, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        wanted = self._take_autostart(values)
        try:
            cfg = coerce_config(values)
        except ConfigError as e:
            raise ValueError(str(e)) from e
        ok = self._app.complete_first_run(cfg)
        if wanted is not None:
            autostart.set_enabled(wanted)
        info = self.get_state()
        info["ok"] = ok
        return info

    def choose_folder(self) -> str | None:
        window = self._app.window
        if window is None:
            return None
        import webview

        dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if dialog_type is None:  # pragma: no cover - aeltere pywebview-Version
            dialog_type = webview.FOLDER_DIALOG  # type: ignore[attr-defined]
        current = self._app.cfg.resolved_db_path.parent
        result = window.create_file_dialog(dialog_type, directory=str(current if current.exists() else Path.home()))
        if not result:
            return None
        folder = result[0] if isinstance(result, (list, tuple)) else str(result)
        return str(Path(folder) / DB_FILE_NAME)

    def open_logs(self) -> bool:
        self._app.open_logs()
        return True

    def open_data_dir(self) -> bool:
        try:
            os.startfile(str(self._app.data_dir))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def compact_database(self) -> dict[str, str]:
        return {"message": self._app.compact_database()}

    def quit_app(self) -> bool:
        self._app.request_quit()
        return True

    def logs_path(self) -> str:
        return str(logs_dir())
