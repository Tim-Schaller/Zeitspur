"""Teams-/Graph-Abbildung (rein) und DPAPI-Zugangsdatenspeicher."""
import json
import sys
from datetime import datetime, timezone

import pytest

from zeitspur import teams
from zeitspur.teams import UserMatcher, _display_names, _iso_to_ms, _name_tokens, build_call_event


def test_iso_to_ms_utc_and_fractional():
    ms = _iso_to_ms("2026-09-09T10:00:00Z")
    assert datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour == 10
    # ueberlange Sekundenbruchteile (Graph) duerfen nicht scheitern
    ms2 = _iso_to_ms("2026-09-09T10:00:00.1234567Z")
    assert ms2 is not None
    assert _iso_to_ms(None) is None
    assert _iso_to_ms("kaputt") is None


def test_display_names_dedup_and_limit():
    parts = [{"user": {"displayName": "Anna"}}, {"user": {"displayName": "Tim"}},
             {"user": {"displayName": "Anna"}}, {"identity": {"user": {"displayName": "Bob"}}}, {}]
    assert _display_names(parts) == ["Anna", "Tim", "Bob"]
    assert _display_names(parts, limit=1) == ["Anna"]
    assert _display_names(None) == []


def test_build_call_event():
    record = {
        "id": "call-1", "type": "peerToPeer", "modalities": ["audio"],
        "startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:12:30Z",
        "organizer": {"user": {"displayName": "Anna Müller"}},
        "participants": [{"user": {"displayName": "Anna Müller"}}, {"user": {"displayName": "Tim"}}],
    }
    e = build_call_event(record)
    assert e.category == "call" and e.ext_id == "call-1" and e.location == "Microsoft Teams"
    assert e.subject == "Teams-Anruf mit Anna Müller" and e.organizer == "Anna Müller"
    assert e.attendees == "Anna Müller; Tim"
    assert e.ts_end - e.ts_start == 12 * 60_000 + 30_000
    assert json.loads(e.extra)["type"] == "peerToPeer"


def test_build_call_event_organizer_v2_fallback():
    e = build_call_event({"id": "c3", "startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:05:00Z",
                          "organizer_v2": {"identity": {"user": {"displayName": "Chef"}}}})
    assert e.organizer == "Chef" and e.subject == "Teams-Anruf mit Chef"


def test_build_call_event_without_organizer():
    e = build_call_event({"id": "c2", "startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:01:00Z",
                          "participants": [{"user": {"displayName": "X"}}]})
    assert e.subject.startswith("Teams-Anruf mit X")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")
def test_credentials_roundtrip(tmp_path):
    path = tmp_path / "teams_credentials.bin"
    assert teams.load_credentials(path) is None and not teams.has_credentials(path)
    teams.save_credentials("tenant-123", "client-456", "s3cr3t", path)
    assert teams.has_credentials(path)
    creds = teams.load_credentials(path)
    assert creds == {"tenant_id": "tenant-123", "client_id": "client-456", "client_secret": "s3cr3t"}
    # nicht im Klartext gespeichert
    assert b"s3cr3t" not in path.read_bytes()
    assert teams.clear_credentials(path) is True and not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")
def test_from_stored_none_without_credentials(tmp_path):
    assert teams.TeamsCallRecords.from_stored(tmp_path / "missing.bin") is None


