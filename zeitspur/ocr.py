"""Texterkennung mit dem gebuendelten tesseract.exe - ausschliesslich im Speicher.

Das Bild wird als PNG-Bytes ueber stdin uebergeben ("tesseract - -"), das Ergebnis (TSV) ueber
stdout gelesen. Es entsteht zu keinem Zeitpunkt eine Bilddatei auf der Platte (im Gegensatz zu
pytesseract, das intern NamedTemporaryFile verwendet).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ENV_TESSERACT = "ZEITSPUR_TESSERACT"


class OcrError(Exception):
    pass


@dataclass
class OcrResult:
    text: str
    confidence: float | None
    words: int


def parse_tsv(tsv: str, min_confidence: float) -> OcrResult:
    """Tesseract-TSV -> Text. Nur Woerter (level 5) mit conf >= min_confidence; Zeilen bleiben erhalten,
    Bloecke werden durch Leerzeilen getrennt."""
    lines_out: list[str] = []
    confs: list[float] = []
    current_key: tuple[str, str, str, str] | None = None
    current_block: str | None = None
    current_words: list[str] = []

    def flush() -> None:
        nonlocal current_words
        if current_words:
            lines_out.append(" ".join(current_words))
            current_words = []

    for raw in tsv.splitlines():
        parts = raw.split("\t")
        if len(parts) != 12 or parts[0] == "level":
            continue
        level, page, block, par, line = parts[0], parts[1], parts[2], parts[3], parts[4]
        if level != "5":
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        word = parts[11].strip()
        if not word or conf < min_confidence:
            continue
        key = (page, block, par, line)
        if key != current_key:
            flush()
            if current_block is not None and block != current_block and lines_out:
                lines_out.append("")
            current_key, current_block = key, block
        current_words.append(word)
        confs.append(conf)
    flush()
    text = "\n".join(lines_out).strip()
    return OcrResult(text=text, confidence=(sum(confs) / len(confs)) if confs else None, words=len(confs))


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get(ENV_TESSERACT)
    if env:
        p = Path(env)
        dirs.append(p if p.is_dir() else p.parent)
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "tesseract")
    dirs.append(Path(__file__).resolve().parents[1] / "installer" / "tesseract-portable")
    return dirs


def locate_tesseract() -> Path | None:
    """Findet tesseract.exe: Umgebungsvariable, neben der EXE (gepackt) oder installer/tesseract-portable."""
    for d in _candidate_dirs():
        exe = d / "tesseract.exe"
        if exe.exists():
            return exe
    return None


class TesseractEngine:
    def __init__(self, exe: Path | None = None, *, tessdata_dir: Path | None = None, lang: str = "deu+eng",
                 psm: int = 3, min_confidence: float = 40.0, timeout: float = 120.0):
        self.exe = Path(exe) if exe else locate_tesseract()
        self.tessdata_dir = Path(tessdata_dir) if tessdata_dir else (self.exe.parent / "tessdata" if self.exe else None)
        self.lang = lang
        self.psm = int(psm)
        self.min_confidence = float(min_confidence)
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._stopping = False
        self._version: str | None = None

    def available(self) -> bool:
        return bool(self.exe and self.exe.exists() and self.tessdata_dir and self.tessdata_dir.exists())

    def missing_languages(self) -> list[str]:
        if not self.tessdata_dir:
            return self.lang.split("+")
        return [l for l in self.lang.split("+") if not (self.tessdata_dir / f"{l}.traineddata").exists()]

    def version(self) -> str:
        """Versionszeile von tesseract.exe; wird gecacht, damit Statusabfragen keinen Prozess starten."""
        if self._version is not None:
            return self._version
        if not self.exe or not self.exe.exists():
            return ""
        try:
            r = subprocess.run([str(self.exe), "--version"], capture_output=True, text=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            first = (r.stdout or r.stderr).strip().splitlines()
            self._version = first[0] if first else ""
        except (OSError, subprocess.SubprocessError):
            self._version = ""
        return self._version

    def _command(self) -> list[str]:
        assert self.exe is not None
        cmd = [str(self.exe), "-", "-", "-l", self.lang, "--psm", str(self.psm)]
        if self.tessdata_dir:
            cmd += ["--tessdata-dir", str(self.tessdata_dir)]
        cmd.append("tsv")
        return cmd

    def recognize(self, image_bytes: bytes) -> OcrResult:
        """OCR eines PNG/JPEG/WebP-Bildes (Bytes im Speicher)."""
        if not self.available():
            raise OcrError("tesseract.exe oder tessdata nicht gefunden")
        env = os.environ.copy()
        env["OMP_THREAD_LIMIT"] = "1"  # ein Kern, keine CPU-Spitzen ueber alle Kerne
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
        try:
            proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, **kwargs)
        except OSError as e:
            raise OcrError(f"tesseract konnte nicht gestartet werden: {e}") from e
        with self._proc_lock:
            if self._stopping:
                # kill() lief im Fenster zwischen Popen und diesem Merken -> diesen frischen Prozess selbst
                # beenden, sonst bleibt ein Zombie-tesseract.exe zurueck.
                proc.kill()
                raise OcrError("OCR wird beendet")
            self._proc = proc
        try:
            try:
                out, err = proc.communicate(image_bytes, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise OcrError(f"tesseract Timeout nach {self.timeout:.0f} s")
        finally:
            with self._proc_lock:
                self._proc = None
        if proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip().splitlines()
            raise OcrError(f"tesseract rc={proc.returncode}: {msg[-1] if msg else 'unbekannter Fehler'}")
        return parse_tsv(out.decode("utf-8", "replace"), self.min_confidence)

    def kill(self) -> None:
        """Bricht einen laufenden OCR-Aufruf ab (beim Beenden)."""
        with self._proc_lock:
            self._stopping = True   # verhindert, dass ein gerade startender Aufruf einen Zombie hinterlaesst
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
