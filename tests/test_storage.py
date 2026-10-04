import glob
import os
from datetime import datetime, timedelta

import pytest

from zeitspur import timeutil
from zeitspur.storage import (OCR_DONE, OCR_FAILED, OCR_PENDING, SCHEMA_VERSION, ReadOnlyStorage,
                                 Storage, build_fts_query)
from tests.helpers import FAKE_WEBP, make_entry, make_webp

T0 = timeutil.to_ms(datetime(2026, 9, 8, 9, 0, 0))
MIN = 60_000


def test_insert_and_get(storage):
    eid = make_entry(storage, T0, T0 + 5000, process_name="outlook.exe", window_title="Posteingang - Outlook",
                     exe_path=r"C:\Programme\outlook.exe", webp=FAKE_WEBP)
    row = storage.get_entry(eid)
    assert row["id"] == eid and row["ts_start"] == T0 and row["ts_end"] == T0 + 5000
    assert row["process_name"] == "outlook.exe" and row["window_title"] == "Posteingang - Outlook"
    assert row["exe_path"].endswith("outlook.exe")
    assert row["width"] == 1920 and row["height"] == 1200
    assert row["ocr_status"] == OCR_PENDING and row["ocr_text"] is None
    assert storage.get_image(eid) == FAKE_WEBP
    assert storage.get_entry(999) is None and storage.get_image(999) is None


def test_extend_entries_never_shrinks(storage):
    a = make_entry(storage, T0, T0 + 5000)
    b = make_entry(storage, T0 + MIN, T0 + MIN + 5000)
    storage.extend_entries({a: T0 + 30_000, b: T0 + MIN + 1000})
    assert storage.get_entry(a)["ts_end"] == T0 + 30_000
    assert storage.get_entry(b)["ts_end"] == T0 + MIN + 5000  # kleinerer Wert wird ignoriert
    storage.extend_entry(a, T0 + 40_000)
    assert storage.get_entry(a)["ts_end"] == T0 + 40_000


@pytest.mark.parametrize("text,expected", [
    ("Rechnung", '"Rechnung"*'),
    ("Rechnung Müller", '"Rechnung"* "Müller"*'),
    ('"Serverumzug Meeting" kalender', '"Serverumzug Meeting" "kalender"*'),
    ("a OR b", '"a"* OR "b"*'),
    ("OR", None),
    ("", None),
    (None, None),
    ("foo*", '"foo"*'),
    ('bad"quote', '"badquote"*'),
    ("NOT allein", '"allein"*'),
])
def test_build_fts_query(text, expected):
    assert build_fts_query(text) == expected


def test_search_prefix_phrase_and_diacritics(storage):
    hit = make_entry(storage, T0, ocr_text="Rechnungsnummer 4711 Müller Serverumzug", window_title="Word")
    make_entry(storage, T0 + MIN, ocr_text="Wetterbericht Berlin", window_title="Firefox")
    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert ids(storage.search("rechnung")) == [hit]        # Praefix
    assert ids(storage.search("muller")) == [hit]          # Diakritika-unabhaengig
    assert ids(storage.search("Müller")) == [hit]
    assert ids(storage.search('"4711 Müller"')) == [hit]  # Phrase
    assert ids(storage.search('"Müller 4711"')) == []      # falsche Reihenfolge
    assert ids(storage.search("kaffee")) == []
    snippet = storage.search("rechnung")[0]["snippet"]
    assert "Rechnungsnummer" in snippet  # STX/ETX-Marker


def test_search_matches_window_title(storage):
    eid = make_entry(storage, T0, window_title="Posteingang - Outlook")
    rows = storage.search("posteingang")
    assert [r["id"] for r in rows] == [eid]
    assert rows[0]["window_title"] == "Posteingang - Outlook"


