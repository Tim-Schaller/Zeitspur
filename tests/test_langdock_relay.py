"""Langdock-Zugang: Relay-Client gegen ein nachgebautes Relay (wss mit Testzertifikat) und einen kleinen
MCP-Server als Kindprozess. Geprueft wird der Vertrag aus PROTOKOLL.md (Version 1) und die Sperren des Clients."""
import asyncio
import datetime
import json
import ssl
import sys
import time
from pathlib import Path

import pytest

from zeitspur import langdock_relay as lr
from zeitspur import plugins
from zeitspur.config import Config

TOKEN = "agent-token-0123456789abcdef"

FAKE_MCP = r'''
import json, os, sys, time
pid_file = sys.argv[1]
open(pid_file, "w").write(str(os.getpid()))
TOOLS = [{"name": n, "description": n, "inputSchema": {"type": "object"}}
         for n in ("echo", "slow", "crash", "get_screenshot")]
def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
              "serverInfo": {"name": "Zeitspur", "version": "test"}, "instructions": "nur Daten"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "crash":
            sys.exit(3)
        if name == "slow":
            time.sleep(2)
        text = json.dumps({"tool": name, "args": msg["params"].get("arguments"), "local_id": mid})
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unbekannt"}})
'''


# --------------------------------------------------------------------------- Hilfen

def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    import ipaddress

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


