import os
import threading
import time

from zeitspur import timeutil
from zeitspur.cleanup import MaintenanceJob, is_cleanup_due, is_fts_optimize_due, run_cleanup
from zeitspur.config import Config
from zeitspur.storage import OCR_FAILED, OCR_PENDING
from tests.helpers import make_entry

DAY = timeutil.MS_PER_DAY
NOW = timeutil.to_ms(__import__("datetime").datetime(2026, 9, 10, 12, 0, 0))


def test_retention_deletes_only_old_entries(storage):
    old1 = make_entry(storage, NOW - 20 * DAY, ocr_text="alt eins")
    old2 = make_entry(storage, NOW - 15 * DAY, ocr_text="alt zwei")
    keep1 = make_entry(storage, NOW - 13 * DAY, ocr_text="behalten")
    keep2 = make_entry(storage, NOW - 60_000, ocr_text="frisch")
    cfg = Config(retention_days=14)
    report = run_cleanup(storage, cfg, now_ms=NOW, batch_size=1, pause_s=0)
    assert report.deleted_retention == 2 and report.deleted_size_cap == 0 and not report.aborted
    remaining = [r["id"] for r in storage.entries_between(0, 10 ** 15)]
    assert remaining == [keep1, keep2]
    assert storage.get_entry(old1) is None and storage.get_image(old2) is None
    assert storage.search("alt") == [] and len(storage.search("behalten")) == 1
    assert storage.get_meta("last_cleanup") == str(NOW)
    assert storage.get_meta("bytes_freed_total") is not None
    assert not is_cleanup_due(storage, NOW + DAY - 1)
    assert is_cleanup_due(storage, NOW + DAY)


def test_size_cap_deletes_oldest_first(storage):
    blob = os.urandom(120_000)
    ids = [make_entry(storage, NOW - (30 - i) * 60_000, webp=blob) for i in range(30)]  # ~3.6 MB
    cfg = Config(retention_days=14, max_db_size_gb=1.0 / 1024)  # 1 MB Deckel
    report = run_cleanup(storage, cfg, now_ms=NOW, batch_size=4, pause_s=0)
    assert report.deleted_retention == 0 and report.deleted_size_cap > 0
    remaining = [r["id"] for r in storage.entries_between(0, 10 ** 15)]
    assert remaining and remaining == ids[-len(remaining):]  # nur die neuesten bleiben
    st = storage.stats()
    assert (st["page_count"] - st["freelist_count"]) * st["page_size"] <= 1024 ** 2
    assert report.bytes_after < report.bytes_before


def test_file_shrinks_and_report_counts_bytes(tmp_path, key):
    from zeitspur.storage import Storage

    s = Storage(tmp_path / "shrink.db", key)
    blob = os.urandom(200_000)
    for i in range(40):
        make_entry(s, NOW - 20 * DAY + i * 1000, webp=blob)
    for i in range(5):
        make_entry(s, NOW - i * 1000, webp=blob)
    s.checkpoint("TRUNCATE")
    before = (tmp_path / "shrink.db").stat().st_size
    report = run_cleanup(s, Config(retention_days=14), now_ms=NOW, pause_s=0)
    after = (tmp_path / "shrink.db").stat().st_size
    assert report.deleted_retention == 40 and report.pages_freed > 0
    assert after < before / 2
    assert report.bytes_freed > 0 and report.bytes_before >= before
    s.close()


def test_stop_event_aborts_between_batches(storage):
    for i in range(10):
        make_entry(storage, NOW - 30 * DAY + i * 1000)
    class StopAfterFirstPause(threading.Event):
        def wait(self, timeout=None):  # wird nach jedem Batch aufgerufen -> Abbruch nach dem ersten
            self.set()
            return True

    stop = StopAfterFirstPause()
    report = run_cleanup(storage, Config(retention_days=14), stop, now_ms=NOW, batch_size=3, pause_s=0.001)
    assert report.aborted
    assert report.deleted_retention == 3
    assert storage.get_meta("last_cleanup") is None  # abgebrochene Laeufe gelten nicht als erledigt


def test_maintenance_thread_runs_and_resets_failed_ocr(storage, monkeypatch):
    old = make_entry(storage, NOW - 20 * DAY)
    failed = make_entry(storage, NOW - 1000)
    storage.mark_ocr_failed(failed)
    assert storage.get_entry(failed)["ocr_status"] == OCR_FAILED
    monkeypatch.setattr(timeutil, "now_ms", lambda: NOW)
    stop = threading.Event()
    reports = []
    job = MaintenanceJob(Config(retention_days=14), storage, stop, initial_delay_s=0, check_interval_s=0.2,
                         busy_idle_s=0, on_report=reports.append)  # busy_idle_s=0: nie verschieben
    job.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not reports:
        time.sleep(0.05)
    stop.set()
    job.trigger()
    job.join(3)
    assert not job.is_alive()
    assert reports and reports[0].deleted_retention == 1 and job.runs == 1
    assert storage.get_entry(old) is None
    assert storage.get_entry(failed)["ocr_status"] == OCR_PENDING
    assert storage.get_meta("last_fts_optimize") is not None
    assert not is_fts_optimize_due(storage, NOW + 6 * DAY)
    assert is_fts_optimize_due(storage, NOW + 8 * DAY)


def _mk_storage(tmp_path, key, n=3):
    from zeitspur.storage import Storage
    s = Storage(tmp_path / "zeitspur.db", key)
    for i in range(n):
        s.insert_entry(ts_start=i, ts_end=i + 1, monitor_id=1, process_name="x.exe", window_title="t",
                       exe_path=None, width=1, height=1, webp=b"abc")
    return s


