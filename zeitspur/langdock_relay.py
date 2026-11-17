"""Langdock-Zugang: Zeitspur beantwortet MCP-Aufrufe aus Langdock ueber ein eigenes Relay.

Langdock startet keine lokalen MCP-Server, sondern ruft sie aus seiner Cloud ueber eine oeffentliche
HTTPS-Adresse auf. Dazwischen steht ein eigenes Relay (Protokoll: PROTOKOLL.md im Relay-Repo, Version 1).
Zeitspur baut die Verbindung selbst auf - eine WebSocket-Verbindung nach aussen, der PC ist nie von aussen
erreichbar - und reicht jeden Aufruf an denselben MCP-Server weiter, den auch Claude Desktop startet:
`Zeitspur.exe --mcp`, nur lesend, als eigener Prozess.

Regeln:
- Nur wss:// mit Zertifikatspruefung wie Windows (winutil.https_context). Der Agent-Token steht nur im
  Header und taucht in keiner Meldung und keinem Protokoll auf.
- Weitergereicht werden nur tools/call (fuer freigegebene Werkzeuge) und ping. tools/list beantwortet der
  Client aus seiner Liste, alles andere lehnt er ab - auch wenn das Relay es schickt.
- Screenshots nur mit ausdruecklicher Freigabe: sonst fehlt get_screenshot in der Werkzeugliste, und Aufrufe
  werden abgewiesen. Doppelt gesichert, falls die Freigabe in Langdock anders steht.
- Protokolliert werden nur Metadaten (Werkzeug, Dauer, Status, Groesse), keine Argumente und keine Ergebnisse.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import random
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import __version__

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

log = logging.getLogger(__name__)

PLUGIN_ID = "langdock"
PROTOCOL_VERSION = 1
MCP_PROTOCOL_VERSION = "2026-07-28"      # Vorschlag an den lokalen Server; er antwortet mit seiner Version
MAX_MESSAGE = 25 * 1024 * 1024           # laut Vertrag; Screenshots kommen als Base64
CALL_TIMEOUT_S = 85.0                    # knapp unter den 90 s des Relays: lieber selbst eine klare Meldung
WELCOME_TIMEOUT_S = 15.0
INIT_TIMEOUT_S = 60.0                    # der MCP-Server oeffnet beim Start die Datenbank
OPEN_TIMEOUT_S = 20.0
PING_INTERVAL_S = 20.0
PING_TIMEOUT_S = 40.0                    # zusammen mit dem Intervall: spaetestens nach 60 s ohne Pong tot
BACKOFF_S = (2, 5, 10, 20, 40, 60)
REJECTED_RETRY_S = 600.0
REPLACED_RETRY_S = 300.0
MCP_FAILED_RETRY_S = 60.0
STABLE_AFTER_S = 60.0
SCREENSHOT_TOOL = "get_screenshot"
CLOSE_REPLACED = 4000
FORWARDED_METHODS = ("tools/call",)

# Zustaende fuer Tray und Plugin-Browser
OFF, STARTING, CONNECTING, CONNECTED, WAITING, REJECTED, ERROR = (
    "off", "starting", "connecting", "connected", "waiting", "rejected", "error")


class LocalMcpError(Exception):
    """Der lokale MCP-Server antwortet nicht (mehr) oder mit einem Fehler."""


class RelayProtocolError(Exception):
    """Das Relay haelt sich nicht an den Vertrag."""


# --------------------------------------------------------------------------- Adresse

def normalize_url(raw: str) -> str:
    """Eingabe -> wss-Adresse des Agent-Endpunkts. Ohne Pfad wird /agent ergaenzt. Wirft ValueError."""
    url = (raw or "").strip()
    if not url:
        raise ValueError("Bitte die Relay-Adresse eintragen.")
    if "://" not in url:
        url = "wss://" + url
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https":
        scheme = "wss"
    if scheme != "wss" or not parts.hostname:
        raise ValueError("Nur wss:// bzw. https:// ist erlaubt – sonst wäre der Verkehr unterwegs mitlesbar.")
    if parts.username or parts.password:
        raise ValueError("Zugangsdaten gehören nicht in die Adresse, sondern ins Feld „Agent-Token“.")
    if parts.query or parts.fragment:
        raise ValueError("Die Adresse darf keine Parameter (?…, #…) enthalten.")
    try:
        parts.port  # noqa: B018 - wirft bei ungueltigem Port
    except ValueError as e:
        raise ValueError("Der Port in der Adresse ist ungültig.") from e
    path = parts.path if parts.path not in ("", "/") else "/agent"
    return urllib.parse.urlunsplit((scheme, parts.netloc.lower(), path, "", ""))


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "?"


# --------------------------------------------------------------------------- lokaler MCP-Server

def mcp_command() -> tuple[list[str], dict[str, str] | None]:
    """Startkommando des MCP-Servers - wie fuer Claude Desktop (mcp_server.server_command), ohne das schwere
    MCP-Paket in den Dienstprozess zu laden."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--mcp"], None
    root = str(Path(__file__).resolve().parents[1])
    path = os.environ.get("PYTHONPATH")
    return [str(Path(sys.executable).resolve()), "-m", "zeitspur.mcp_server"], {
        "PYTHONPATH": root + (os.pathsep + path if path else "")}


