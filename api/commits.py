"""Commits per session: git's commit lines in the transcript plus repository history in the active hours.

The agent rarely commits itself, so a commit made in a terminal while the session ran, or within
the grace period after it went quiet, counts too. Each commit says which way it was found.
"""

from __future__ import annotations

import bisect
import os
import subprocess
import threading
import time
from dataclasses import replace

from .common import Commit, Parsed, segments

LOG_FORMAT = "--format=%H%x1f%ct%x1f%an%x1f%s"


def _git(cwd: str, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class GitHistory:
    """Repository roots and branch history, cached; history is re-read after `ttl` seconds."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._tops: dict[str, str | None] = {}
        self._logs: dict[str, tuple[float, list[Commit], list[float]]] = {}
        self._lock = threading.Lock()

    def toplevel(self, path: str) -> str | None:
        with self._lock:
            if path in self._tops:
                return self._tops[path]
        top = None
        if os.path.isdir(path):
            out = _git(path, "rev-parse", "--show-toplevel")
            top = out.strip() if out and out.strip() else None
        with self._lock:
            self._tops[path] = top
        return top

    def log(self, repo: str) -> tuple[list[Commit], list[float]]:
        """All commits on local branches, oldest first, with their times for bisecting."""
        now = time.monotonic()
        with self._lock:
            cached = self._logs.get(repo)
        if cached and now - cached[0] < self.ttl:
            return cached[1], cached[2]
        commits = []
        for line in (_git(repo, "log", "--branches", "--no-color", LOG_FORMAT) or "").splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4 and parts[1].isdigit():
                commits.append(Commit(parts[0], parts[3], float(parts[1]), author=parts[2], repo=repo))
        commits.sort(key=lambda commit: commit.time)
        times = [commit.time for commit in commits]
        with self._lock:
            self._logs[repo] = (now, commits, times)
        return commits, times


def session_commits(parsed: Parsed, history: GitHistory, gap: float, grace: float) -> list[Commit]:
    runs = segments(parsed.times, gap)
    repos = sorted({top for cwd in parsed.cwds if (top := history.toplevel(cwd))})
    logs = {repo: history.log(repo) for repo in repos}
    found: dict[str, Commit] = {}
    for commits, times in logs.values():
        for start, end in runs:
            for index in range(bisect.bisect_left(times, start), bisect.bisect_right(times, end + grace)):
                found[commits[index].hash] = replace(commits[index], in_window=True)
    for seen in parsed.transcript_commits:
        match = next((commit for commit in found.values() if commit.hash.startswith(seen.hash)), None)
        if match is None:
            match = next(
                (replace(c) for commits, _ in logs.values() for c in commits if c.hash.startswith(seen.hash)),
                None,
            )
            if match is not None:
                found[match.hash] = match
        if match is not None:
            match.in_transcript = True
        else:
            # Amended or rebased away since, or made in a repository that is not mounted.
            found[seen.hash] = replace(seen)
    return sorted(found.values(), key=lambda commit: commit.time)