def test_search_time_filter_and_order(storage):
    early = make_entry(storage, T0, ocr_text="alpha projekt")
    late = make_entry(storage, T0 + 2 * MIN, ocr_text="alpha projekt zwei")
    assert {r["id"] for r in storage.search("alpha")} == {early, late}
    assert [r["id"] for r in storage.search("alpha", from_ms=T0 + MIN)] == [late]
    assert [r["id"] for r in storage.search("alpha", to_ms=T0 + MIN)] == [early]
    assert [r["id"] for r in storage.search("alpha", order="time")] == [late, early]
    assert len(storage.search("alpha", limit=1)) == 1


def test_search_invalid_input_returns_empty(storage):
    make_entry(storage, T0, ocr_text="text")
    assert storage.search("") == []
    assert storage.search("OR OR") == []
    assert storage.search('"""') == []


def test_entries_between_and_around(storage):
    a = make_entry(storage, T0, T0 + 5000)
    b = make_entry(storage, T0 + 10 * MIN, T0 + 10 * MIN + 5000)
    c = make_entry(storage, T0 + 20 * MIN, T0 + 25 * MIN)  # langer Block
    between = storage.entries_between(T0 + 5 * MIN, T0 + 15 * MIN)
    assert [r["id"] for r in between] == [b]
    assert "ocr_text" not in between[0]
    assert [r["id"] for r in storage.entries_between(T0 + 22 * MIN, T0 + 23 * MIN)] == [c]  # ueberlappend
    around = storage.entries_around(T0 + 10 * MIN, 60_000)
    assert [r["id"] for r in around] == [b] and "ocr_text" in around[0]
    assert [r["id"] for r in storage.entries_between(T0 - MIN, T0 + 30 * MIN)] == [a, b, c]
    assert storage.count_between(T0, T0 + 15 * MIN) == 2


def test_neighbor_ids(storage):
    a = make_entry(storage, T0)
    b = make_entry(storage, T0 + MIN)
    c = make_entry(storage, T0 + 2 * MIN)
    assert storage.neighbor_ids(b) == (a, c)
    assert storage.neighbor_ids(a) == (None, b)
    assert storage.neighbor_ids(c) == (b, None)
    assert storage.neighbor_ids(999) == (None, None)


def test_app_stats_and_title_stats(storage):
    make_entry(storage, T0, T0 + 10 * MIN, process_name="code.exe", window_title="main.py")
    make_entry(storage, T0 + 10 * MIN, T0 + 15 * MIN, process_name="code.exe", window_title="test.py")
    make_entry(storage, T0 + 15 * MIN, T0 + 17 * MIN, process_name="outlook.exe", window_title="Posteingang")
    stats = storage.app_stats(T0, T0 + 60 * MIN)
    assert [s["process_name"] for s in stats] == ["code.exe", "outlook.exe"]
    assert stats[0]["total_ms"] == 15 * MIN and stats[0]["entries"] == 2 and stats[0]["titles"] == 2
    assert stats[1]["total_ms"] == 2 * MIN
    # Beschneidung auf den Zeitraum
    clipped = storage.app_stats(T0 + 5 * MIN, T0 + 12 * MIN)
    assert clipped[0]["total_ms"] == 7 * MIN
    titles = storage.title_stats(T0, T0 + 60 * MIN, "code.exe")
    assert [t["window_title"] for t in titles] == ["main.py", "test.py"]


def test_list_days(storage):
    day1 = datetime(2026, 9, 8, 23, 30)
    day2 = day1 + timedelta(hours=2)  # 9.9. 01:30 lokal
    make_entry(storage, timeutil.to_ms(day1))
    make_entry(storage, timeutil.to_ms(day2))
    make_entry(storage, timeutil.to_ms(day2) + 1000)
    assert storage.list_days() == ["2026-09-08", "2026-09-09"]


