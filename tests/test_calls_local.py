"""Gespraeche in allen Apps: App-Erkennung, Mitschreiben laufender und beendeter Gespraeche, Video, Filter."""
import json

from zeitspur import calls_local as cl
from zeitspur.teams_local import MicUsage

T = 1_790_000_000_000
ZOOM = r"C:#Program Files#Zoom#bin#Zoom.exe"
EDGE = r"C:#Program Files (x86)#Microsoft#Edge#Application#msedge.exe"
STARFACE = r"C:#Users#max#AppData#Local#Programs#STARFACE#STARFACE App.exe"
PLAUD = r"C:#Program Files#Plaud#Plaud.exe"


class Quelle:
    """Gibt vor, was Windows je App als letzte Mikrofon-/Kameranutzung meldet."""

    def __init__(self):
        self.mic: dict = {}
        self.cam: dict = {}

    def __call__(self, capability):
        return dict(self.mic if capability == "microphone" else self.cam)


def _events(storage):
    return storage.events_between(T - 1, T + 10 ** 8)


def test_apps_werden_erkannt():
    assert cl.identify(ZOOM) == cl.App("Zoom", "call")
    assert cl.identify(STARFACE) == cl.App("STARFACE", "call")
    assert cl.identify("5319275A.WhatsAppDesktop_cv1g1gvanyjgm").name == "WhatsApp"
    assert cl.identify("MSTeams_8wekyb3d8bbwe").name == "Microsoft Teams"
    assert cl.identify(EDGE).kind == "browser"
    assert cl.identify("windows.immersivecontrolpanel_cw5n1h2txyewy").kind == "system"
    assert cl.identify(r"C:#Tools#Diktat.exe") == cl.App("Diktat", "other")
    assert cl.identify("Contoso.SuperApp_abc123").name == "SuperApp"


def test_browser_titel_ohne_browsernamen():
    edge = "Meet – abc-defg-hij und 3 weitere Seiten - Persönlich – Microsoft​ Edge"
    assert cl.clean_browser_title(edge) == "Meet – abc-defg-hij"
    assert cl.clean_browser_title("Zoom Meeting - Google Chrome") == "Zoom Meeting"
    assert cl.clean_browser_title("Jitsi Meet — Mozilla Firefox") == "Jitsi Meet"


def test_laufendes_gespraech_wird_verlaengert_und_einmal_abgeschlossen(storage):
    quelle = Quelle()
    rec = cl.CallsRecorder(read=quelle, read_titles=lambda exe: [])
    quelle.mic[ZOOM] = MicUsage(ZOOM, T, None)
    assert rec.observe(storage, T + 60_000) == 1
    assert rec.observe(storage, T + 120_000) == 1
    [ev] = _events(storage)
    assert ev["subject"] == "Gespräch: Zoom" and ev["ts_end"] == T + 120_000
    assert ev["category"] == "conversation" and json.loads(ev["extra"])["in_progress"]
    quelle.mic[ZOOM] = MicUsage(ZOOM, T, T + 150_000)
    assert rec.observe(storage, T + 200_000) == 1
    assert rec.observe(storage, T + 260_000) == 0                     # abgeschlossen: nicht mehr anfassen
    [ev] = _events(storage)
    assert ev["ts_end"] == T + 150_000 and not json.loads(ev["extra"])["in_progress"]
    assert cl.CallsRecorder(read=quelle).observe(storage, T + 300_000) == 0   # auch nach einem Neustart nicht


def test_kamera_macht_ein_videogespraech(storage):
    quelle = Quelle()
    quelle.mic[ZOOM] = MicUsage(ZOOM, T, T + 600_000)
    quelle.cam[ZOOM] = MicUsage(ZOOM, T + 10_000, T + 500_000)
    cl.CallsRecorder(read=quelle).observe(storage, T + 700_000)
    [ev] = _events(storage)
    assert ev["subject"] == "Gespräch: Zoom mit Video" and json.loads(ev["extra"])["video"] is True


def test_filter_kurz_system_auslassen_und_andere_arten(storage):
    quelle = Quelle()
    quelle.mic = {
        ZOOM: MicUsage(ZOOM, T, T + 3_000),                           # zu kurz (Geraetetest)
        "windows.immersivecontrolpanel_cw5n1h2txyewy": MicUsage("x", T, T + 60_000),   # Systemeinstellungen
        PLAUD: MicUsage(PLAUD, T, T + 60_000),
        STARFACE: MicUsage(STARFACE, T, T + 60_000),
    }
    assert cl.CallsRecorder(read=quelle).observe(storage, T + 100_000, ignore=["starface"], include_other=False) == 0
    assert cl.CallsRecorder(read=quelle).observe(storage, T + 100_000) == 2
    assert sorted(e["subject"] for e in _events(storage)) == ["Aufnahme: Plaud", "Gespräch: STARFACE"]


def test_teams_bleibt_beim_eigenen_plugin(storage):
    quelle = Quelle()
    quelle.mic["MSTeams_8wekyb3d8bbwe"] = MicUsage("MSTeams_8wekyb3d8bbwe", T, T + 60_000)
    assert cl.CallsRecorder(read=quelle).observe(storage, T + 90_000, skip_teams=True) == 0
    assert cl.CallsRecorder(read=quelle).observe(storage, T + 90_000) == 1


def test_meeting_im_browser_nur_mit_eindeutigem_titel(storage):
    quelle = Quelle()
    quelle.mic[EDGE] = MicUsage(EDGE, T, None)
    titel = ["Posteingang - Outlook - Persönlich – Microsoft​ Edge",
             "Meet – abc-defg-hij - Persönlich – Microsoft​ Edge"]
    rec = cl.CallsRecorder(read=quelle, read_titles=lambda exe: titel if exe == "msedge.exe" else [])
    assert rec.observe(storage, T + 60_000, titles_allowed=False) == 1    # pausiert: keine Titel
    assert json.loads(_events(storage)[0]["extra"]).get("window_titles") is None
    rec.observe(storage, T + 90_000)
    [ev] = _events(storage)
    assert ev["subject"] == "Gespräch im Browser (Edge): Meet – abc-defg-hij"
    assert json.loads(ev["extra"])["window_titles"] == ["Meet – abc-defg-hij"]   # der Outlook-Tab nicht


def test_beschreibung_fuer_verbindung_testen():
    quelle = Quelle()
    assert "keine App" in cl.describe_recent(read=quelle)
    quelle.mic[ZOOM] = MicUsage(ZOOM, T, None)
    assert "Zoom (gerade)" in cl.describe_recent(read=quelle)