def test_fetch_clamps_window_to_last_30_days(monkeypatch):
    from datetime import date, datetime, timezone
    import re
    captured = {}
    client = teams.TeamsCallRecords({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    client._token = "faketoken"
    def fake_get(url):
        captured["url"] = url
        return {"value": []}
    monkeypatch.setattr(client, "_get", fake_get)
    import urllib.parse
    # Fenster weit in Vergangenheit UND Zukunft -> muss auf [jetzt-30d, jetzt] beschnitten werden
    client.fetch(date.today().replace(day=1), date.today())  # end=heute -> Obergrenze < jetzt+1
    url = urllib.parse.unquote_plus(captured["url"])
    m = re.search(r"ge (\S+Z) and startDateTime lt (\S+Z)", url)
    assert m, url
    lo = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hi = datetime.strptime(m.group(2), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert hi <= now  # nicht in der Zukunft
    assert (now - lo).days <= 30  # innerhalb 30 Tage


def test_fetch_empty_when_window_out_of_range(monkeypatch):
    from datetime import date, timedelta
    client = teams.TeamsCallRecords({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    client._token = "faketoken"
    called = {"n": 0}
    monkeypatch.setattr(client, "_get", lambda url: called.__setitem__("n", called["n"] + 1) or {"value": []})
    # komplett in der Vergangenheit (> 30 Tage) -> gar kein Abruf
    old = date.today() - timedelta(days=90)
    assert client.fetch(old, old) == [] and called["n"] == 0


def test_user_matcher_by_id_and_name():
    m = UserMatcher(user_id="TIM-ID-123", names=["Mustermann, Max"])
    assert m.active()
    assert m.matches({"user": {"id": "tim-id-123", "displayName": "Irgendwer"}})   # Id case-insensitiv
    assert m.matches({"user": {"id": "OTHER", "displayName": "Max Mustermann"}})      # Name, Reihenfolge egal
    assert not m.matches({"user": {"id": "OTHER", "displayName": "Erika Musterfrau"}})
    assert m.matches({"phone": {"id": "OTHER", "displayName": "Mustermann,  Max"}})   # anderer Rollen-Slot
    assert not m.matches(None) and not UserMatcher().active()


def test_name_tokens():
    assert _name_tokens("Mustermann, Max") == _name_tokens("Max Mustermann") == frozenset({"mustermann", "max"})


def test_build_call_event_direction():
    rec = {"id": "c1", "startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:05:00Z",
           "organizer": {"user": {"displayName": "Tim"}}, "participants_v2": [{"identity": {"user": {"displayName": "Bob"}}}]}
    out = build_call_event(rec, direction="outgoing")
    assert "(ausgehend)" in out.subject and "Bob" in out.subject  # Gegenpart statt eigenem Namen
    assert '"direction": "outgoing"' in out.extra
    inc = build_call_event({"id": "c2", "startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:05:00Z",
                            "organizer": {"user": {"displayName": "Chef"}}}, direction="incoming")
    assert "(eingehend)" in inc.subject and "Chef" in inc.subject


def test_identity_label_guid_and_phone():
    from zeitspur.teams import _identity_label, _participant_identity
    assert _identity_label({"user": {"id": "x", "displayName": "Anna"}}) == "Anna"
    # nicht aufloesbarer Nutzer: Graph liefert die Objekt-Id als Anzeigename -> kein echter Name
    guid = "00000000-0000-4000-8000-000000000001"
    assert _identity_label({"user": {"id": guid, "displayName": guid}}) is None
    assert _identity_label({"user": {"id": "abc", "displayName": "abc"}}) is None  # dn == id
    # Telefonnummer als Rueckfall, wenn kein Name hinterlegt ist
    assert _identity_label({"phone": {"id": "+490001234567", "displayName": None}}) == "+490001234567"
    # Teilnehmer-Form {'identity': {...}} wird zur identitySet aufgeloest
    assert _participant_identity({"identity": {"user": {"displayName": "Bob"}}}) == {"user": {"displayName": "Bob"}}
    assert _participant_identity({"user": {"displayName": "Bob"}}) == {"user": {"displayName": "Bob"}}


def test_build_call_event_excludes_self():
    guid = "00000000-0000-4000-8000-000000000001"
    m = UserMatcher(user_id="TIM")
    me = {"identity": {"user": {"id": "TIM", "displayName": "Mustermann, Max"}}}
    org = {"organizer_v2": {"identity": {"user": {"id": "TIM", "displayName": "Mustermann, Max"}}}}
    base = {"startDateTime": "2026-09-09T10:00:00Z", "endDateTime": "2026-09-09T10:05:00Z", **org}
    # eigener Name wird aus dem angezeigten Gegenpart ausgeblendet
    e = build_call_event({**base, "id": "c1", "participants_v2": [me, {"identity": {"user": {"id": "BOB", "displayName": "Musterfrau, Erika"}}}]},
                         direction="outgoing", matcher=m)
    assert e.subject == "Teams-Anruf (ausgehend) mit Musterfrau, Erika" and e.attendees == "Musterfrau, Erika"
    # Gegenpart nicht aufloesbar (displayName == GUID) -> ohne Namen, nie die eigene Id
    e2 = build_call_event({**base, "id": "c2", "participants_v2": [me, {"identity": {"user": {"id": guid, "displayName": guid}}}]},
                          direction="outgoing", matcher=m)
    assert e2.subject == "Teams-Anruf (ausgehend)" and e2.attendees is None
    # PSTN: Gegenpart ist eine Rufnummer
    e3 = build_call_event({**base, "id": "c3", "participants_v2": [{"identity": {"phone": {"id": "+490001234567"}}}, me]},
                          direction="outgoing", matcher=m)
    assert e3.subject == "Teams-Anruf (ausgehend) mit +490001234567"


def _rec(rid, org_id, start="2026-09-09T10:00:00Z", end="2026-09-09T10:05:00Z"):
    return {"id": rid, "startDateTime": start, "endDateTime": end,
            "organizer_v2": {"identity": {"user": {"id": org_id, "displayName": org_id}}}}


def test_fetch_filters_to_user_and_caches(monkeypatch):
    client = teams.TeamsCallRecords({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    client._token = "tok"
    collection = {"value": [_rec("r1", "TIM"), _rec("r2", "BOB"), _rec("r3", "EVE")]}
    monkeypatch.setattr(client, "_get", lambda url: collection)
    expand = {
        "r1": [{"identity": {"user": {"id": "TIM", "displayName": "Tim"}}}, {"identity": {"user": {"id": "BOB", "displayName": "Bob"}}}],
        "r2": [{"identity": {"user": {"id": "BOB"}}}, {"identity": {"user": {"id": "TIM", "displayName": "Tim"}}}],
        "r3": [{"identity": {"user": {"id": "BOB"}}}, {"identity": {"user": {"id": "EVE"}}}],
    }
    calls = []
    monkeypatch.setattr(client, "_expand_participants", lambda ext: calls.append(ext) or expand[ext])
    checked = {}
    matcher = UserMatcher(user_id="TIM")
    events = client.fetch(__import__("datetime").date(2026, 9, 9), __import__("datetime").date(2026, 9, 9),
                          matcher=matcher, checked=checked)
    dirs = {e.ext_id: __import__("json").loads(e.extra)["direction"] for e in events}
    assert dirs == {"r1": "outgoing", "r2": "incoming"}          # r3 (nicht Tim) verworfen
    assert checked == {"r1": True, "r2": True, "r3": False}      # Klassifizierung gecacht
    # zweiter Lauf mit gefuelltem Cache: r3 wird nicht erneut expandiert
    calls.clear()
    client.fetch(__import__("datetime").date(2026, 9, 9), __import__("datetime").date(2026, 9, 9), matcher=matcher, checked=checked)
    assert "r3" not in calls   # r3 aus Cache als False -> keine erneute Abfrage


def test_fetch_without_matcher_returns_all(monkeypatch):
    client = teams.TeamsCallRecords({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    client._token = "tok"
    monkeypatch.setattr(client, "_get", lambda url: {"value": [_rec("r1", "TIM"), _rec("r2", "BOB")]})
    events = client.fetch(__import__("datetime").date(2026, 9, 9), __import__("datetime").date(2026, 9, 9))
    assert len(events) == 2  # ohne Matcher: unveraendert alle (Rueckwaertskompatibilitaet)
