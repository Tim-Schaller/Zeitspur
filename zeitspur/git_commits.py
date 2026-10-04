"""Git-Commits: eigene Commits aus lokalen Repositories - ohne Netz und ohne Konto.

Durchsucht die gewaehlten Ordner (Unterordner bis zu drei Ebenen tief) nach Repositories und liest mit
`git log --all` die Commits im Zeitraum. Eigene Commits erkennt das Plugin an Name oder E-Mail-Adresse - entweder
aus den Einstellungen oder aus der Git-Konfiguration (user.name, user.email) des jeweiligen Repositories.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE = "git"
MAX_DEPTH = 3
MAX_REPOS = 300
TIMEOUT_S = 30
SKIP_DIRS = {"node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", "bin", "obj",
             ".tox", ".idea", ".vscode", "packages", "vendor"}
_GIT_PATHS = (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe")
_SEP, _END = "\x1f", "\x1e"


@dataclass(frozen=True)
class Commit:
    hash: str
    ts_ms: int
    author: str
    email: str
    subject: str


def find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
    for candidate in (*_GIT_PATHS, str(local)):
        if Path(candidate).is_file():
            return candidate
    return None


def find_repos(folders: list[str], max_depth: int = MAX_DEPTH) -> list[Path]:
    """Repositories in den Ordnern (ein Repository wird nicht weiter durchsucht)."""
    repos: list[Path] = []
    for folder in folders:
        queue = [(Path(folder), 0)]
        while queue and len(repos) < MAX_REPOS:
            path, depth = queue.pop(0)
            if (path / ".git").exists():
                repos.append(path)
                continue
            if depth >= max_depth:
                continue
            try:
                children = sorted(p for p in path.iterdir() if p.is_dir())
            except OSError:
                continue
            queue.extend((c, depth + 1) for c in children
                         if not c.name.startswith(".") and c.name.lower() not in SKIP_DIRS)
    # doppelt angegebene Ordner nur einmal
    seen, unique = set(), []
    for r in repos:
        key = str(r.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# Neutralisiert angreiferkontrollierte Einstellungen eines geklonten Fremd-Repos (Hooks, Pager, fsmonitor,
# externe Protokolle), egal welcher Subbefehl spaeter dazukommt - Defense-in-Depth.
_HARDEN = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=", "-c", "core.pager=cat",
           "-c", "protocol.allow=never", "-c", "safe.bareRepository=explicit"]


def _run(args: list[str]) -> str:
    if args:
        args = [args[0], *_HARDEN, *args[1:]]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0
    result = subprocess.run(args, capture_output=True, timeout=TIMEOUT_S, env=env, creationflags=flags,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise OSError(result.stderr.strip()[:200] or f"git beendet mit {result.returncode}")
    return result.stdout


def identities(git: str, repo: Path) -> set[str]:
    """user.name und user.email aus der Git-Konfiguration (Repository vor global), klein geschrieben."""
    out = set()
    for key in ("user.name", "user.email"):
        try:
            value = _run([git, "-C", str(repo), "config", "--get", key]).strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if value:
            out.add(value.lower())
    return out


def parse_log(text: str) -> list[Commit]:
    commits = []
    for record in text.split(_END):
        parts = record.strip("\r\n").split(_SEP)
        if len(parts) != 5 or not parts[0]:
            continue
        sha, ts, author, email, subject = parts
        try:
            commits.append(Commit(sha.strip(), int(ts) * 1000, author, email, subject))
        except ValueError:
            continue
    return commits


def repo_commits(git: str, repo: Path, start_ms: int, end_ms: int) -> list[Commit]:
    """Commits im Zeitraum (Autorzeit). --since/--until filtern nach Committerzeit, daher mit Puffer und Nachfilter."""
    def iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    text = _run([git, "-C", str(repo), "log", "--all", f"--since={iso(start_ms - 7 * 86_400_000)}",
                 f"--until={iso(end_ms + 7 * 86_400_000)}", f"--format=%H{_SEP}%at{_SEP}%an{_SEP}%ae{_SEP}%s{_END}"])
    return [c for c in parse_log(text) if start_ms <= c.ts_ms < end_ms]


def commit_row(repo_name: str, c: Commit) -> dict:
    return {"ext_id": c.hash, "ts_start": c.ts_ms, "ts_end": c.ts_ms, "subject": f"{repo_name}: {c.subject}"[:200],
            "location": repo_name, "organizer": c.author or None, "attendees": None, "category": "commit",
            "extra": json.dumps({"repo": repo_name, "hash": c.hash[:10]}, ensure_ascii=False)}


def fetch_commits(folders: list[str], authors: list[str], start_ms: int, end_ms: int) -> list[dict]:
    from .plugins import PluginError

    git = find_git()
    if not git:
        raise PluginError("Git ist nicht installiert.")
    wanted_global = {a.strip().lower() for a in authors if a.strip()}
    rows: list[dict] = []
    failed = []
    seen: set[str] = set()   # derselbe Commit in zwei Klonen erscheint nur einmal
    for repo in find_repos(folders):
        try:
            commits = repo_commits(git, repo, start_ms, end_ms)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("Git: %s nicht lesbar: %s", repo.name, e)
            failed.append(repo.name)
            continue
        wanted = wanted_global or identities(git, repo)
        if not wanted:
            continue
        for c in commits:
            if (c.author.lower() in wanted or c.email.lower() in wanted) and c.hash not in seen:
                seen.add(c.hash)
                rows.append(commit_row(repo.name, c))
    if failed and not rows:
        raise PluginError("Repositories nicht lesbar: " + ", ".join(failed[:5]))
    return rows


def describe_repos(folders: list[str]) -> str:
    repos = find_repos(folders)
    if not repos:
        return "Keine Git-Repositories in den gewählten Ordnern gefunden."
    names = ", ".join(r.name for r in repos[:8]) + (f" und {len(repos) - 8} weitere" if len(repos) > 8 else "")
    return f"Bereit. {len(repos)} Repositories gefunden: {names}."