def test_pending_ocr_and_fts_update(storage):
    eid = make_entry(storage, T0, window_title="Editor")
    assert storage.pending_ocr_ids() == [eid]
    assert storage.search("serverumzug") == []
    storage.set_ocr_result(eid, "Notizen zum Serverumzug", 88.5)
    assert storage.pending_ocr_ids() == []
    row = storage.get_entry(eid)
    assert row["ocr_status"] == OCR_DONE and row["ocr_conf"] == 88.5
    assert [r["id"] for r in storage.search("serverumzug")] == [eid]
    other = make_entry(storage, T0 + MIN)
    storage.mark_ocr_failed(other)
    assert storage.get_entry(other)["ocr_status"] == OCR_FAILED
    assert storage.pending_ocr_ids() == []
    assert storage.reset_failed_ocr() == 1
    assert storage.pending_ocr_ids() == [other]
    assert storage.stats()["pending_ocr"] == 1


def test_delete_entry_cascades(storage):
    eid = make_entry(storage, T0, ocr_text="loeschen bitte")
    assert storage.delete_entry(eid) == 1
    assert storage.get_entry(eid) is None
    assert storage.get_image(eid) is None
    assert storage.search("loeschen") == []
    assert storage.delete_entry(eid) == 0


def test_delete_older_than_in_batches(storage):
    ids = [make_entry(storage, T0 + i * MIN) for i in range(10)]
    cutoff = T0 + 7 * MIN  # 7 Eintraege aelter
    deleted = 0
    while True:
        n = storage.delete_older_than(cutoff, batch=3)
        if n == 0:
            break
        assert n <= 3
        deleted += n
    assert deleted == 7
    remaining = [r["id"] for r in storage.entries_between(T0 - MIN, T0 + 60 * MIN)]
    assert remaining == ids[7:]
    assert storage.delete_oldest(2) == 2
    assert storage.stats()["entries"] == 1


def test_delete_between(storage):
    a = make_entry(storage, T0, T0 + 5000)
    b = make_entry(storage, T0 + 5 * MIN, T0 + 12 * MIN)   # laeuft in den Bereich hinein
    c = make_entry(storage, T0 + 11 * MIN, T0 + 11 * MIN + 5000)
    d = make_entry(storage, T0 + 20 * MIN)
    assert storage.delete_between(T0 + 10 * MIN, T0 + 15 * MIN) == 2
    assert [r["id"] for r in storage.entries_between(T0 - MIN, T0 + 60 * MIN)] == [a, d]
    assert b not in (a, d) and c not in (a, d)


def test_incremental_vacuum_shrinks_file(tmp_path, key):
    s = Storage(tmp_path / "big.db", key)
    blob = os.urandom(200_000)
    ids = [make_entry(s, T0 + i * 1000, webp=blob) for i in range(60)]
    s.checkpoint("TRUNCATE")
    size_full = (tmp_path / "big.db").stat().st_size
    assert size_full > 10_000_000
    for eid in ids[:50]:
        s.delete_entry(eid)
    assert s.stats()["freelist_count"] > 1000
    freed = s.incremental_vacuum()
    assert freed > 1000
    s.checkpoint("TRUNCATE")
    size_after = (tmp_path / "big.db").stat().st_size
    assert size_after < size_full / 2
    assert s.stats()["freelist_count"] == 0
    s.close()


def test_no_plaintext_temp_files_during_heavy_queries(storage):
    """temp_store=MEMORY: SQLite darf keine etilqs_*-Sortierdateien in %TEMP% anlegen."""
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    assert tmp, "TEMP nicht gesetzt"
    before = set(glob.glob(os.path.join(tmp, "etilqs_*")))
    for i in range(300):
        make_entry(storage, T0 + i * 1000, process_name=f"app{i % 7}.exe", window_title=f"Fenster {i}",
                   ocr_text=" ".join(os.urandom(6).hex() for _ in range(80)))
    with storage._lock:
        storage._con.execute("PRAGMA cache_size = -64")  # winziger Cache -> wuerde ohne MEMORY auslagern
    storage.app_stats(T0 - MIN, T0 + 60 * MIN)
    storage.search("a", limit=500)
    storage._rows("SELECT window_title, group_concat(ocr_text) FROM entries GROUP BY window_title ORDER BY 2 DESC")
    after = set(glob.glob(os.path.join(tmp, "etilqs_*")))
    assert after - before == set()


