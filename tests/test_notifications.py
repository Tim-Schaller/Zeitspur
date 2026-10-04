"""Windows-Benachrichtigungen: Toast-XML, App-Namen, Mitschreiben mit Filtern, Lesen der Windows-Datenbank."""
import json
import sqlite3

from zeitspur import notifications as nt

T = 1_790_000_000_000
TOAST = ('<toast launch="x"><visual><binding template="ToastGeneric"><text>Erika Musterfrau</text>'
         '<text>Hallo, wie   geht es?</text><text placement="attribution">via Teams</text></binding></visual></toast>')


def test_toast_texte():
    assert nt.parse_toast_texts(TOAST) == ("Erika Musterfrau", "Hallo, wie geht es?", "via Teams")
    assert nt.parse_toast_texts(TOAST.encode("utf-8"))[0] == "Erika Musterfrau"
    assert nt.parse_toast_texts("<kaputt") == ()


def test_app_namen_und_systemmeldungen():
    assert nt.app_name("MSTeams_8wekyb3d8bbwe!MSTeams") == ("Teams", False)
    assert nt.app_name("5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App") == ("WhatsApp", False)
    assert nt.app_name("Windows.SystemToast.SecurityAndMaintenance")[1] is True
    assert nt.app_name("Contoso.Chat_abc!App") == ("Chat", False)


def _toast(nid, ts, app="MSTeams_8wekyb3d8bbwe!MSTeams", texts=("Erika Musterfrau", "Hallo")):
    return nt.Toast(nid, ts, app, texts)


SETTINGS = {"store_text": True, "ignore_system": True, "ignore_apps": [], "only_apps": []}


def test_neue_mitteilungen_einmal_mitschreiben(storage):
    toasts = [_toast(1, T), _toast(2, T + 1000, "Windows.SystemToast.Update", ("Neustart nötig",))]
    rec = nt.NotificationRecorder(read=lambda: toasts)
    assert rec.observe(storage, T + 5000, SETTINGS) == 1                    # Systemmeldung bleibt aussen vor
    assert rec.observe(storage, T + 6000, SETTINGS) == 0                    # erst nach 30 s wieder nachsehen
    toasts.append(_toast(3, T + 40_000))
    assert rec.observe(storage, T + 40_000, SETTINGS) == 1
    events = storage.events_between(T - 1, T + 10 ** 7)
    assert [e["subject"] for e in events] == ["Teams: Erika Musterfrau", "Teams: Erika Musterfrau"]
    assert json.loads(events[0]["extra"]) == {"app": "Teams", "text": "Hallo"}
    assert events[0]["category"] == "notification" and events[0]["ts_end"] == events[0]["ts_start"]
    # Neustart: schon Gespeichertes nicht noch einmal
    assert nt.NotificationRecorder(read=lambda: toasts).observe(storage, T + 100_000, SETTINGS) == 0


def test_pausiert_wird_nichts_gelesen(storage):
    def read():
        raise AssertionError("darf nicht gelesen werden")
    assert nt.NotificationRecorder(read=read).observe(storage, T, SETTINGS, titles_allowed=False) == 0


def test_filter_und_ohne_textvorschau(storage):
    toasts = [_toast(1, T), _toast(2, T + 1, "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", ("Max", "Geheim"))]
    settings = {**SETTINGS, "ignore_apps": ["whatsapp"], "store_text": False}
    assert nt.NotificationRecorder(read=lambda: toasts).observe(storage, T + 5000, settings) == 1
    [ev] = storage.events_between(T - 1, T + 10 ** 7)
    assert json.loads(ev["extra"]) == {"app": "Teams"}                      # keine Textvorschau
    only = {**SETTINGS, "only_apps": ["WhatsApp"]}
    assert nt.NotificationRecorder(read=lambda: toasts).observe(storage, T + 5000, only) == 1


def test_windows_datenbank_lesen(tmp_path):
    db = tmp_path / "wpndatabase.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT)")
    con.execute("CREATE TABLE Notification (Id INTEGER, HandlerId INTEGER, Type TEXT, Payload BLOB, ArrivalTime INTEGER)")
    con.execute("INSERT INTO NotificationHandler VALUES (7, 'MSTeams_8wekyb3d8bbwe!MSTeams')")
    filetime = (T * 10_000) + 116_444_736_000_000_000
    con.execute("INSERT INTO Notification VALUES (1, 7, 'toast', ?, ?)", (TOAST.encode("utf-8"), filetime))
    con.execute("INSERT INTO Notification VALUES (2, 7, 'badge', ?, ?)", (b"<badge/>", filetime))
    con.commit()
    con.close()
    [toast] = nt.read_toasts(db)
    assert toast.arrival_ms == T and toast.app_id.startswith("MSTeams") and toast.texts[0] == "Erika Musterfrau"
    assert "1 Mitteilungen" in nt.describe_current(read=lambda: [toast])
