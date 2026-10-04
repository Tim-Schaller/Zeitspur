"""Outlook-Mails lokal ueber COM (klassisches Outlook): wann welche Mail gesendet bzw. empfangen wurde.

Gelesen werden nur Betreff, Absender bzw. Empfaenger und Uhrzeit aus "Gesendete Elemente" und dem Posteingang -
nie der Mailtext oder Anhaenge. Wie beim Kalender (outlook.py): Faellt COM aus, wird ein OutlookError GEWORFEN,
damit der Abgleich die zwischengespeicherten Mails nicht als "keine Mails" loescht.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta

from .outlook import OutlookError, _com_datetime_to_ms

log = logging.getLogger(__name__)

SOURCE = "outlook_mail"
OL_FOLDER_SENT = 5
OL_FOLDER_INBOX = 6
OL_MAIL = 43                 # olMail - Besprechungsanfragen, Berichte usw. bleiben aussen vor
MAX_PER_FOLDER = 1500
MAX_RECIPIENTS = 25


def build_row(*, kind: str, subject: str, ts_ms: int, sender: str | None, recipients: list[str],
              entry_id: str | None) -> dict:
    """Reine Abbildung einer Mail auf eine Zeile (ohne COM, daher testbar)."""
    subject = (subject or "").strip() or "(ohne Betreff)"
    recipients = [r for r in (x.strip() for x in recipients) if r][:MAX_RECIPIENTS]
    if kind == "sent":
        first = recipients[0] if recipients else "?"
        more = f" +{len(recipients) - 1}" if len(recipients) > 1 else ""
        title, category, folder = f"An {first}{more}: {subject}", "mail_sent", "Gesendete Elemente"
    else:
        title, category, folder = f"Von {sender or '?'}: {subject}", "mail_received", "Posteingang"
    return {"ext_id": entry_id or f"{kind}-{ts_ms}", "ts_start": ts_ms, "ts_end": ts_ms, "subject": title,
            "location": None, "organizer": sender or None, "attendees": "; ".join(recipients) or None,
            "category": category, "extra": json.dumps({"folder": folder}, ensure_ascii=False)}


def _restrict_date(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y %I:%M %p")


class OutlookMail:
    """Liest Mails aus dem klassischen Outlook. Instanzen sind an einen Thread gebunden (COM-Apartment)."""

    def fetch(self, start: date, end: date, *, sent: bool = True, received: bool = True) -> list[dict]:
        if sys.platform != "win32":
            return []
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:  # pragma: no cover
            raise OutlookError(f"pywin32 fehlt: {e}") from e

        window_start = datetime(start.year, start.month, start.day)
        window_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
        lo, hi = int(window_start.timestamp() * 1000), int(window_end.timestamp() * 1000)
        pythoncom.CoInitialize()
        try:
            namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            rows: list[dict] = []
            for wanted, folder_id, field, kind in ((sent, OL_FOLDER_SENT, "SentOn", "sent"),
                                                   (received, OL_FOLDER_INBOX, "ReceivedTime", "received")):
                if wanted:
                    rows.extend(self._folder(namespace.GetDefaultFolder(folder_id), field, kind, window_start,
                                             window_end, lo, hi))
            log.info("Outlook-Mails: %d fuer %s..%s gelesen", len(rows), start, end)
            return rows
        except OutlookError:
            raise
        except Exception as e:  # pywintypes.com_error u. a. -> WERFEN, nicht []
            log.warning("Outlook-Mails konnten nicht gelesen werden: %s", e)
            raise OutlookError(str(e)) from e
        finally:
            pythoncom.CoUninitialize()

    def _folder(self, folder, field: str, kind: str, window_start: datetime, window_end: datetime,
                lo: int, hi: int) -> list[dict]:
        items = folder.Items
        items.Sort(f"[{field}]", True)   # neueste zuerst
        try:
            items = items.Restrict(f"[{field}] >= '{_restrict_date(window_start)}' AND "
                                   f"[{field}] < '{_restrict_date(window_end)}'")
            items.Sort(f"[{field}]", True)   # die Auswahl ebenfalls - das Abbrechen unten setzt die Reihenfolge voraus
        except Exception as e:  # Restrict ist bei manchen Spracheinstellungen zickig -> selbst filtern
            log.debug("Outlook.Restrict (Mails) fehlgeschlagen: %s", e)
        rows: list[dict] = []
        count = 0
        for item in items:
            count += 1
            if count > MAX_PER_FOLDER:
                log.warning("Outlook-Mails: Ordner nach %d Mails abgeschnitten", MAX_PER_FOLDER)
                break
            try:
                if int(getattr(item, "Class", 0) or 0) != OL_MAIL:
                    continue
                ts = _com_datetime_to_ms(getattr(item, field))
                if ts is None:
                    continue
                if ts < lo:
                    break                     # sortiert: ab hier nur noch aeltere
                if ts >= hi:
                    continue
                recipients = [r.strip() for r in str(getattr(item, "To", "") or "").split(";")]
                rows.append(build_row(kind=kind, subject=str(getattr(item, "Subject", "") or ""), ts_ms=ts,
                                      sender=str(getattr(item, "SenderName", "") or "") or None,
                                      recipients=recipients, entry_id=str(getattr(item, "EntryID", "") or "")))
            except Exception as e:  # eine defekte Mail darf den Rest nicht kippen
                log.debug("Outlook-Mail uebersprungen: %s", e)
        return rows
