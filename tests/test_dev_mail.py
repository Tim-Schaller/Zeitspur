"""Outlook-Mails, Git-Commits und GitHub-Aktivitaet - ohne echtes Outlook und ohne Netz."""
import json
import os
import subprocess
from datetime import datetime

import pytest

from zeitspur import git_commits as gc
from zeitspur import github_activity as gh
from zeitspur import httpclient
from zeitspur import outlook_mail as om
from zeitspur.plugins import PluginError

T = 1_790_000_000_000


# --------------------------------------------------------------------------- Outlook-Mails

def test_mailzeilen():
    sent = om.build_row(kind="sent", subject="Angebot", ts_ms=T, sender="Ich",
                        recipients=["Erika Musterfrau", " ", "Max Mustermann"], entry_id="E1")
    assert sent["subject"] == "An Erika Musterfrau +1: Angebot" and sent["category"] == "mail_sent"
    assert sent["attendees"] == "Erika Musterfrau; Max Mustermann" and sent["ts_end"] == T
    got = om.build_row(kind="received", subject="", ts_ms=T, sender="Max Mustermann", recipients=[], entry_id=None)
    assert got["subject"] == "Von Max Mustermann: (ohne Betreff)" and got["category"] == "mail_received"
    assert json.loads(got["extra"]) == {"folder": "Posteingang"}


class _Item:
    def __init__(self, cls, when, subject, to="Erika Musterfrau", sender="Ich", eid="E"):
        self.Class, self.SentOn, self.Subject, self.To, self.SenderName, self.EntryID = cls, when, subject, to, sender, eid


class _Items(list):
    def Sort(self, *args):
        pass

    def Restrict(self, query):
        return self


class _Folder:
    def __init__(self, items):
        self.Items = _Items(items)


def test_ordner_lesen_neueste_zuerst_nur_mails_im_fenster():
    start, end = datetime(2026, 10, 5), datetime(2026, 10, 6)
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    items = [_Item(om.OL_MAIL, datetime(2026, 10, 6, 9).astimezone(), "zu neu"),
             _Item(53, datetime(2026, 10, 5, 12).astimezone(), "Besprechungsanfrage"),     # keine Mail
             _Item(om.OL_MAIL, datetime(2026, 10, 5, 11).astimezone(), "Bericht", eid="E2"),
             _Item(om.OL_MAIL, datetime(2026, 10, 4, 18).astimezone(), "zu alt"),
             _Item(om.OL_MAIL, datetime(2026, 10, 5, 8).astimezone(), "nach dem Abbruch")]
    rows = om.OutlookMail()._folder(_Folder(items), "SentOn", "sent", start, end, lo, hi)
    assert [r["subject"] for r in rows] == ["An Erika Musterfrau: Bericht"] and rows[0]["ext_id"] == "E2"


# --------------------------------------------------------------------------- Git

def test_git_log_lesen():
    text = f"abc{gc._SEP}1790000000{gc._SEP}Max{gc._SEP}max@beispiel.de{gc._SEP}Erster Commit{gc._END}\n" \
           f"kaputt{gc._END}"
    [c] = gc.parse_log(text)
    assert c == gc.Commit("abc", 1_790_000_000_000, "Max", "max@beispiel.de", "Erster Commit")


def test_repositories_finden(tmp_path):
    for repo in ("a", "gruppe/b", "gruppe/node_modules/c", ".versteckt/d", "x/y/z/zu-tief"):
        (tmp_path / repo / ".git").mkdir(parents=True)
    (tmp_path / "a" / "unter" / ".git").mkdir(parents=True)          # in einem Repository wird nicht gesucht
    names = [p.name for p in gc.find_repos([str(tmp_path), str(tmp_path / "a")])]
    assert names == ["a", "b"]


def _git(repo, *args, when="2026-10-01T10:00:00+02:00"):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run([gc.find_git(), "-C", str(repo), "-c", "commit.gpgsign=false", *args], check=True,
                   capture_output=True, env=env)


@pytest.mark.skipif(gc.find_git() is None, reason="Git nicht installiert")
def test_eigene_commits_aus_echtem_repository(tmp_path):
    repo = tmp_path / "projekte" / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Max Mustermann")
    _git(repo, "config", "user.email", "max@beispiel.de")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "Erster Commit")
    _git(repo, "-c", "user.name=Erika", "-c", "user.email=erika@beispiel.de", "commit", "-q", "--allow-empty",
         "-m", "Fremder Commit", when="2026-10-01T11:00:00+02:00")
    start = int(datetime(2026, 9, 30).timestamp() * 1000)
    end = int(datetime(2026, 10, 3).timestamp() * 1000)
    rows = gc.fetch_commits([str(tmp_path)], [], start, end)
    assert [r["subject"] for r in rows] == ["app: Erster Commit"]       # nur eigene (aus der Git-Konfiguration)
    assert rows[0]["category"] == "commit" and json.loads(rows[0]["extra"])["repo"] == "app"
    assert len(gc.fetch_commits([str(tmp_path)], ["erika@beispiel.de", "max mustermann"], start, end)) == 2
    assert gc.fetch_commits([str(tmp_path)], [], end, end + 1000) == []
    assert "1 Repositories gefunden: app" in gc.describe_repos([str(tmp_path)])


