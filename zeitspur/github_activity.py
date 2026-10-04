"""GitHub-Aktivitaet eines Kontos: Pushes, Pull Requests, Issues, Reviews, Kommentare, Releases.

Quelle ist die Events-Schnittstelle (GET /users/<name>/events). Ohne Token nur oeffentliche Aktivitaet, mit einem
persoenlichen Zugriffstoken desselben Kontos auch private Repositories. GitHub liefert hoechstens die letzten
90 Tage bzw. 300 Ereignisse. Der Token steht nur im Authorization-Header (httpclient laesst ihn bei einer
Weiterleitung auf einen anderen Host weg).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import httpclient

log = logging.getLogger(__name__)

SOURCE = "github"
API = "https://api.github.com"
PAGES = 3
PER_PAGE = 100
MAX_COMMITS = 5

_VERBS = {"opened": "geöffnet", "closed": "geschlossen", "reopened": "wieder geöffnet", "edited": "bearbeitet",
          "ready_for_review": "zur Prüfung bereit", "converted_to_draft": "zum Entwurf gemacht",
          "assigned": "zugewiesen", "labeled": "markiert", "created": "angelegt", "published": "veröffentlicht"}
_REVIEW = {"approved": "freigegeben", "changes_requested": "Änderungen erbeten", "commented": "kommentiert"}


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def describe_event(ev: dict) -> tuple[str, dict]:
    """GitHub-Ereignis -> (Betreff, Zusatzangaben)."""
    kind = ev.get("type") or ""
    repo = (ev.get("repo") or {}).get("name") or "?"
    p = ev.get("payload") or {}
    extra: dict = {"type": kind}
    if kind == "PushEvent":
        branch = str(p.get("ref") or "").removeprefix("refs/heads/")
        commits = p.get("commits") or []
        n = p.get("size") or p.get("distinct_size") or len(commits)
        messages = [str(c.get("message") or "").splitlines()[0] for c in commits[:MAX_COMMITS] if c.get("message")]
        if messages:
            extra["commits"] = messages
        detail = ", ".join(x for x in (branch, f"{n} Commit{'s' if n != 1 else ''}" if n else "") if x)
        return f"Push nach {repo}" + (f" ({detail})" if detail else ""), extra
    if kind in ("PullRequestEvent", "PullRequestReviewEvent", "PullRequestReviewCommentEvent"):
        pr = p.get("pull_request") or {}
        ref = f"{repo}#{pr.get('number', '?')}"
        title = pr.get("title") or ""
        if pr.get("html_url"):
            extra["url"] = pr["html_url"]
        if kind == "PullRequestEvent":
            action = p.get("action") or ""
            verb = "gemergt" if action == "closed" and pr.get("merged") else _VERBS.get(action, action)
            return f"Pull Request {verb}: {title} ({ref})", extra
        if kind == "PullRequestReviewEvent":
            state = ((p.get("review") or {}).get("state") or "").lower()
            return f"Review ({_REVIEW.get(state, state or 'abgegeben')}): {title} ({ref})", extra
        return f"Review-Kommentar: {title} ({ref})", extra
    if kind in ("IssuesEvent", "IssueCommentEvent"):
        issue = p.get("issue") or {}
        ref = f"{repo}#{issue.get('number', '?')}"
        if issue.get("html_url"):
            extra["url"] = issue["html_url"]
        if kind == "IssuesEvent":
            action = p.get("action") or ""
            return f"Issue {_VERBS.get(action, action)}: {issue.get('title', '')} ({ref})", extra
        return f"Kommentar: {issue.get('title', '')} ({ref})", extra
    if kind == "CreateEvent":
        ref_type, ref = p.get("ref_type") or "", p.get("ref") or ""
        if ref_type == "repository":
            return f"Repository angelegt: {repo}", extra
        return f"{'Branch' if ref_type == 'branch' else 'Tag'} {ref} angelegt ({repo})", extra
    if kind == "DeleteEvent":
        return f"{'Branch' if p.get('ref_type') == 'branch' else 'Tag'} {p.get('ref', '')} gelöscht ({repo})", extra
    if kind == "ReleaseEvent":
        release = p.get("release") or {}
        if release.get("html_url"):
            extra["url"] = release["html_url"]
        return f"Release {release.get('name') or release.get('tag_name') or ''} veröffentlicht ({repo})", extra
    simple = {"ForkEvent": f"Fork von {repo}", "WatchEvent": f"Stern für {repo}", "PublicEvent": f"{repo} öffentlich gemacht",
              "MemberEvent": f"Mitglied hinzugefügt ({repo})", "GollumEvent": f"Wiki bearbeitet ({repo})",
              "CommitCommentEvent": f"Commit-Kommentar ({repo})"}
    return simple.get(kind, f"{kind.removesuffix('Event') or 'Ereignis'} ({repo})"), extra


def event_row(ev: dict) -> dict | None:
    try:
        ts = _ts(ev["created_at"])
    except (KeyError, ValueError, TypeError):
        return None
    subject, extra = describe_event(ev)
    repo = (ev.get("repo") or {}).get("name")
    return {"ext_id": str(ev.get("id") or f"gh-{ts}"), "ts_start": ts, "ts_end": ts, "subject": subject[:200],
            "location": repo, "organizer": None, "attendees": None, "category": "github",
            "extra": json.dumps(extra, ensure_ascii=False)}


def fetch_activity(username: str, token: str | None, start_ms: int, end_ms: int, get=httpclient.get_json) -> list[dict]:
    from .plugins import PluginError

    rows: list[dict] = []
    for page in range(1, PAGES + 1):
        try:
            events = get(f"{API}/users/{username}/events", headers=_headers(token),
                         params={"per_page": PER_PAGE, "page": page})
        except httpclient.HttpError as e:
            raise PluginError(f"GitHub: {e}") from None
        if not isinstance(events, list) or not events:
            break
        oldest = None
        for ev in events:
            row = event_row(ev)
            if row is None:
                continue
            oldest = row["ts_start"] if oldest is None else min(oldest, row["ts_start"])
            if start_ms <= row["ts_start"] < end_ms:
                rows.append(row)
        if len(events) < PER_PAGE or (oldest is not None and oldest < start_ms):
            break
    return rows


def test_account(username: str, token: str | None, get=httpclient.get_json) -> str:
    from .plugins import PluginError

    try:
        user = get(f"{API}/users/{username}", headers=_headers(None))
    except httpclient.HttpError as e:
        if e.status == 404:
            raise PluginError(f"GitHub-Konto „{username}“ gibt es nicht.") from None
        raise PluginError(f"GitHub: {e}") from None
    text = f"GitHub-Konto „{user.get('login', username)}“ gefunden"
    if token:
        try:
            me = get(f"{API}/user", headers=_headers(token))
        except httpclient.HttpError as e:
            raise PluginError(f"Token abgelehnt: {e}") from None
        login = me.get("login", "")
        if login.lower() != username.lower():
            return text + f", aber der Token gehört zu „{login}“ – private Aktivität fehlt dann."
        return text + ", Token passt (auch private Repositories)."
    return text + " (ohne Token: nur öffentliche Aktivität)."
