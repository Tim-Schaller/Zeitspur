"""MCP-Smoke-Test ueber stdio-Pipes: initialize -> tools/list -> tools/call, wie es ein Claude-Client tut.

  .venv\\Scripts\\python tools\\mcp_smoke.py                       (python -m zeitspur.mcp_server)
  .venv\\Scripts\\python tools\\mcp_smoke.py --exe dist\\Zeitspur\\ZeitspurMCP.exe
Der Datenordner kommt aus ZEITSPUR_DATA_DIR (z. B. der Demo-Ordner von tools/ui_smoke.py).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, cmd: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=ROOT)
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"keine Antwort auf {method}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"keine Antwort auf {method}")
            if line is None:
                raise RuntimeError(f"Server beendet vor Antwort auf {method}: {self.proc.stderr.read()[:500]!r}")
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                print("  (kein JSON auf stdout!)", line[:120])
                continue
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return -1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--exe", help="Pfad zu ZeitspurMCP.exe (Standard: python -m zeitspur.mcp_server)")
    p.add_argument("--query", default="Serverumzug")
    p.add_argument("--mcp-flag", action="store_true", help="'--mcp' anhaengen (Zeitspur.exe im MCP-Modus)")
    args = p.parse_args()
    cmd = [args.exe] if args.exe else [sys.executable, "-m", "zeitspur.mcp_server"]
    if args.mcp_flag:
        cmd.append("--mcp")
    env = dict(os.environ)
    env.setdefault("ZEITSPUR_DATA_DIR", str(Path(os.environ["TEMP"]) / "ZeitspurSmoke-demo"))
    print("Kommando:", cmd, "| Datenordner:", env["ZEITSPUR_DATA_DIR"])
    client = Client(cmd, env)
    ok = True
    try:
        init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "zeitspur-smoke", "version": "1.0"}})
        print("initialize:", init.get("serverInfo"), "protocol", init.get("protocolVersion"))
        client.notify("notifications/initialized")
        tools = client.request("tools/list")["tools"]
        names = sorted(t["name"] for t in tools)
        print("tools/list:", names)
        expected = {"get_time_context", "search_activity", "get_activity_at", "list_active_apps", "get_entry", "get_screenshot", "get_calendar"}
        if set(names) != expected:
            print("FEHLER: unerwartete Werkzeugliste")
            ok = False
        ctx = client.request("tools/call", {"name": "get_time_context", "arguments": {}})
        text = ctx["content"][0]["text"] if ctx.get("content") else ""
        print("get_time_context:", text[:300].replace("\n", " "))
        if ctx.get("isError"):
            ok = False
        res = client.request("tools/call", {"name": "search_activity", "arguments": {"query": args.query, "limit": 3}})
        print("search_activity:", (res["content"][0]["text"] if res.get("content") else "")[:300].replace("\n", " "))
        apps = client.request("tools/call", {"name": "list_active_apps", "arguments": {"day": "heute"}})
        print("list_active_apps:", (apps["content"][0]["text"] if apps.get("content") else "")[:200].replace("\n", " "))
        structured = res.get("structuredContent") or {}
        if not structured and res.get("content"):
            try:
                structured = json.loads(res["content"][0]["text"])
            except (ValueError, KeyError, IndexError):
                structured = {}
        hits = structured.get("results") or []
        if hits:
            shot = client.request("tools/call", {"name": "get_screenshot", "arguments": {"entry_id": hits[0]["entry_id"], "max_width": 640}})
            img = [c for c in shot.get("content", []) if c.get("type") == "image"]
            print("get_screenshot:", img[0]["mimeType"] if img else "KEIN BILD", len(img[0]["data"]) if img else 0, "Base64-Zeichen")
            ok = ok and bool(img)
        else:
            print("get_screenshot: uebersprungen (keine Treffer)")
    except Exception as e:
        print("FEHLER:", e)
        ok = False
    finally:
        code = client.close()
        print("Server beendet mit", code, "| stderr:", (client.proc.stderr.read() or b"")[:300])
        ok = ok and code == 0
    print("ERGEBNIS:", "OK" if ok else "FEHLER")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