def test_meta_and_stats(storage):
    assert storage.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert storage.get_meta("missing", "x") == "x"
    storage.set_meta("last_cleanup", "123")
    storage.set_meta("last_cleanup", "456")
    assert storage.get_meta("last_cleanup") == "456"
    st = storage.stats()
    assert st["entries"] == 0 and st["oldest_ms"] is None
    make_entry(storage, T0, T0 + 5000)
    st = storage.stats()
    assert st["entries"] == 1 and st["oldest_ms"] == T0 and st["newest_ms"] == T0 + 5000
    assert st["db_size_bytes"] > 0 and st["page_size"] in (4096, 8192)


def test_readonly_storage_sees_committed_data(tmp_path, key):
    db = tmp_path / "shared.db"
    writer = Storage(db, key)
    reader = ReadOnlyStorage(db, key)
    eid = make_entry(writer, T0, ocr_text="parallel lesen", webp=make_webp())
    assert reader.get_entry(eid)["ocr_text"] == "parallel lesen"
    assert reader.get_image(eid) == writer.get_image(eid)
    assert [r["id"] for r in reader.search("parallel")] == [eid]
    assert reader.stats()["entries"] == 1
    reader.close()
    writer.close()


def test_schema_version_newer_than_program_raises(tmp_path, key):
    s = Storage(tmp_path / "new.db", key)
    s.set_meta("schema_version", "99")
    s.close()
    with pytest.raises(RuntimeError):
        Storage(tmp_path / "new.db", key)


def test_search_score_is_bm25_not_zero(storage):
    strong = make_entry(storage, T0, ocr_text="Serverumzug Serverumzug Serverumzug Planung", window_title="Notizen")
    weak = make_entry(storage, T0 + MIN, ocr_text="einmal Serverumzug in einem langen Text " + "wort " * 80, window_title="Doku")
    rows = storage.search("serverumzug")
    assert [r["id"] for r in rows] == [strong, weak]
    assert all(r["score"] < 0 for r in rows)  # bm25 liefert negative Werte, bester Treffer am kleinsten
    assert rows[0]["score"] < rows[1]["score"]


def test_transaction_refused_after_close(tmp_path, key):
    from zeitspur.storage import Storage, StorageClosed
    s = Storage(tmp_path / "closed.db", key)
    s.close()
    with pytest.raises(StorageClosed):
        with s.transaction():
            pass  # Worker-Thread schreibt nach dem Herunterfahren -> sauber abgewiesen, kein use-after-close


def test_migration_ergaenzt_ocr_attempts_in_alter_datenbank(tmp_path, key):
    """Eine vor v4 angelegte Datenbank darf beim Oeffnen nicht scheitern."""
    from zeitspur import crypto
    from zeitspur.storage import Storage

    pfad = tmp_path / "alt.db"
    con = crypto.open_database(pfad, key)
    con.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, ts_start INTEGER NOT NULL,"
                " ts_end INTEGER NOT NULL, monitor_id INTEGER NOT NULL DEFAULT 1,"
                " process_name TEXT NOT NULL DEFAULT '', window_title TEXT NOT NULL DEFAULT '',"
                " exe_path TEXT, width INTEGER NOT NULL, height INTEGER NOT NULL,"
                " ocr_status INTEGER NOT NULL DEFAULT 0, ocr_text TEXT, ocr_conf REAL,"
                " created_at INTEGER NOT NULL)")
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '3')")
    con.execute("INSERT INTO entries(ts_start, ts_end, width, height, created_at)"
                " VALUES (1, 2, 10, 10, 3)")
    con.close()

    s = Storage(pfad, key)
    try:
        assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert s.get_entry(1)["ocr_attempts"] == 0
        s.mark_ocr_failed(1)
        assert s.get_entry(1)["ocr_attempts"] == 1
    finally:
        s.close()