def _dumps(msg: Any) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def tool_error(req_id: Any, text: str) -> dict:
    """Werkzeug-Ergebnis mit isError - Langdock zeigt den Text, statt die Unterhaltung abzubrechen."""
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


class LocalMcp:
    """Zeitspurs MCP-Server als Kindprozess, angesprochen ueber stdio (JSON-RPC, eine Zeile je Nachricht)."""

    def __init__(self, on_tools_changed: Callable[[], Any] | None = None,
                 command: tuple[list[str], dict[str, str] | None] | None = None):
        self._on_tools_changed = on_tools_changed
        self._command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self.init_result: dict | None = None
        self.tools_result: dict | None = None

    @property
    def alive(self) -> bool:
        return (self._proc is not None and self._proc.returncode is None
                and self._reader is not None and not self._reader.done())

    def new_id(self) -> int:
        return next(self._ids)

    async def start(self) -> None:
        argv, extra_env = self._command or mcp_command()
        env = {**os.environ, **extra_env} if extra_env else None
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=2 * MAX_MESSAGE, **kwargs)
        self._reader = asyncio.create_task(self._read_loop())
        try:
            self.init_result = await self.request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "zeitspur-langdock-relay", "version": __version__}}, timeout=INIT_TIMEOUT_S)
            await self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools_result = await self.request("tools/list", {}, timeout=30.0)
        except BaseException:
            await self.close()
            raise
        names = [t.get("name") for t in (self.tools_result or {}).get("tools", []) if isinstance(t, dict)]
        log.info("Langdock: MCP-Server bereit (%d Werkzeuge)", len(names))

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        """Eigene Anfrage an den Server; liefert result oder wirft LocalMcpError."""
        response = await self.call({"jsonrpc": "2.0", "method": method, "params": params}, self.new_id(), timeout)
        if "error" in response:
            message = (response.get("error") or {}).get("message", "unbekannter Fehler")
            raise LocalMcpError(f"{method}: {message}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def call(self, msg: dict, local_id: int, timeout: float) -> dict:
        """Schickt eine JSON-RPC-Anfrage unter der id local_id und liefert die Antwort des Servers."""
        if not self.alive:
            raise LocalMcpError("MCP-Server läuft nicht")
        future = asyncio.get_running_loop().create_future()
        self._pending[local_id] = future
        try:
            await self._write({**msg, "id": local_id})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(local_id, None)

    async def notify(self, msg: dict) -> None:
        await self._write(msg)

    async def cancel(self, local_id: int) -> None:
        try:
            await self.notify({"jsonrpc": "2.0", "method": "notifications/cancelled",
                               "params": {"requestId": local_id, "reason": "in Langdock abgebrochen"}})
        except LocalMcpError:
            pass

    async def _write(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise LocalMcpError("MCP-Server läuft nicht")
        data = (_dumps(msg) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (ConnectionError, OSError) as e:
                raise LocalMcpError("MCP-Server nimmt nichts mehr an") from e

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError:   # Zeile laenger als das Limit: die Zeilengrenzen sind verloren
                    log.warning("Langdock: Antwort des MCP-Servers zu groß – Server wird neu gestartet")
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("Langdock: unlesbare Zeile vom MCP-Server verworfen")
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Langdock: Lesen vom MCP-Server fehlgeschlagen")
        finally:
            error = LocalMcpError("MCP-Server beendet")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            if "id" in msg:   # Anfrage des Servers (Sampling, Roots ...): gibt es ueber Langdock nicht
                try:
                    await self._write({"jsonrpc": "2.0", "id": msg["id"],
                                       "error": {"code": -32601, "message": "Nicht unterstützt"}})
                except LocalMcpError:
                    pass
            elif msg.get("method") == "notifications/tools/list_changed":
                asyncio.create_task(self._refresh_tools())
            return
        msg_id = msg.get("id")
        future = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
        if future is not None and not future.done():
            future.set_result(msg)

    async def _refresh_tools(self) -> None:
        try:
            self.tools_result = await self.request("tools/list", {}, timeout=30.0)
        except (LocalMcpError, asyncio.TimeoutError):
            return
        if self._on_tools_changed is not None:
            result = self._on_tools_changed()
            if asyncio.iscoroutine(result):
                await result

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()   # stdin zu = der MCP-Server beendet sich selbst
                await asyncio.wait_for(proc.wait(), 3.0)
            except (asyncio.TimeoutError, OSError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 3.0)
                except (ProcessLookupError, OSError, asyncio.TimeoutError):
                    pass
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


# --------------------------------------------------------------------------- Relay-Verbindung

@dataclass(frozen=True)
class RelaySettings:
    url: str
    token: str = field(repr=False)
    screenshots: bool = False


@dataclass
class _Inflight:
    task: asyncio.Task
    req_id: Any
    local_id: int | None = None


def _describe_os_error(e: BaseException) -> str:
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"Zertifikat des Relays ungültig ({e.verify_message or 'Prüfung fehlgeschlagen'})"
    if isinstance(e, ssl.SSLError):
        return "TLS-Verbindung fehlgeschlagen"
    if isinstance(e, socket.gaierror):
        return "Adresse nicht auflösbar (kein Internet oder falscher Hostname)"
    if isinstance(e, ConnectionRefusedError):
        return "Verbindung abgelehnt"
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "Zeitüberschreitung beim Verbindungsaufbau"
    return f"nicht erreichbar ({type(e).__name__})"


def _fmt_delay(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


class RelayClient:
    """Eine Verbindung zum Relay samt lokalem MCP-Server; laeuft in einem eigenen Thread mit eigener Event-Loop."""

    def __init__(self, settings: RelaySettings, *, on_status: Callable[[dict], None] | None = None,
                 mcp_factory: Callable[..., LocalMcp] | None = None, ssl_context: ssl.SSLContext | None = None):
        self.settings = settings
        self._on_status = on_status
        self._mcp_factory = mcp_factory or LocalMcp
        self._ssl_context = ssl_context
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {"state": OFF, "text": "Aus", "since": time.time(),
                                        "last_call": None, "calls": 0}
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._mcp_lock: asyncio.Lock | None = None
        self._mcp: LocalMcp | None = None
        self._ws = None
        self._inflight: dict[str, _Inflight] = {}

    # ---- Steuerung (beliebiger Thread) ----------------------------------------------------------
    def start(self) -> None:
        self._loop = asyncio.new_event_loop()   # unter Windows eine Proactor-Loop: kann Kindprozesse
        self._stop = asyncio.Event()
        self._mcp_lock = asyncio.Lock()
        self._set_status(STARTING, "Startet …")
        self._thread = threading.Thread(target=self._run, name="langdock-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:   # Loop schon geschlossen
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    # ---- intern ---------------------------------------------------------------------------------
    def _set_status(self, state: str, text: str, **extra: Any) -> None:
        with self._lock:
            changed = state != self._status["state"] or text != self._status["text"]
            if changed:
                self._status.update(state=state, text=text, since=time.time())
            self._status.update(extra)
            snapshot = dict(self._status)
        if changed:
            log.info("Langdock: %s", text)
            if self._on_status is not None:
                try:
                    self._on_status(snapshot)
                except Exception:
                    log.debug("Statusmeldung fehlgeschlagen", exc_info=True)

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("Langdock-Relay beendet sich mit Fehler")
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            finally:
                self._loop.close()
            self._set_status(OFF, "Aus")

    async def _sleep(self, seconds: float) -> None:
        assert self._stop is not None
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _main(self) -> None:
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

        assert self._stop is not None
        host = host_of(self.settings.url)
        attempt = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._ensure_mcp()
                except Exception as e:
                    detail = str(e) if isinstance(e, LocalMcpError) else type(e).__name__
                    log.warning("Langdock: MCP-Server startet nicht: %s", detail)
                    self._set_status(ERROR, f"Zeitspurs MCP-Server startet nicht ({detail}) – neuer Versuch in "
                                            f"{_fmt_delay(MCP_FAILED_RETRY_S)}")
                    await self._sleep(MCP_FAILED_RETRY_S)
                    continue
                self._set_status(CONNECTING, f"Verbinde mit {host} …")
                started = time.monotonic()
                state, delay = WAITING, None
                try:
                    reason = await self._serve_once(host)
                except InvalidStatus as e:
                    code = e.response.status_code
                    if code in (401, 403):
                        state, delay = REJECTED, REJECTED_RETRY_S
                        reason = f"Relay lehnt den Agent-Token ab (HTTP {code})"
                    elif code == 404:
                        reason = "Relay kennt die Adresse nicht (HTTP 404) – Pfad prüfen, meist /agent"
                    else:
                        reason = f"Relay antwortet mit HTTP {code}"
                except InvalidURI:
                    state, delay, reason = ERROR, 3600.0, "Relay-Adresse ungültig"
                except ConnectionClosed as e:
                    code = e.rcvd.code if e.rcvd is not None else None
                    if code == CLOSE_REPLACED:
                        delay, reason = REPLACED_RETRY_S, "von einer anderen Verbindung ersetzt"
                    else:
                        reason = "Verbindung abgebrochen" + (f" (Code {code})" if code else "")
                except RelayProtocolError as e:
                    reason = str(e)
                except LocalMcpError as e:
                    reason = f"MCP-Server: {e}"
                except InvalidHandshake as e:
                    reason = f"Verbindungsaufbau fehlgeschlagen ({type(e).__name__})"
                except (OSError, asyncio.TimeoutError) as e:
                    reason = _describe_os_error(e)
                except Exception as e:   # nie aufgeben: lieber spaeter neu versuchen als still aus sein
                    log.exception("Langdock: unerwarteter Fehler in der Verbindung")
                    reason = f"unerwarteter Fehler ({type(e).__name__})"
                if self._stop.is_set():
                    break
                if time.monotonic() - started >= STABLE_AFTER_S:
                    attempt = 0
                if delay is None:
                    delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] * random.uniform(0.8, 1.2)
                    attempt += 1
                log.warning("Langdock: getrennt: %s", reason)
                self._set_status(state, f"Getrennt: {reason} – neuer Versuch in {_fmt_delay(delay)}")
                await self._sleep(delay)
        finally:
            await self._cleanup()

    async def _ensure_mcp(self) -> None:
        assert self._mcp_lock is not None
        async with self._mcp_lock:
            if self._mcp is not None and self._mcp.alive:
                return
            before = self._visible_tools() if self._mcp is not None else None
            if self._mcp is not None:
                await self._mcp.close()
                self._mcp = None
            if self._ws is None:
                self._set_status(STARTING, "Starte Zeitspurs MCP-Server …")
            mcp = self._mcp_factory(on_tools_changed=self._tools_changed)
            await mcp.start()
            self._mcp = mcp
            if before is not None and before != self._visible_tools():
                await self._tools_changed()

    def _visible_tools(self) -> dict:
        result = dict((self._mcp.tools_result if self._mcp is not None else None) or {})
        tools = [t for t in result.get("tools", []) if isinstance(t, dict)]
        if not self.settings.screenshots:
            tools = [t for t in tools if t.get("name") != SCREENSHOT_TOOL]
        result["tools"] = tools
        return result

    def _allowed_tools(self) -> set[str]:
        return {t.get("name") for t in self._visible_tools()["tools"] if isinstance(t.get("name"), str)}

    async def _tools_changed(self) -> None:
        ws = self._ws
        if ws is None:
            return
        from websockets.exceptions import ConnectionClosed

        try:
            await ws.send(_dumps({"t": "tools_changed", "tools": self._visible_tools()}))
        except ConnectionClosed:
            pass

    async def _serve_once(self, host: str) -> str:
        """Eine Verbindung vom Aufbau bis zum Ende. Liefert den Grund bei sauberem Ende, sonst Ausnahme."""
        from websockets.asyncio.client import connect

        assert self._stop is not None and self._mcp is not None
        ssl_context = self._ssl_context
        if ssl_context is None:
            from .winutil import https_context
            ssl_context = https_context()
        async with connect(
                self.settings.url, additional_headers={"Authorization": f"Bearer {self.settings.token}"},
                ssl=ssl_context, user_agent_header=f"Zeitspur/{__version__}", open_timeout=OPEN_TIMEOUT_S,
                ping_interval=PING_INTERVAL_S, ping_timeout=PING_TIMEOUT_S, close_timeout=5,
                max_size=MAX_MESSAGE, max_queue=64) as ws:
            await ws.send(_dumps({"t": "hello", "v": PROTOCOL_VERSION, "init": self._mcp.init_result,
                                  "tools": self._visible_tools()}))
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT_S))
            except asyncio.TimeoutError as e:
                raise RelayProtocolError("Relay hat die Anmeldung nicht bestätigt") from e
            except ValueError as e:
                raise RelayProtocolError("Relay hat unlesbar geantwortet") from e
            if not isinstance(first, dict) or first.get("t") != "welcome":
                raise RelayProtocolError("Relay hat die Anmeldung nicht bestätigt")
            if first.get("v") != PROTOCOL_VERSION:
                raise RelayProtocolError(f"Relay spricht Protokollversion {first.get('v')}, "
                                         f"Zeitspur {PROTOCOL_VERSION}")
            self._ws = ws
            self._set_status(CONNECTED, f"Verbunden mit {host}")
            reader = asyncio.create_task(self._read_frames(ws))
            stopper = asyncio.create_task(self._stop.wait())
            try:
                done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    reader.result()   # Ausnahme (Verbindungsabbruch) weiterreichen
                    return "Relay hat die Verbindung beendet"
                await ws.close(1001, "Zeitspur beendet den Zugang")
                return "beendet"
            finally:
                for task in (reader, stopper):
                    task.cancel()
                await asyncio.gather(reader, stopper, return_exceptions=True)
                self._ws = None
                await self._cancel_inflight()

    async def _read_frames(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    log.warning("Langdock: unlesbare Nachricht vom Relay verworfen")
                    continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log.warning("Langdock: unlesbare Nachricht vom Relay verworfen")
                continue
            if isinstance(msg, dict):
                self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "call":
            rid, rpc = msg.get("rid"), msg.get("msg")
            if not isinstance(rid, str) or not rid or not isinstance(rpc, dict):
                log.warning("Langdock: Aufruf ohne rid oder msg verworfen")
                return
            if "id" not in rpc:      # eine Benachrichtigung im falschen Umschlag
                self._handle_notify(rpc)
                return
            if rid in self._inflight:
                log.warning("Langdock: doppelte rid verworfen")
                return
            task = asyncio.create_task(self._handle_call(rid, rpc))
            self._inflight[rid] = _Inflight(task, rpc.get("id"))
            task.add_done_callback(lambda _t, rid=rid: self._inflight.pop(rid, None))
        elif kind == "notify":
            rpc = msg.get("msg")
            if isinstance(rpc, dict):
                self._handle_notify(rpc)
        elif kind != "welcome":
            log.debug("Langdock: unbekannte Nachricht %r ignoriert", kind)

    def _handle_notify(self, rpc: dict) -> None:
        if rpc.get("method") != "notifications/cancelled":
            return
        params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
        matches = [rid for rid, f in self._inflight.items() if f.req_id == params.get("requestId")]
        if len(matches) != 1:    # ids sind nur je Langdock-Sitzung eindeutig: im Zweifel nichts abbrechen
            return
        inflight = self._inflight.pop(matches[0])
        inflight.task.cancel()
        if inflight.local_id is not None and self._mcp is not None:
            asyncio.create_task(self._mcp.cancel(inflight.local_id))
        log.info("Langdock: Aufruf abgebrochen")

    async def _handle_call(self, rid: str, rpc: dict) -> None:
        from websockets.exceptions import ConnectionClosed

        started = time.monotonic()
        method = rpc.get("method")
        req_id = rpc.get("id")
        tool: str | None = None
        outcome = "ok"
        if method == "ping":
            response: dict = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": req_id, "result": self._visible_tools()}
        elif method in FORWARDED_METHODS:
            params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
            tool = params.get("name") if isinstance(params.get("name"), str) else None
            if tool not in self._allowed_tools():
                outcome = "gesperrt"
                response = tool_error(req_id, f"Das Werkzeug „{tool}“ ist in Zeitspur für Langdock nicht freigegeben.")
            else:
                response, outcome = await self._forward(rid, rpc, req_id)
        else:
            outcome = "abgelehnt"
            response = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32601, "message": f"Methode {method!r} gibt Zeitspur über Langdock nicht frei"}}
        data = _dumps({"t": "reply", "rid": rid, "msg": response})
        size = len(data.encode("utf-8"))
        if size > MAX_MESSAGE:
            outcome = "zu groß"
            data = _dumps({"t": "reply", "rid": rid, "msg": tool_error(
                req_id, f"Die Antwort ist mit {size / 1024 / 1024:.0f} MB zu groß für das Relay (höchstens "
                        f"{MAX_MESSAGE // 1024 // 1024} MB). Bei Screenshots hilft eine kleinere Breite.")})
            size = len(data.encode("utf-8"))
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(data)
        except ConnectionClosed:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info("Langdock: %s%s – %s, %d ms, %d Bytes", method, f" {tool}" if tool else "", outcome,
                 duration_ms, size)
        if method in FORWARDED_METHODS:
            with self._lock:
                self._status["last_call"] = time.time()
                self._status["calls"] = int(self._status.get("calls") or 0) + 1

    async def _forward(self, rid: str, rpc: dict, req_id: Any) -> tuple[dict, str]:
        try:
            await self._ensure_mcp()
        except Exception:
            log.warning("Langdock: MCP-Server nicht bereit", exc_info=True)
            return tool_error(req_id, "Zeitspurs MCP-Server ist gerade nicht bereit. Bitte gleich noch einmal "
                                      "versuchen."), "fehler"
        mcp = self._mcp
        assert mcp is not None
        local_id = mcp.new_id()
        inflight = self._inflight.get(rid)
        if inflight is not None:
            inflight.local_id = local_id
        try:
            response = await mcp.call(rpc, local_id, CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            await mcp.cancel(local_id)
            return tool_error(req_id, "Zeitspur hat nicht rechtzeitig geantwortet."), "zeitüberschreitung"
        except LocalMcpError:
            return tool_error(req_id, "Zeitspurs MCP-Server ist gerade nicht bereit. Bitte gleich noch einmal "
                                      "versuchen."), "fehler"
        response = {**response, "id": req_id}
        result = response.get("result")
        failed = "error" in response or (isinstance(result, dict) and result.get("isError"))
        return response, "fehler" if failed else "ok"

    async def _cancel_inflight(self) -> None:
        tasks = [f.task for f in self._inflight.values()]
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup(self) -> None:
        await self._cancel_inflight()
        if self._mcp is not None:
            await self._mcp.close()
            self._mcp = None


# --------------------------------------------------------------------------- Verwaltung

def desired_settings(cfg: "Config") -> tuple[RelaySettings | None, str]:
    """Was laut Konfiguration laufen soll - oder None mit dem Grund, warum nicht."""
    from . import plugins

    if PLUGIN_ID not in cfg.installed_plugins:
        return None, "Aus (Plugin nicht hinzugefügt)"
    plugin = plugins.get(PLUGIN_ID)
    settings = plugin.settings(cfg)
    if not plugins.as_bool(settings.get("active", True)):
        return None, "Aus (im Tray ausgeschaltet)"
    try:
        url = normalize_url(str(settings.get("url") or ""))
    except ValueError:
        return None, "Aus (Relay-Adresse fehlt)"
    token = (plugin.load_credentials() or {}).get("token", "").strip()
    if not token:
        return None, "Aus (Agent-Token fehlt)"
    return RelaySettings(url, token, plugins.as_bool(settings.get("screenshots"))), ""


def is_active(cfg: "Config") -> bool:
    from . import plugins

    if PLUGIN_ID not in cfg.installed_plugins:
        return False
    return plugins.as_bool(plugins.get(PLUGIN_ID).settings(cfg).get("active", True))


class RelayManager:
    """Haelt hoechstens einen RelayClient passend zur Konfiguration am Laufen. Aus jedem Thread aufrufbar."""

    def __init__(self, client_factory: Callable[..., RelayClient] = RelayClient):
        self._factory = client_factory
        self._apply_lock = threading.Lock()
        self._client: RelayClient | None = None
        self._settings: RelaySettings | None = None
        self._note = "Aus"
        self.on_change: Callable[[], None] | None = None

    def apply(self, cfg: "Config") -> None:
        desired, note = desired_settings(cfg)
        with self._apply_lock:
            client = self._client
            if desired is not None and desired == self._settings and client is not None and client.is_alive():
                return
            self._note = note or "Aus"
            if client is not None:
                client.stop()
                self._client = None
            self._settings = desired
            if desired is not None:
                self._client = self._factory(desired, on_status=self._status_changed)
                self._client.start()
        self._status_changed(None)

    def stop(self) -> None:
        with self._apply_lock:
            client, self._client, self._settings = self._client, None, None
            if client is not None:
                client.stop()

    def status(self) -> dict[str, Any]:
        client = self._client
        if client is not None:
            return client.status()
        return {"state": OFF, "text": self._note, "since": None, "last_call": None, "calls": 0}

    def status_text(self, wait_s: float = 0.0) -> str:
        """Fuer den Knopf "Testen": wartet hoechstens wait_s, bis der Verbindungsaufbau ausgegangen ist."""
        deadline = time.monotonic() + wait_s
        status = self.status()
        while status["state"] in (STARTING, CONNECTING) and time.monotonic() < deadline:
            time.sleep(0.25)
            status = self.status()
        text = status["text"]
        if status["state"] == CONNECTED:
            text += ". Langdock kann Zeitspur jetzt abfragen."
            if status.get("last_call"):
                text += " Letzte Abfrage " + time.strftime("%H:%M", time.localtime(status["last_call"])) + "."
        return text

    def _status_changed(self, _snapshot: dict | None) -> None:
        callback = self.on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                log.debug("on_change fehlgeschlagen", exc_info=True)


MANAGER = RelayManager()
