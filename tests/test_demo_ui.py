"""tools/demo_ui.py baut die Seiten fuer die README-Screenshots (tools/screenshots.py) - mit erfundenen Daten."""
import importlib.util
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _demo_ui():
    spec = importlib.util.spec_from_file_location("demo_ui_fuer_tests", ROOT / "tools" / "demo_ui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_api_runs_under_the_page_csp():
    """Die Demo-Antworten muessen den Nonce der Seite tragen und vor dem App-Skript stehen - sonst blockiert die
    CSP sie, und alle Screenshots zeigen eine leere Oberflaeche (nach der CSP-Haertung in 0.4.0 so passiert)."""
    page = _demo_ui()._page({"state": {}}, "")
    allowed = re.search(r"script-src 'nonce-([^']+)'", page).group(1)
    tags = re.findall(r"<script[^>]*>", page)
    assert len(tags) >= 2 and all(tag == f'<script nonce="{allowed}">' for tag in tags)
    assert page.index("window.pywebview = { api") < page.rindex("<script")   # vor dem App-Skript


def test_demo_day_tells_a_consistent_location_story():
    """Die Standort-Spur im Screenshot: Autofahrt ins Buero, zu Fuss zum Mittagessen und zurueck, abends heim."""
    rows = _demo_ui().demo_gps(date(2026, 9, 9))
    modes = [json.loads(r["extra"]).get("mode") for r in rows if r["category"] == "track"]
    assert modes == ["car", "walk", "walk", "car"]
    assert any(r["subject"] == "Bistro am Markt" for r in rows if r["category"] == "visit")
