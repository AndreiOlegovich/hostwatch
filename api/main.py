"""Hostwatch: local dashboard for Claude Code and OpenCode sessions, disks and USB cards."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .claude_sessions import ClaudeStore
from .commits import GitHistory, session_commits
from .common import Commit, Parsed, iso, resume_command, segments, topics
from .host import HostAgent, HostError
from .opencode_sessions import OpenCodeStore

Source = Literal["claude", "opencode"]
# Unmounting waits for writes still on their way to the card, which can take minutes.
ACTION_TIMEOUT = 660.0


@dataclass(frozen=True)
class Settings:
    claude_dir: Path
    opencode_db: Path
    ui_dist: Path
    agent_socket: Path = Path("/run/hostwatch/agent.sock")
    home: str = ""
    gap: float = 3600.0
    grace: float = 1800.0
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])

    @classmethod
    def from_env(cls) -> Settings:
        home = os.environ.get("HOST_HOME") or str(Path.home())
        return cls(
            claude_dir=Path(os.environ.get("CLAUDE_PROJECTS_DIR") or f"{home}/.claude/projects"),
            opencode_db=Path(os.environ.get("OPENCODE_DB") or f"{home}/.local/share/opencode/opencode.db"),
            ui_dist=Path(os.environ.get("UI_DIST") or Path(__file__).resolve().parent.parent / "ui" / "dist"),
            agent_socket=Path(os.environ.get("AGENT_SOCKET") or "/run/hostwatch/agent.sock"),
            home=home,
            gap=float(os.environ.get("SEGMENT_GAP_MINUTES", "60")) * 60,
            grace=float(os.environ.get("COMMIT_GRACE_MINUTES", "30")) * 60,
            allowed_hosts=os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(","),
        )


class DeviceBody(BaseModel):
    device: str
    readonly: bool = False


class CardBody(BaseModel):
    serial: str


class ReadonlyBody(CardBody):
    readonly: bool


def same_site(request: Request) -> None:
    """Refuse changes another web page could trigger: a cross-site form can't set this header."""
    site = request.headers.get("sec-fetch-site", "same-origin")
    if request.headers.get("x-hostwatch") != "1" or site not in ("same-origin", "none"):
        raise HTTPException(403, "Refused: the request did not come from the Hostwatch page")


def summary(parsed: Parsed, commits: list[Commit], gap: float) -> dict[str, object]:
    runs = segments(parsed.times, gap)
    topic_list = topics(parsed.prompts)
    return {
        "source": parsed.source,
        "id": parsed.id,
        "title": parsed.title or (topic_list[0] if topic_list else "(no prompt)"),
        "directory": parsed.directory,
        "branch": parsed.branch,
        "started": iso(runs[0][0]) if runs else None,
        "ended": iso(runs[-1][1]) if runs else None,
        "active_seconds": int(sum(end - start for start, end in runs)),
        "prompts": len(parsed.prompts),
        "responses": parsed.assistant_messages,
        "tool_calls": sum(parsed.tool_counts.values()),
        "topics": topic_list,
        "commits": len(commits),
        "agent_commits": sum(1 for commit in commits if commit.in_transcript),
        "files_changed": len(parsed.files),
        "models": parsed.models,
        "tokens": parsed.tokens,
        "cost": parsed.cost,
        "subagents": parsed.subagents,
        "archived": parsed.archived,
        "resume": resume_command(parsed.source, parsed.directory, parsed.id),
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Hostwatch", docs_url=None, redoc_url=None, openapi_url=None)
    # Localhost only: a page on another site that rebinds its DNS to 127.0.0.1 gets a 400.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    stores = {"claude": ClaudeStore(settings.claude_dir), "opencode": OpenCodeStore(settings.opencode_db)}
    paths = {"claude": settings.claude_dir, "opencode": settings.opencode_db}
    history = GitHistory()
    agent = HostAgent(settings.agent_socket)

    def commits_of(parsed: Parsed) -> list[Commit]:
        return session_commits(parsed, history, settings.gap, settings.grace)

    # Plain def endpoints: FastAPI runs them in its thread pool, so parsing does not block the loop.
    @app.get("/api/info")
    def info() -> dict[str, object]:
        return {
            "home": settings.home,
            "grace_minutes": settings.grace / 60,
            "sources": {
                name: {"path": str(paths[name]), "available": store.available()} for name, store in stores.items()
            },
            "host_agent": {"path": str(settings.agent_socket), "available": agent.available()},
        }

    @app.get("/api/sessions/{source}")
    def list_sessions(source: Source) -> dict[str, object]:
        store = stores[source]
        if not store.available():
            return {"sessions": [], "error": f"{paths[source]} is not there (not mounted?)"}
        try:
            parsed = store.summaries()
        except (OSError, sqlite3.Error) as exc:
            return {"sessions": [], "error": f"cannot read {paths[source]}: {exc}"}
        parsed.sort(key=lambda p: max(p.times, default=0), reverse=True)
        return {"sessions": [summary(p, commits_of(p), settings.gap) for p in parsed], "error": None}

    @app.get("/api/sessions/{source}/{session_id}")
    def session_detail(source: Source, session_id: str) -> dict[str, object]:
        try:
            parsed = stores[source].detail(session_id) if stores[source].available() else None
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, f"cannot read {paths[source]}: {exc}") from exc
        if parsed is None:
            raise HTTPException(404, "No such session")
        commits = commits_of(parsed)
        data = summary(parsed, commits, settings.gap)
        data.update(
            {
                "commit_list": [commit.to_json() for commit in commits],
                "files": parsed.files,
                "tools": parsed.tool_counts.most_common(),
                "cwds": sorted(parsed.cwds),
                "transcript": [item.to_json() for item in parsed.items],
            }
        )
        return data

    def host(method: str, path: str, body: BaseModel | None = None, timeout: float = 30) -> dict:
        try:
            return agent.call(method, path, body.model_dump() if body else None, timeout)
        except HostError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/disks")
    def disks() -> dict:
        return host("GET", "/disks")

    @app.get("/api/cards")
    def cards() -> dict:
        return host("GET", "/cards")

    @app.post("/api/mount", dependencies=[Depends(same_site)])
    def mount(body: DeviceBody) -> dict:
        return host("POST", "/mount", body, ACTION_TIMEOUT)

    @app.post("/api/unmount", dependencies=[Depends(same_site)])
    def unmount(body: DeviceBody) -> dict:
        return host("POST", "/unmount", body, ACTION_TIMEOUT)

    @app.post("/api/cards/readonly", dependencies=[Depends(same_site)])
    def card_readonly(body: ReadonlyBody) -> dict:
        return host("POST", "/readonly", body, ACTION_TIMEOUT)

    @app.post("/api/cards/eject", dependencies=[Depends(same_site)])
    def card_eject(body: CardBody) -> dict:
        return host("POST", "/eject", body, ACTION_TIMEOUT)

    if settings.ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_dist, html=True), name="ui")
    return app


app = create_app(Settings.from_env())
