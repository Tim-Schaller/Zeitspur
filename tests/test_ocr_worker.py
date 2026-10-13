import threading
import time

from zeitspur import ocr_worker
from zeitspur.ocr import OcrError, OcrResult
from zeitspur.ocr_worker import OcrWorker
from zeitspur.storage import OCR_DONE, OCR_FAILED
from tests.helpers import make_entry, make_webp

T0 = 1_757_500_000_000


class FakeEngine:
    def __init__(self, text="Erkannter Text", fail=False, delay=0.0):
        self.text, self.fail, self.delay = text, fail, delay
        self.calls: list[bytes] = []
        self.killed = False

    def recognize(self, png: bytes) -> OcrResult:
        self.calls.append(png)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OcrError("kaputt")
        return OcrResult(self.text, 90.0, len(self.text.split()))

    def kill(self):
        self.killed = True


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def run_worker(storage, engine, **kwargs):
    stop = threading.Event()
    worker = OcrWorker(engine, storage, stop, **kwargs)
    worker.start()
    return worker, stop


def test_submitted_job_is_processed(storage):
    engine = FakeEngine("Notizen zum Serverumzug")
    worker, stop = run_worker(storage, engine, maxsize=5, backlog_enabled=False)
    try:
        eid = make_entry(storage, T0)
        assert worker.submit(eid, b"png-bytes")
        assert wait_for(lambda: storage.get_entry(eid)["ocr_status"] == OCR_DONE)
        row = storage.get_entry(eid)
        assert row["ocr_text"] == "Notizen zum Serverumzug" and row["ocr_conf"] == 90.0
        assert [r["id"] for r in storage.search("serverumzug")] == [eid]
        assert worker.processed == 1 and engine.calls == [b"png-bytes"]
    finally:
        stop.set()
        worker.join(3)
    assert not worker.is_alive()


def test_backlog_is_recovered_from_stored_image(storage, monkeypatch):
    monkeypatch.setattr(ocr_worker, "BACKLOG_INTERVAL", 0.0)
    monkeypatch.setattr(ocr_worker, "BACKLOG_MIN_AGE_MS", 0)
    engine = FakeEngine("aus dem Backlog")
    eid = make_entry(storage, T0, webp=make_webp())
    worker, stop = run_worker(storage, engine, maxsize=5, backlog_enabled=True)
    try:
        assert wait_for(lambda: storage.get_entry(eid)["ocr_status"] == OCR_DONE)
        assert storage.get_entry(eid)["ocr_text"] == "aus dem Backlog"
        assert engine.calls and engine.calls[0][:8] == b"\x89PNG\r\n\x1a\n"  # aus WebP dekodiert
    finally:
        stop.set()
        worker.join(3)


def test_failure_marks_entry_failed(storage):
    engine = FakeEngine(fail=True)
    worker, stop = run_worker(storage, engine, maxsize=5, backlog_enabled=False)
    try:
        eid = make_entry(storage, T0)
        worker.submit(eid, b"x")
        assert wait_for(lambda: storage.get_entry(eid)["ocr_status"] == OCR_FAILED)
        assert worker.failed == 1 and worker.processed == 0
    finally:
        stop.set()
        worker.join(3)


def test_full_queue_drops_and_stop_kills(storage):
    engine = FakeEngine()
    stop = threading.Event()
    worker = OcrWorker(engine, storage, stop, maxsize=1, backlog_enabled=False)  # nicht gestartet
    assert worker.submit(1, b"a")
    assert not worker.submit(2, b"b")
    assert worker.dropped == 1 and worker.pending() == 1
    worker.stop()
    assert engine.killed


def test_throttle_pauses_between_jobs(storage):
    ids = [make_entry(storage, ts_start=T0 + i) for i in range(2)]
    engine = FakeEngine(delay=0.2)
    worker, stop = run_worker(storage, engine, maxsize=5, backlog_enabled=False, throttle=2.0)
    started = time.monotonic()
    try:
        for i in ids:
            assert worker.submit(i, b"png")
        assert wait_for(lambda: worker.processed == 2)
        # 2 x 0,2 s Arbeit + Pause von 2 x 0,4 s nach dem ersten Job (die zweite folgt erst danach)
        assert time.monotonic() - started >= 0.2 + 0.4 + 0.2
    finally:
        stop.set()


def test_pause_on_battery_holds_jobs_back(storage, monkeypatch):
    on_battery = {"value": True}
    monkeypatch.setattr(ocr_worker.winutil, "on_battery", lambda: on_battery["value"])
    monkeypatch.setattr(ocr_worker, "BATTERY_CHECK_INTERVAL", 0.05)
    entry_id = make_entry(storage, ts_start=T0)
    worker, stop = run_worker(storage, FakeEngine(), maxsize=5, backlog_enabled=False, pause_on_battery=True)
    try:
        assert worker.submit(entry_id, b"png")
        time.sleep(0.4)
        assert worker.processed == 0
        on_battery["value"] = False
        assert wait_for(lambda: worker.processed == 1)
    finally:
        stop.set()