def test_backup_is_readable_with_the_same_key(tmp_path, key):
    """Eine Sicherung nuetzt nur, wenn sie sich spaeter auch oeffnen laesst."""
    from zeitspur.storage import ReadOnlyStorage
    s = _mk_storage(tmp_path, key)
    size = s.backup_to(tmp_path / "backups" / "kopie.db")
    s.close()
    assert size > 0
    r = ReadOnlyStorage(tmp_path / "backups" / "kopie.db", key)
    assert r.stats()["entries"] == 3
    r.close()
    assert not list((tmp_path / "backups").glob("*.tmp"))  # keine halbfertigen Reste


def test_make_backup_rotates_and_respects_limits(tmp_path, key, monkeypatch):
    from zeitspur.cleanup import make_backup
    from zeitspur.config import Config
    s = _mk_storage(tmp_path, key)
    cfg = Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=2)

    # drei "Tage" nacheinander -> nur die juengsten zwei bleiben
    for day in ("20260101", "20260102", "20260103"):
        monkeypatch.setattr("zeitspur.cleanup.time.strftime", lambda f, d=day: d)
        assert make_backup(s, cfg) == f"zeitspur-{day}.db"
    kept = sorted(f.name for f in (tmp_path / "backups").glob("zeitspur-*.db"))
    assert kept == ["zeitspur-20260102.db", "zeitspur-20260103.db"]

    # abgeschaltet und Groessenlimit
    assert make_backup(s, Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=0)) is None
    assert make_backup(s, Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=2,
                                 backup_max_gb=0.000001)) is None
    s.close()


def test_make_backup_survives_a_failure(tmp_path, key, monkeypatch):
    """Scheitert die Sicherung, darf sie den Aufraeumlauf nicht mitreissen."""
    from zeitspur.cleanup import make_backup
    from zeitspur.config import Config
    s = _mk_storage(tmp_path, key)
    monkeypatch.setattr(type(s), "backup_to", lambda *a, **k: (_ for _ in ()).throw(OSError("Platte voll")))
    assert make_backup(s, Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=2)) is None
    s.close()


def test_make_backup_runs_once_per_day(tmp_path, key, monkeypatch):
    """Darf oft aufgerufen werden (der Wartungs-Thread tut das) - schreibt aber nur einmal taeglich."""
    from zeitspur.cleanup import make_backup
    from zeitspur.config import Config
    monkeypatch.setattr("zeitspur.cleanup.time.strftime", lambda f: "20260917")
    s = _mk_storage(tmp_path, key)
    cfg = Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=2)
    assert make_backup(s, cfg) == "zeitspur-20260917.db"
    assert make_backup(s, cfg) is None          # zweiter Aufruf am selben Tag: nichts zu tun
    s.close()
    assert len(list((tmp_path / "backups").glob("zeitspur-*.db"))) == 1


def test_maintenance_backs_up_even_when_cleanup_is_not_due(tmp_path, key, monkeypatch):
    """Nach einem Neustart darf die Sicherung nicht 24 h auf den naechsten Aufraeumlauf warten."""
    import threading

    from zeitspur import cleanup as cleanup_mod
    from zeitspur.config import Config
    s = _mk_storage(tmp_path, key)
    monkeypatch.setattr(cleanup_mod, "is_cleanup_due", lambda storage: False)
    monkeypatch.setattr(cleanup_mod, "is_fts_optimize_due", lambda storage: False)
    job = cleanup_mod.MaintenanceJob(Config(db_path=str(tmp_path / "zeitspur.db"), backup_count=2),
                                     s, threading.Event(), busy_idle_s=0)  # nicht verschieben
    job._check()
    s.close()
    assert list((tmp_path / "backups").glob("zeitspur-*.db")), "trotz uebersprungenem Aufraeumen muss gesichert werden"


def test_wartung_wird_verschoben_solange_der_nutzer_arbeitet(storage, monkeypatch):
    """Die taegliche Sicherung schreibt die ganze Datenbank neu - nicht waehrend der Arbeit."""
    from zeitspur import cleanup as cleanup_mod
    stop = threading.Event()
    job = MaintenanceJob(Config(), storage, stop, busy_idle_s=90, max_defer_s=6 * 3600)

    monkeypatch.setattr(cleanup_mod.winutil, "idle_seconds", lambda: 5.0)
    assert job._postpone_while_busy() is True

    monkeypatch.setattr(cleanup_mod.winutil, "idle_seconds", lambda: 300.0)
    assert job._postpone_while_busy() is False

    # Dauernutzung darf die Wartung nicht ewig blockieren
    job.max_defer_s = 0
    monkeypatch.setattr(cleanup_mod.winutil, "idle_seconds", lambda: 5.0)
    assert job._postpone_while_busy() is True      # erste Verschiebung merkt sich den Zeitpunkt
    assert job._postpone_while_busy() is False     # danach laeuft sie trotzdem


def test_reset_failed_ocr_beachtet_die_versuchsgrenze(storage):
    """Ein Bild, an dem die Texterkennung immer scheitert, darf nicht ewig Rechenzeit kosten."""
    entry = make_entry(storage, NOW - 1000)
    for versuch in (1, 2, 3):
        storage.mark_ocr_failed(entry)
        assert storage.get_entry(entry)["ocr_attempts"] == versuch
        erwartet = 1 if versuch < 3 else 0
        assert storage.reset_failed_ocr(max_attempts=3) == erwartet
    assert storage.get_entry(entry)["ocr_status"] == OCR_FAILED
