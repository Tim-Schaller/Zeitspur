"""Windows-Ereignisprotokolle lesen - fuer die Plugins PC-Zeiten und WLAN-Netze.

Ohne Adminrechte lesbar sind das System-Protokoll und Betriebsprotokolle wie WLAN-AutoConfig; das
Sicherheitsprotokoll (Sperren/Entsperren, Anmeldungen) dagegen nicht. Gefiltert wird schon in Windows ueber
eine XPath-Abfrage (Ereignis-Ids und Zeitraum), gelesen wird das Ereignis-XML.
"""
from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
MAX_EVENTS = 50_000
_SYSTEMTIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z?$")


@dataclass(frozen=True)
class LogEvent:
    event_id: int
    provider: str
    ts_ms: int
    data: dict[str, str] = field(default_factory=dict)


def parse_systemtime(value: str) -> int:
    """'2026-10-04T10:10:54.1234567Z' (UTC, bis zu 7 Nachkommastellen) -> Unix-ms."""
    m = _SYSTEMTIME.match(value.strip())
    if not m:
        raise ValueError(f"Zeitangabe nicht lesbar: {value!r}")
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    micro = int((m.group(2) or "0")[:6].ljust(6, "0"))
    return int(dt.timestamp() * 1000) + micro // 1000


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_event_xml(xml: str) -> LogEvent | None:
    """Ereignis-XML -> LogEvent (rein, ohne Windows-Aufrufe; daher testbar)."""
    try:
        root = ET.fromstring(xml)
        system = root.find("e:System", NS)
        event_id = int(system.find("e:EventID", NS).text)
        provider = system.find("e:Provider", NS).get("Name") or ""
        ts = parse_systemtime(system.find("e:TimeCreated", NS).get("SystemTime"))
    except (ET.ParseError, AttributeError, TypeError, ValueError):
        return None
    data = {d.get("Name"): (d.text or "") for d in root.findall("e:EventData/e:Data", NS) if d.get("Name")}
    return LogEvent(event_id, provider, ts, data)


def build_xpath(event_ids: list[int], start_ms: int, end_ms: int) -> str:
    ids = " or ".join(f"EventID={int(i)}" for i in event_ids)
    return (f"*[System[({ids}) and TimeCreated[@SystemTime>='{iso_utc(start_ms)}' "
            f"and @SystemTime<='{iso_utc(end_ms)}']]]")


def query(channel: str, event_ids: list[int], start_ms: int, end_ms: int) -> list[LogEvent]:
    """Ereignisse eines Protokolls im Zeitraum, aelteste zuerst. Wirft OSError, wenn das Protokoll nicht lesbar ist."""
    if sys.platform != "win32":
        return []
    import win32evtlog

    try:
        handle = win32evtlog.EvtQuery(channel, win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryForwardDirection,
                                      build_xpath(event_ids, start_ms, end_ms))
    except Exception as e:   # pywintypes.error: Zugriff verweigert, Protokoll fehlt
        raise OSError(f"Ereignisprotokoll {channel} nicht lesbar: {e}") from e
    events: list[LogEvent] = []
    while len(events) < MAX_EVENTS:
        batch = win32evtlog.EvtNext(handle, 256)
        if not batch:
            break
        for item in batch:
            parsed = parse_event_xml(win32evtlog.EvtRender(item, win32evtlog.EvtRenderEventXml))
            if parsed is not None:
                events.append(parsed)
    return events


def access_reason(channel: str) -> str | None:
    """Grund, warum ein Protokoll nicht lesbar ist - sonst None."""
    if sys.platform != "win32":
        return "Nur unter Windows verfügbar."
    import win32evtlog

    try:
        win32evtlog.EvtQuery(channel, win32evtlog.EvtQueryChannelPath, "*[System[EventID=0]]")
    except Exception as e:
        log.debug("Protokoll %s nicht lesbar: %s", channel, e)
        return f"Das Windows-Ereignisprotokoll „{channel}“ ist auf diesem PC nicht lesbar."
    return None
