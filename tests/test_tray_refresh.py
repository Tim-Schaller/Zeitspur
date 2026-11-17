"""Tray-Menue und Zugangsdaten: Nichts darf auf einen haengenden Menue-Neuaufbau warten (11.11.2026: waehrend der
taeglichen Sicherung hing das Speichern im Plugin-Browser), und gleichzeitiges Speichern darf nicht kollidieren."""
import threading
import time

from zeitspur import credentials
from zeitspur.tray import TrayIcon


class _FakeIcon:
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0
        self.visible = False

    def update_menu(self):
        self.calls += 1
        self.release.wait(10)   # wie ein Menue-Aufbau, der auf die belegte Datenbank wartet

    def stop(self):
        pass


def test_refresh_menu_kehrt_sofort_zurueck_und_baut_spaeter_auf():
    tray = TrayIcon(app=object())
    fake = _FakeIcon()
    tray._icon = fake
    tray._setup(fake)
    try:
        started = time.monotonic()
        for _ in range(5):
            tray.refresh_menu()
        assert time.monotonic() - started < 0.5
        deadline = time.monotonic() + 3
        while fake.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.calls == 1           # mehrere Aufrufe hintereinander: ein Neuaufbau (der noch laeuft)
        fake.release.set()
    finally:
        fake.release.set()
        tray.stop()


def test_gleichzeitiges_speichern_der_zugangsdaten_kollidiert_nicht():
    barrier = threading.Barrier(4)
    errors = []

    def save(i):
        try:
            barrier.wait()
            credentials.save("langdock", {"token": f"token-{i}-0123456789abcdef"})
        except Exception as e:   # pragma: no cover - genau das soll nicht passieren
            errors.append(e)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert credentials.load("langdock")["token"].startswith("token-")
    assert not list(credentials.path("langdock").parent.glob("*.tmp"))