class FakeRelay:
    """Das Relay aus Sicht des Clients: prueft den Token, nimmt hello an und schickt Aufrufe."""

    def __init__(self, *, welcome: dict | None = None, close_code: int | None = None):
        self.welcome = welcome if welcome is not None else {"t": "welcome", "v": 1}
        self.close_code = close_code
        self.hellos: asyncio.Queue = asyncio.Queue()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.auth_headers: list[str | None] = []

    def process_request(self, connection, request):
        auth = request.headers.get("Authorization")
        self.auth_headers.append(auth)
        if auth != f"Bearer {TOKEN}":
            return connection.respond(401, "Token falsch\n")
        return None

    async def handler(self, ws):
        self.ws = ws
        await self.hellos.put(json.loads(await ws.recv()))
        await ws.send(json.dumps(self.welcome))
        if self.close_code is not None:
            await ws.close(self.close_code, "replaced")
            return
        async for raw in ws:
            await self.inbox.put(json.loads(raw))

    async def call(self, rid: str, msg: dict, timeout: float = 10.0) -> dict:
        await self.ws.send(json.dumps({"t": "call", "rid": rid, "msg": msg}))
        return await self.reply_for(rid, timeout)

    async def reply_for(self, rid: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            msg = await asyncio.wait_for(self.inbox.get(), max(remaining, 0.01))
            if msg.get("t") == "reply" and msg.get("rid") == rid:
                return msg


async def _wait_state(client: lr.RelayClient, states: tuple[str, ...], timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.status()
        if status["state"] in states:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"Zustand {states} nicht erreicht, zuletzt {client.status()}")


def _pid_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.fixture
def env(tmp_path):
    cert, key = _self_signed(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_verify_locations(cert)   # vertraut genau diesem Testzertifikat, Hostname wird geprueft
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_MCP, encoding="utf-8")
    pid_file = tmp_path / "mcp.pid"

    def make_client(port: int, *, token: str = TOKEN, screenshots: bool = False) -> lr.RelayClient:
        settings = lr.RelaySettings(f"wss://127.0.0.1:{port}/agent", token, screenshots)
        factory = lambda **kw: lr.LocalMcp(command=([sys.executable, "-I", str(script), str(pid_file)], None), **kw)
        return lr.RelayClient(settings, mcp_factory=factory, ssl_context=client_ctx)

    return {"server_ctx": server_ctx, "make_client": make_client, "pid_file": pid_file}


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


async def _serve(env, relay: FakeRelay):
    from websockets.asyncio.server import serve

    return await serve(relay.handler, "127.0.0.1", 0, ssl=env["server_ctx"], process_request=relay.process_request,
                       max_size=lr.MAX_MESSAGE)


def _port(server) -> int:
    return server.sockets[0].getsockname()[1]


# --------------------------------------------------------------------------- Verbindung und Vertrag

def test_hello_mit_init_und_werkzeugen_ohne_screenshot_dann_verbunden(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            hello = await asyncio.wait_for(relay.hellos.get(), 20)
            assert hello["t"] == "hello" and hello["v"] == 1
            assert hello["init"]["serverInfo"]["name"] == "Zeitspur" and hello["init"]["instructions"] == "nur Daten"
            assert [t["name"] for t in hello["tools"]["tools"]] == ["echo", "slow", "crash"]   # ohne get_screenshot
            status = await _wait_state(client, (lr.CONNECTED,))
            assert status["text"] == "Verbunden mit 127.0.0.1"
            assert relay.auth_headers == [f"Bearer {TOKEN}"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()
        assert client.status()["state"] == lr.OFF

    _run(scenario())


def test_screenshots_erscheinen_nur_mit_freigabe(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server), screenshots=True)
        client.start()
        try:
            hello = await asyncio.wait_for(relay.hellos.get(), 20)
            assert "get_screenshot" in [t["name"] for t in hello["tools"]["tools"]]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_aufrufe_werden_weitergereicht_und_ids_zurueckgesetzt(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            await asyncio.wait_for(relay.hellos.get(), 20)
            await _wait_state(client, (lr.CONNECTED,))
            # Zwei Langdock-Sitzungen benutzen dieselbe JSON-RPC-id - jede Antwort muss zu ihrer rid passen
            call = lambda arg: {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "echo", "arguments": {"x": arg}}}
            await relay.ws.send(json.dumps({"t": "call", "rid": "a", "msg": call("A")}))
            await relay.ws.send(json.dumps({"t": "call", "rid": "b", "msg": call("B")}))
            replies, local_ids = {}, set()
            for _ in range(2):
                msg = await asyncio.wait_for(relay.inbox.get(), 10)
                replies[msg["rid"]] = msg["msg"]
            for rid, arg in (("a", "A"), ("b", "B")):
                assert replies[rid]["id"] == 1
                body = json.loads(replies[rid]["result"]["content"][0]["text"])
                assert body["tool"] == "echo" and body["args"] == {"x": arg}
                local_ids.add(body["local_id"])
            assert len(local_ids) == 2 and min(local_ids) > 2   # eigene ids: 1 und 2 waren initialize und tools/list
            assert client.status()["calls"] == 2 and client.status()["last_call"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_sperren_screenshot_fremde_methoden_ping_und_werkzeugliste(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            await asyncio.wait_for(relay.hellos.get(), 20)
            await _wait_state(client, (lr.CONNECTED,))
            shot = await relay.call("s", {"jsonrpc": "2.0", "id": "x", "method": "tools/call",
                                          "params": {"name": "get_screenshot", "arguments": {"entry_id": 1}}})
            assert shot["msg"]["id"] == "x" and shot["msg"]["result"]["isError"] is True
            assert "nicht freigegeben" in shot["msg"]["result"]["content"][0]["text"]
            other = await relay.call("r", {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}})
            assert other["msg"]["error"]["code"] == -32601
            ping = await relay.call("p", {"jsonrpc": "2.0", "id": 8, "method": "ping"})
            assert ping["msg"] == {"jsonrpc": "2.0", "id": 8, "result": {}}
            tools = await relay.call("l", {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})
            assert [t["name"] for t in tools["msg"]["result"]["tools"]] == ["echo", "slow", "crash"]
            assert client.status()["calls"] == 1   # nur tools/call zaehlt
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_absturz_des_mcp_servers_wird_gemeldet_und_neu_gestartet(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            await asyncio.wait_for(relay.hellos.get(), 20)
            await _wait_state(client, (lr.CONNECTED,))
            first_pid = int(env["pid_file"].read_text())
            crash = await relay.call("c", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "crash", "arguments": {}}})
            assert crash["msg"]["result"]["isError"] is True
            echo = await relay.call("e", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                          "params": {"name": "echo", "arguments": {}}})
            assert "isError" not in echo["msg"]["result"]
            assert int(env["pid_file"].read_text()) != first_pid
            assert client.status()["state"] == lr.CONNECTED   # die Verbindung zum Relay blieb stehen
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_zeitueberschreitung_liefert_klare_meldung(env, monkeypatch):
    monkeypatch.setattr(lr, "CALL_TIMEOUT_S", 0.5)

    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            await asyncio.wait_for(relay.hellos.get(), 20)
            await _wait_state(client, (lr.CONNECTED,))
            slow = await relay.call("z", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": "slow", "arguments": {}}})
            assert "nicht rechtzeitig" in slow["msg"]["result"]["content"][0]["text"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_stop_beendet_verbindung_und_kindprozess(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            await asyncio.wait_for(relay.hellos.get(), 20)
            await _wait_state(client, (lr.CONNECTED,))
            pid = int(env["pid_file"].read_text())
            assert _pid_alive(pid)
            await asyncio.get_running_loop().run_in_executor(None, client.stop)
            assert not client.is_alive()
            deadline = time.monotonic() + 5
            while _pid_alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert not _pid_alive(pid)
            assert relay.ws.close_code == 1001
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


# --------------------------------------------------------------------------- Fehlerbilder

def test_falscher_token_wird_als_abgelehnt_gemeldet(env):
    async def scenario():
        relay = FakeRelay()
        server = await _serve(env, relay)
        client = env["make_client"](_port(server), token="falsch-falsch-falsch-123")
        client.start()
        try:
            status = await _wait_state(client, (lr.REJECTED,), timeout=20)
            assert "Agent-Token" in status["text"] and "401" in status["text"] and "10 min" in status["text"]
            assert "falsch-falsch" not in status["text"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_ersetzte_verbindung_wartet_laenger(env):
    async def scenario():
        relay = FakeRelay(close_code=lr.CLOSE_REPLACED)
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            status = await _wait_state(client, (lr.WAITING,), timeout=20)
            assert "ersetzt" in status["text"] and "5 min" in status["text"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_falsche_protokollversion_wird_erkannt(env):
    async def scenario():
        relay = FakeRelay(welcome={"t": "welcome", "v": 2})
        server = await _serve(env, relay)
        client = env["make_client"](_port(server))
        client.start()
        try:
            status = await _wait_state(client, (lr.WAITING,), timeout=20)
            assert "Protokollversion 2" in status["text"]
        finally:
            client.stop()
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_relay_nicht_erreichbar(env):
    async def scenario():
        client = env["make_client"](1)   # Port 1: niemand hoert
        client.start()
        try:
            status = await _wait_state(client, (lr.WAITING,), timeout=30)
            assert status["text"].startswith("Getrennt:") and "neuer Versuch" in status["text"]
        finally:
            client.stop()

    _run(scenario())


# --------------------------------------------------------------------------- Adresse, Plugin, Verwaltung

@pytest.mark.parametrize("raw,expected", [
    ("zeitspur.example.de", "wss://zeitspur.example.de/agent"),
    ("https://Zeitspur.Example.de/", "wss://zeitspur.example.de/agent"),
    ("wss://zeitspur.example.de:8443/agent", "wss://zeitspur.example.de:8443/agent"),
    ("wss://zeitspur.example.de/relay/agent", "wss://zeitspur.example.de/relay/agent"),
])
def test_adresse_wird_vereinheitlicht(raw, expected):
    assert lr.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ws://zeitspur.example.de/agent", "http://x.de", "wss://user:pw@x.de/agent",
                                 "wss://x.de/agent?token=abc", "wss://x.de:99999/agent"])
def test_unsichere_oder_falsche_adressen_werden_abgelehnt(raw):
    with pytest.raises(ValueError):
        lr.normalize_url(raw)


def test_plugin_prueft_adresse_und_token():
    plugin = plugins.get("langdock")
    assert plugin.service and plugin.category == "KI-Zugänge"
    clean = plugin.clean_settings({"url": "https://zeitspur.example.de", "screenshots": False, "active": True})
    assert clean["url"] == "wss://zeitspur.example.de/agent"
    with pytest.raises(ValueError):
        plugin.clean_settings({"url": "ws://zeitspur.example.de"})
    assert plugin.clean_credentials({"token": f"  {TOKEN} "})["token"] == TOKEN
    with pytest.raises(ValueError):
        plugin.clean_credentials({"token": "kurz"})


def test_abgleich_laesst_zugaenge_aus(monkeypatch):
    from zeitspur.events_sync import EventSync

    plugin = plugins.get("langdock")
    monkeypatch.setattr(type(plugin), "fetch", lambda *a, **k: pytest.fail("fetch darf nicht laufen"))
    cfg = Config(installed_plugins=["langdock"])
    sync = EventSync(cfg, storage=None)
    assert sync.any_enabled() is False
    report = sync.sync_range(datetime.date.today(), datetime.date.today())
    assert report.counts == {} and report.errors == []


def test_gewuenschte_einstellungen_aus_der_konfiguration(monkeypatch):
    plugin = plugins.get("langdock")
    creds = {"token": TOKEN}
    monkeypatch.setattr(type(plugin), "load_credentials", lambda self: dict(creds))
    assert lr.desired_settings(Config())[0] is None
    cfg = Config(installed_plugins=["langdock"])
    assert lr.desired_settings(cfg) == (None, "Aus (Relay-Adresse fehlt)")
    cfg.plugin_settings = {"langdock": {"url": "wss://r.example.de/agent"}}
    settings, note = lr.desired_settings(cfg)
    assert settings == lr.RelaySettings("wss://r.example.de/agent", TOKEN, False) and note == ""
    assert TOKEN not in repr(settings)
    cfg.plugin_settings = {"langdock": {"url": "wss://r.example.de/agent", "active": False}}
    assert lr.desired_settings(cfg) == (None, "Aus (im Tray ausgeschaltet)")
    assert lr.is_active(cfg) is False
    creds.clear()
    cfg.plugin_settings = {"langdock": {"url": "wss://r.example.de/agent"}}
    assert lr.desired_settings(cfg) == (None, "Aus (Agent-Token fehlt)")


class _FakeClient:
    made: list = []

    def __init__(self, settings, on_status=None):
        self.settings, self.on_status, self.running = settings, on_status, False
        _FakeClient.made.append(self)

    def start(self):
        self.running = True

    def stop(self, timeout=8.0):
        self.running = False

    def is_alive(self):
        return self.running

    def status(self):
        return {"state": lr.CONNECTED, "text": "Verbunden mit r.example.de", "last_call": None}


def test_verwaltung_startet_wechselt_und_stoppt(monkeypatch):
    plugin = plugins.get("langdock")
    monkeypatch.setattr(type(plugin), "load_credentials", lambda self: {"token": TOKEN})
    _FakeClient.made = []
    changes = []
    manager = lr.RelayManager(client_factory=_FakeClient)
    manager.on_change = lambda: changes.append(1)
    cfg = Config(installed_plugins=["langdock"], plugin_settings={"langdock": {"url": "wss://r.example.de/agent"}})
    manager.apply(cfg)
    manager.apply(cfg)                      # unveraendert: kein neuer Client
    assert len(_FakeClient.made) == 1 and _FakeClient.made[0].running
    assert manager.status_text().startswith("Verbunden mit r.example.de. Langdock kann")
    cfg.plugin_settings = {"langdock": {"url": "wss://r.example.de/agent", "screenshots": True}}
    manager.apply(cfg)                      # geaendert: alter Client weg, neuer mit Freigabe
    assert len(_FakeClient.made) == 2 and not _FakeClient.made[0].running and _FakeClient.made[1].settings.screenshots
    cfg.plugin_settings = {"langdock": {"url": "wss://r.example.de/agent", "active": False}}
    manager.apply(cfg)
    assert not _FakeClient.made[1].running
    assert manager.status() == {"state": lr.OFF, "text": "Aus (im Tray ausgeschaltet)", "since": None,
                                "last_call": None, "calls": 0}
    assert changes


def test_plugin_test_meldet_fehlende_einrichtung():
    plugin = plugins.get("langdock")
    assert plugin.test_connection(Config(installed_plugins=["langdock"])).startswith("Nicht bereit:")