# --------------------------------------------------------------------------- GitHub

def _ev(kind, payload=None, created="2026-10-04T12:00:00Z", repo="Tim-Schaller/Zeitspur", eid="1"):
    return {"id": eid, "type": kind, "repo": {"name": repo}, "payload": payload or {}, "created_at": created}


@pytest.mark.parametrize("ev,subject", [
    (_ev("PushEvent", {"ref": "refs/heads/main", "size": 2, "commits": [{"message": "Fix\n\nDetails"}]}),
     "Push nach Tim-Schaller/Zeitspur (main, 2 Commits)"),
    (_ev("PullRequestEvent", {"action": "closed", "pull_request": {"number": 7, "title": "Plugins", "merged": True}}),
     "Pull Request gemergt: Plugins (Tim-Schaller/Zeitspur#7)"),
    (_ev("PullRequestReviewEvent", {"review": {"state": "approved"}, "pull_request": {"number": 7, "title": "P"}}),
     "Review (freigegeben): P (Tim-Schaller/Zeitspur#7)"),
    (_ev("IssuesEvent", {"action": "opened", "issue": {"number": 3, "title": "Bug"}}),
     "Issue geöffnet: Bug (Tim-Schaller/Zeitspur#3)"),
    (_ev("IssueCommentEvent", {"issue": {"number": 3, "title": "Bug"}}), "Kommentar: Bug (Tim-Schaller/Zeitspur#3)"),
    (_ev("CreateEvent", {"ref_type": "repository"}), "Repository angelegt: Tim-Schaller/Zeitspur"),
    (_ev("CreateEvent", {"ref_type": "tag", "ref": "v0.4.0"}), "Tag v0.4.0 angelegt (Tim-Schaller/Zeitspur)"),
    (_ev("DeleteEvent", {"ref_type": "branch", "ref": "alt"}), "Branch alt gelöscht (Tim-Schaller/Zeitspur)"),
    (_ev("ReleaseEvent", {"release": {"name": "Zeitspur 0.4.0"}}),
     "Release Zeitspur 0.4.0 veröffentlicht (Tim-Schaller/Zeitspur)"),
    (_ev("WatchEvent"), "Stern für Tim-Schaller/Zeitspur"),
    (_ev("SponsorshipEvent"), "Sponsorship (Tim-Schaller/Zeitspur)"),
])
def test_github_ereignisse_beschreiben(ev, subject):
    assert gh.describe_event(ev)[0] == subject


def test_push_merkt_sich_commit_nachrichten():
    _, extra = gh.describe_event(_ev("PushEvent", {"commits": [{"message": "Fix\n\nDetails"}]}))
    assert extra["commits"] == ["Fix"]


def test_github_seitenweise_bis_zum_fenster():
    t0 = int(datetime(2026, 10, 1).timestamp() * 1000)
    calls = []
    page1 = [_ev("WatchEvent", created="2026-10-04T12:00:00Z", eid=str(i)) for i in range(100)]
    page2 = [_ev("WatchEvent", created="2026-10-02T12:00:00Z", eid="a"),
             _ev("WatchEvent", created="2026-09-20T12:00:00Z", eid="b")]

    def get(url, headers=None, params=None):
        calls.append((url, headers, params))
        return {1: page1, 2: page2}.get(params["page"], [])
    rows = gh.fetch_activity("octocat", "geheim", t0, t0 + 10 * 86_400_000, get=get)
    assert len(rows) == 101 and len(calls) == 2                        # Seite 3 nicht mehr noetig
    assert calls[0][0].endswith("/users/octocat/events") and calls[0][1]["Authorization"] == "Bearer geheim"
    assert "Authorization" not in gh._headers(None)


def test_github_fehler_und_konto_pruefen():
    def boom(url, headers=None, params=None):
        raise httpclient.HttpError("Nicht gefunden (HTTP 404)", 404)
    with pytest.raises(PluginError, match="gibt es nicht"):
        gh.test_account("niemand", None, get=boom)
    with pytest.raises(PluginError, match="GitHub"):
        gh.fetch_activity("x", None, 0, 1, get=boom)

    def get(url, headers=None, params=None):
        return {"login": "andere"} if url.endswith("/user") else {"login": "octocat"}
    assert "Token gehört zu „andere“" in gh.test_account("octocat", "t", get=get)
    assert "nur öffentliche" in gh.test_account("octocat", None, get=get)
