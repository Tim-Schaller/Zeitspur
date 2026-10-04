import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from zeitspur.ocr import OcrError, TesseractEngine, locate_tesseract, parse_tsv

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def row(level, block, par, line, word, conf, text):
    return f"{level}\t1\t{block}\t{par}\t{line}\t{word}\t0\t0\t10\t10\t{conf}\t{text}"


SAMPLE_TSV = "\n".join([
    HEADER,
    row(1, 0, 0, 0, 0, -1, ""),
    row(2, 1, 0, 0, 0, -1, ""),
    row(4, 1, 1, 1, 0, -1, ""),
    row(5, 1, 1, 1, 1, 96.5, "Hallo"),
    row(5, 1, 1, 1, 2, 91.0, "Welt"),
    row(5, 1, 1, 1, 3, 12.0, "###"),          # zu geringe Konfidenz
    row(5, 1, 1, 2, 1, 88.0, "zweite"),
    row(5, 1, 1, 2, 2, 90.0, "Zeile"),
    row(5, 2, 1, 1, 1, 80.0, "Neuer"),
    row(5, 2, 1, 1, 2, 85.0, "Block"),
    row(5, 2, 1, 1, 3, 99.0, "   "),          # leer
])


def test_parse_tsv_filters_and_groups_lines_and_blocks():
    result = parse_tsv(SAMPLE_TSV, 40)
    assert result.text == "Hallo Welt\nzweite Zeile\n\nNeuer Block"
    assert result.words == 6
    assert result.confidence == pytest.approx((96.5 + 91 + 88 + 90 + 80 + 85) / 6)


def test_parse_tsv_empty_and_garbage():
    assert parse_tsv("", 40).text == ""
    assert parse_tsv("", 40).confidence is None
    assert parse_tsv("kein\ttsv", 40).words == 0
    assert parse_tsv(HEADER + "\n" + row(5, 1, 1, 1, 1, "abc", "x"), 40).words == 0


def test_engine_without_binary():
    engine = TesseractEngine(exe=Path("C:/nicht/vorhanden/tesseract.exe"))
    assert not engine.available()
    assert engine.missing_languages() == ["deu", "eng"]
    with pytest.raises(OcrError):
        engine.recognize(b"png")
    engine.kill()  # kein laufender Prozess -> kein Fehler


def render(text: str) -> bytes:
    img = Image.new("L", (1000, 140), 255)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((20, 40), text, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


needs_tesseract = pytest.mark.skipif(locate_tesseract() is None, reason="gebuendeltes tesseract.exe fehlt")


@needs_tesseract
def test_engine_recognizes_rendered_text_without_temp_files():
    tmp = os.environ.get("TEMP", "")
    before = set(os.listdir(tmp)) if tmp else set()
    engine = TesseractEngine(lang="deu+eng", psm=6, min_confidence=40)
    assert engine.available() and engine.missing_languages() == []
    assert engine.version().startswith("tesseract")
    result = engine.recognize(render("Serverumzug 4711 Müller"))
    assert "Serverumzug" in result.text and "4711" in result.text and "Müller" in result.text
    assert result.confidence and result.confidence > 60 and result.words >= 3
    after = set(os.listdir(tmp)) if tmp else set()
    assert not [f for f in after - before if f.startswith("tess_")]


@needs_tesseract
def test_engine_rejects_invalid_image():
    engine = TesseractEngine()
    with pytest.raises(OcrError):
        engine.recognize(b"das ist kein Bild")
