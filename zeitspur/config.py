"""Konfiguration: %LOCALAPPDATA%\\Zeitspur\\config.yaml laden, validieren, speichern."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from . import APP_NAME, edition

log = logging.getLogger(__name__)

ENV_DATA_DIR = "ZEITSPUR_DATA_DIR"  # Override fuer Tests / portable Nutzung

DB_FILE_NAME = "zeitspur.db"
DEFAULT_DB_PATH = f"%LOCALAPPDATA%/Zeitspur/{DB_FILE_NAME}"
DEFAULT_EXCLUDED_PROCESSES = ["KeePass.*", ".*Banking.*"]
DEFAULT_EXCLUDED_TITLES = [".*Inkognito.*", ".*Private Browsing.*", ".*InPrivate.*", ".*Privates Fenster.*"]


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    # --- Werte aus der Spezifikation ---
    capture_interval_seconds: int = 5
    idle_pause_minutes: int = 3
    retention_days: int = 14
    max_image_width: int = 1920
    webp_quality: int = 75
    db_path: str = DEFAULT_DB_PATH
    excluded_process_regex: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_PROCESSES))
    excluded_title_regex: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_TITLES))
    ocr_language: str = "deu+eng"
    # --- Ergaenzungen ---
    change_threshold: float = 0.015      # Anteil geaenderter Pixel, ab dem ein Frame als neu gilt
    max_block_minutes: int = 10          # spaetestens danach wird ein neuer Keyframe gespeichert
    ocr_psm: int = 3                     # Tesseract page segmentation mode (3 = auto, 11 = sparse text)
    ocr_min_confidence: int = 40         # Woerter mit geringerer Konfidenz werden verworfen
    ocr_queue_max: int = 20              # maximale Anzahl wartender OCR-Jobs im Speicher
    max_db_size_gb: float = 20.0         # aelteste Tage werden geloescht, wenn die DB groesser wird
    min_free_disk_gb: float = 2.0        # Aufnahme pausiert, wenn weniger frei ist
    backup_count: int = 2                 # taegliche Sicherungen der Datenbank (0 = aus)
    backup_max_gb: float = 5.0            # darueber wird nicht gesichert (Platzbedarf)
    log_level: str = "INFO"
    # --- Phase 2: Integrationen ---
    # Ereignis-Plugins (Outlook, Teams, Dawarich): mitgeliefert, aber inaktiv, bis sie hier stehen.
    # Verwaltet ueber die Einstellungen -> Plugins; siehe zeitspur/plugins.py.
    installed_plugins: list[str] = field(default_factory=list)
    # Einstellungen der Plugins je Plugin-Id (Felder beschreibt das Plugin selbst). Zugangsdaten nie hier.
    plugin_settings: dict[str, dict] = field(default_factory=dict)
    teams_user_id: str = ""              # Teams-Plugin: Azure-AD-Objekt-Id -> nur EIGENE Anrufe (statt aller im Tenant)
    teams_user_names: list[str] = field(default_factory=list)  # Teams-Plugin: Anzeigenamen als Rueckfall
    map_enabled: bool = False             # Karte im Zeitstrahl; laedt Kacheln aus dem Netz (siehe README)
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"  # austauschbar, z. B. eigener Server
    # Startausschnitt der Karte, wenn (noch) nichts anzuzeigen ist: ab Werk ganz Deutschland. Mit Namen
    # (map_home_label, etwa "Buero") wird der Punkt zum Bezugspunkt auf der Karte und zum bekannten Ort.
    map_home_lat: float = 51.163
    map_home_lon: float = 10.448
    map_home_zoom: int = 6
    map_home_label: str = ""
    # Weitere benannte Orte, damit aus Koordinaten Namen werden: "Name;Breite;Laenge[;Radius in m]".
    # Der Kartenstartpunkt oben gilt automatisch als bekannter Ort.
    known_places: list[str] = field(default_factory=list)
    events_sync_minutes: int = 15        # Intervall der Termin-/Anruf-Synchronisierung
    events_window_days: int = 14         # Tage rueckwaerts/vorwaerts (passt zur Screenshot-Aufbewahrung)
    # Updates (nur Release-Build, siehe updater.py): im Leerlauf selbst installieren; aus = nur Hinweis
    auto_update: bool = True
    update_idle_minutes: int = 5         # so lange ohne Maus-/Tastatureingabe, bevor ein Update installiert wird

    # ---------- abgeleitete Werte ----------
    @property
    def resolved_db_path(self) -> Path:
        """Der Standardpfad folgt dem Datenordner (auch bei ZEITSPUR_DATA_DIR-Override);
        ein explizit gesetzter db_path wird nur expandiert."""
        if self.db_path.strip() == DEFAULT_DB_PATH:
            return data_dir() / DB_FILE_NAME
        return Path(expand_path(self.db_path))

    @property
    def interval_ms(self) -> int:
        return self.capture_interval_seconds * 1000

    @property
    def max_block_ms(self) -> int:
        return self.max_block_minutes * 60_000

    def process_patterns(self) -> list[re.Pattern[str]]:
        return _compile_all(self.excluded_process_regex, "excluded_process_regex")

    def title_patterns(self) -> list[re.Pattern[str]]:
        return _compile_all(self.excluded_title_regex, "excluded_title_regex")

    def validate(self) -> "Config":
        _check_range(self, "capture_interval_seconds", 2, 60)
        _check_range(self, "idle_pause_minutes", 1, 240)
        _check_range(self, "retention_days", 1, 3650)
        _check_range(self, "max_image_width", 640, 7680)
        _check_range(self, "webp_quality", 30, 95)
        _check_range(self, "change_threshold", 0.0, 1.0)
        _check_range(self, "max_block_minutes", 1, 240)
        _check_range(self, "ocr_psm", 0, 13)
        _check_range(self, "ocr_min_confidence", 0, 100)
        _check_range(self, "ocr_queue_max", 1, 500)
        _check_range(self, "max_db_size_gb", 0.1, 10000.0)
        _check_range(self, "min_free_disk_gb", 0.0, 10000.0)
        _check_range(self, "events_sync_minutes", 1, 1440)
        _check_range(self, "events_window_days", 1, 90)
        if not isinstance(self.map_enabled, bool):
            raise ConfigError("map_enabled muss true oder false sein")
        if not isinstance(self.auto_update, bool):
            raise ConfigError("auto_update muss true oder false sein")
        _check_range(self, "update_idle_minutes", 1, 240)
        if not isinstance(self.installed_plugins, list) or not all(isinstance(v, str) for v in self.installed_plugins):
            raise ConfigError("installed_plugins muss eine Liste von Plugin-Namen sein")
        _check_plugin_settings(self.plugin_settings)
        if not isinstance(self.teams_user_id, str):
            raise ConfigError("teams_user_id muss eine Zeichenkette (Azure-AD-Objekt-Id) sein")
        if not isinstance(self.teams_user_names, list) or not all(isinstance(v, str) for v in self.teams_user_names):
            raise ConfigError("teams_user_names muss eine Liste von Zeichenketten sein")
        if not isinstance(self.map_tile_url, str) or not self.map_tile_url.startswith("https://"):
            raise ConfigError("map_tile_url muss eine https-Adresse sein")
        if not all(t in self.map_tile_url for t in ("{z}", "{x}", "{y}")):
            raise ConfigError("map_tile_url braucht die Platzhalter {z}, {x} und {y}")
        _check_range(self, "backup_count", 0, 30)
        _check_range(self, "backup_max_gb", 0.0, 1000.0)
        _check_range(self, "map_home_lat", -90.0, 90.0)
        _check_range(self, "map_home_lon", -180.0, 180.0)
        _check_range(self, "map_home_zoom", 1, 19)
        if not isinstance(self.known_places, list) or not all(isinstance(v, str) for v in self.known_places):
            raise ConfigError("known_places muss eine Liste von Zeichenketten sein")
        # Bekannte Orte gehoeren zur Standort-Historie. Fehlt sie in dieser Ausgabe, bleiben die Eintraege
        # ungeprueft liegen (sie wirken ja nicht) - statt dass jede Konfiguration mit Orten scheitert.
        if edition.LOCATIONS:
            from .location import parse_known_place

            for entry in self.known_places:
                try:
                    parse_known_place(entry)
                except ValueError as e:
                    raise ConfigError(f"known_places: {e}") from e
        if not isinstance(self.map_home_label, str):
            raise ConfigError("map_home_label muss eine Zeichenkette sein")
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ConfigError("db_path darf nicht leer sein")
        if not re.fullmatch(r"[a-z_]{3,}(\+[a-z_]{3,})*", self.ocr_language or ""):
            raise ConfigError(f"ocr_language ungueltig: {self.ocr_language!r} (erwartet z. B. deu+eng)")
        if str(self.log_level).upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"log_level ungueltig: {self.log_level!r}")
        self.log_level = str(self.log_level).upper()
        for name in ("excluded_process_regex", "excluded_title_regex"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{name} muss eine Liste von Zeichenketten sein")
        self.process_patterns()
        self.title_patterns()
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def _check_plugin_settings(value) -> None:
    """plugin_settings: {plugin-id: {schluessel: einfacher Wert oder Liste von Zeichenketten}}."""
    if not isinstance(value, dict):
        raise ConfigError("plugin_settings muss ein Mapping je Plugin sein")
    for pid, settings in value.items():
        if not isinstance(pid, str) or not isinstance(settings, dict):
            raise ConfigError(f"plugin_settings.{pid}: erwartet ein Mapping von Einstellungen")
        for key, v in settings.items():
            ok = (v is None or isinstance(v, (str, int, float, bool))
                  or (isinstance(v, list) and all(isinstance(x, str) for x in v)))
            if not isinstance(key, str) or not ok:
                raise ConfigError(f"plugin_settings.{pid}.{key}: ungueltiger Wert")


def _check_range(cfg: Config, name: str, lo, hi) -> None:
    value = getattr(cfg, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} muss eine Zahl sein (ist {value!r})")
    if not (lo <= value <= hi):
        raise ConfigError(f"{name}={value!r} liegt ausserhalb von {lo}..{hi}")


_regex_cache: dict[tuple[str, ...], list[re.Pattern[str]]] = {}

# Sonden, die klassisches katastrophales Backtracking ausloesen ((a+)+, (a|a)*, (.*x){n} ...). Laenge so
# gewaehlt, dass ein verwundbares Muster messbar langsam wird, die Sonde selbst aber in ~1 s durchlaeuft.
_REDOS_PROBES = ["a" * 26 + "!", "0" * 26 + "!", " " * 26 + "X", ("ab" * 13) + "!"]
_REDOS_BUDGET_S = 0.25

# Strukturelle Ablehnung der klassischen Backtracking-Signatur (ein Quantor auf einer Gruppe, die selbst einen
# unbegrenzten Quantor enthaelt, z. B. (a+)+, (.*)* ). Das greift unabhaengig davon, welche Eingabe ein Angreifer
# spaeter liefert - die Sonden oben koennen je nach Musterform daneben treffen.
_NESTED_QUANT = re.compile(r"""\((?:\?[:=!][^()]*|[^()]*)[*+][^()]*\)\s*[*+{]""", re.VERBOSE)


def _assert_regex_safe(pattern: re.Pattern[str], raw: str, name: str) -> None:
    import time

    if _NESTED_QUANT.search(raw):
        raise ConfigError(
            f"{name}: {raw!r} enthaelt verschachtelte Quantoren (z. B. (a+)+) und kann die Aufnahme durch "
            "katastrophales Backtracking blockieren. Bitte vereinfachen.")
    for probe in _REDOS_PROBES:
        start = time.perf_counter()
        pattern.search(probe)
        if time.perf_counter() - start > _REDOS_BUDGET_S:
            raise ConfigError(
                f"{name}: regulaerer Ausdruck {raw!r} ist zu langsam (katastrophales Backtracking) und wuerde "
                "die Aufnahme blockieren. Bitte vereinfachen (z. B. verschachtelte Quantoren wie (a+)+ vermeiden).")


def _compile_all(patterns: list[str], name: str) -> list[re.Pattern[str]]:
    key = tuple(patterns)
    cached = _regex_cache.get(key)
    if cached is not None:
        return cached
    compiled = []
    for p in patterns:
        try:
            pat = re.compile(p, re.IGNORECASE)
        except re.error as e:
            raise ConfigError(f"{name}: ungueltiger regulaerer Ausdruck {p!r}: {e}") from e
        _assert_regex_safe(pat, p, name)
        compiled.append(pat)
    _regex_cache[key] = compiled
    return compiled


def expand_path(value: str) -> str:
    """Expandiert %VAR%, $VAR und ~ und normalisiert Trenner."""
    expanded = os.path.expandvars(os.path.expanduser(value))
    return os.path.normpath(expanded)


def data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(expand_path(override))
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return data_dir() / "config.yaml"


def key_path() -> Path:
    return data_dir() / "key.bin"


def logs_dir() -> Path:
    return data_dir() / "logs"


def is_first_run() -> bool:
    return not config_path().exists()


FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x2000


def ensure_data_dir(path: Path | None = None) -> Path:
    """Legt den Datenordner an und nimmt ihn aus der Windows-Suchindexierung heraus."""
    d = path or data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    if sys.platform == "win32":
        try:
            k32 = ctypes.windll.kernel32
            attrs = k32.GetFileAttributesW(str(d))
            if attrs != 0xFFFFFFFF and not (attrs & FILE_ATTRIBUTE_NOT_CONTENT_INDEXED):
                k32.SetFileAttributesW(str(d), attrs | FILE_ATTRIBUTE_NOT_CONTENT_INDEXED)
        except Exception:  # pragma: no cover - rein kosmetisch
            log.debug("SetFileAttributesW fehlgeschlagen", exc_info=True)
    return d


def load_config(path: Path | None = None) -> Config:
    """Laedt die Konfiguration; fehlende Datei => Defaults. Unbekannte Schluessel werden ignoriert."""
    p = path or config_path()
    cfg = Config()
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"config.yaml ist kein gueltiges YAML: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError("config.yaml muss ein Mapping (Schluessel: Wert) enthalten")
        raw = _migrate_plugin_flags(raw)
        known = {f.name for f in fields(Config)}
        for k, v in raw.items():
            if k in known:
                setattr(cfg, k, v)
            else:
                log.warning("Unbekannter Konfigurationsschluessel ignoriert: %s", k)
    return cfg.validate()


# Bis Oktober 2026 waren die Ereignisquellen fest eingebaut und per Schalter an/aus. Vorgaben damals:
# Outlook an, Teams und Dawarich aus. Wer so eine Konfiguration mitbringt, behaelt genau das, was lief.
_LEGACY_PLUGIN_FLAGS = (("outlook", "outlook_enabled", True),
                        ("teams", "teams_enabled", False),
                        ("dawarich", "dawarich_enabled", False))


def _migrate_plugin_flags(raw: dict) -> dict:
    """Alte Schalter (outlook_enabled ...) -> installed_plugins. Nur wenn installed_plugins noch fehlt."""
    raw = dict(raw)
    flags = {key: raw.pop(key) for _, key, _ in _LEGACY_PLUGIN_FLAGS if key in raw}
    if "installed_plugins" not in raw:
        raw["installed_plugins"] = [pid for pid, key, default in _LEGACY_PLUGIN_FLAGS
                                    if flags.get(key, default) is True]
        if raw["installed_plugins"]:
            log.info("Konfiguration migriert: aktive Quellen werden zu Plugins (%s)",
                     ", ".join(raw["installed_plugins"]))
    return raw


def save_config(cfg: Config, path: Path | None = None) -> Path:
    cfg.validate()
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Zeitspur Konfiguration. Aenderungen werden beim naechsten Start des Dienstes wirksam.\n"
        "# Zeiten in Sekunden/Minuten/Tagen, Regex-Listen sind case-insensitive.\n"
    )
    body = yaml.safe_dump(cfg.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(header + body, encoding="utf-8")
    os.replace(tmp, p)
    return p
