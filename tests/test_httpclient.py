"""Gemeinsamer HTTPS-Zugang der Online-Plugins: nur https, Zertifikate immer pruefen, keine Geheimnisse in Meldungen."""
import io
import ssl
import urllib.error
import urllib.request

import pytest

from zeitspur import httpclient, winutil


def test_nur_https_und_webcal_wird_https():
    assert httpclient.normalize_url(" webcal://kal.example.org/a.ics ") == "https://kal.example.org/a.ics"
    assert httpclient.normalize_url("https://x.example.org/") == "https://x.example.org/"
    for bad in ("http://x.example.org/", "ftp://x", "https://", "kein link"):
        with pytest.raises(ValueError):
            httpclient.normalize_url(bad)


def test_zertifikatspruefung_wird_nie_abgeschaltet():
    ctx = winutil.https_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    handler = next(h for h in httpclient._opener().handlers if isinstance(h, urllib.request.HTTPSHandler))
    assert handler._context.verify_mode == ssl.CERT_REQUIRED


def _redirect(old_url: str, new_url: str, headers: dict):
    req = urllib.request.Request(old_url, headers=headers)
    return httpclient._SafeRedirect().redirect_request(req, None, 302, "Found", {}, new_url)


def test_weiterleitung_auf_anderen_host_ohne_token():
    new = _redirect("https://api.example.org/a", "https://cdn.example.net/b", {"Authorization": "Bearer geheim"})
    assert not new.has_header("Authorization")
    same = _redirect("https://api.example.org/a", "https://api.example.org/b", {"Authorization": "Bearer geheim"})
    assert same.get_header("Authorization") == "Bearer geheim"


def test_weiterleitung_auf_http_wird_abgelehnt():
    with pytest.raises(httpclient.HttpError, match="unverschlüsselt"):
        _redirect("https://api.example.org/a", "http://api.example.org/b", {})


class _Opener:
    """Spielt Antworten nacheinander ab: bytes = Inhalt, int = HTTP-Fehler, Exception = wird geworfen."""

    def __init__(self, *answers, retry_after="0"):
        self.answers = list(answers)
        self.requests = []
        self.retry_after = retry_after

    def open(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "x", {"Retry-After": self.retry_after}, io.BytesIO())
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


SECRET_URL = "https://kal.example.org/private-abc123geheim/basic.ics"


def test_fehlermeldungen_nennen_nie_die_geheime_adresse():
    for status in (401, 404, 500):
        with pytest.raises(httpclient.HttpError) as err:
            httpclient.request(SECRET_URL, opener=_Opener(status))
        assert "geheim" not in str(err.value) and err.value.status == status
    with pytest.raises(httpclient.HttpError) as err:
        httpclient.request(SECRET_URL, opener=_Opener(urllib.error.URLError(OSError(11001, "Host nicht gefunden"))))
    assert "kal.example.org" in str(err.value) and "geheim" not in str(err.value)


def test_wiederholt_bei_zu_vielen_anfragen():
    opener = _Opener(429, b"ok")
    assert httpclient.request(SECRET_URL, opener=opener) == b"ok"
    assert len(opener.requests) == 2
    with pytest.raises(httpclient.HttpError, match="429"):
        httpclient.request(SECRET_URL, opener=_Opener(429, 429, 429))


def test_zu_grosse_antwort_wird_abgelehnt():
    with pytest.raises(httpclient.HttpError, match="zu groß"):
        httpclient.request(SECRET_URL, opener=_Opener(b"x" * 101), max_bytes=100)


def test_json_und_kennung():
    opener = _Opener(b'{"a": 1}')
    assert httpclient.get_json("https://api.example.org/x", params={"p": 2}, opener=opener) == {"a": 1}
    req = opener.requests[0]
    assert req.full_url.endswith("/x?p=2") and req.get_header("User-agent").startswith("Zeitspur/")
    with pytest.raises(httpclient.HttpError, match="kein gültiges JSON"):
        httpclient.get_json("https://api.example.org/x", opener=_Opener(b"<html>"))
