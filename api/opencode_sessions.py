"""OpenCode sessions: rows of session_v2 and session_message in opencode.db, opened read-only."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .common import PATCH_FILE_LINE, TEXT_CHARS, Item, Parsed, shorten, tool_summary

SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]{8,64}$")
DEFAULT_TITLE = re.compile(r"^(New|Child) session - \d{4}-")
EDIT_TOOLS = {"edit", "multiedit", "write"}
SHELL_TOOLS = {"bash", "shell"}
READABLE = {"session_v2", "session_message"}
COLUMNS = (
    "id, parent_id, directory, title, model, cost, tokens_input, tokens_output, tokens_reasoning,"
    " tokens_cache_read, tokens_cache_write, time_created, time_updated, time_archived"
)


def only_sessions(action: int, table: str | None, *_: object) -> int:
    """SQLite authorizer: credentials and accounts share the file, so reads stop at the session tables."""
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if table in READABLE else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY


def connect(db: Path) -> sqlite3.Connection:
    """Open read-only. Without -wal and -shm next to it a read-only mount needs immutable mode."""
    uri = f"file:{quote(str(db))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.set_authorizer(only_sessions)
    try:
        conn.execute("SELECT 1 FROM session_v2 LIMIT 1")
    except sqlite3.OperationalError:
        conn.close()
        conn = sqlite3.connect(uri + "&immutable=1", uri=True)
        conn.set_authorizer(only_sessions)
        conn.execute("SELECT 1 FROM session_v2 LIMIT 1")
    conn.row_factory = sqlite3.Row
    return conn


def _json(value: object) -> dict:
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _tool(parsed: Parsed, part: dict, when: float, full: bool) -> None:
    name = str(part.get("name") or "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    data = state.get("input")
    parsed.tool_counts[name] += 1
    if isinstance(data, dict):
        if name in EDIT_TOOLS:
            parsed.add_file(data.get("path") or data.get("filePath"))
        if name == "patch" and isinstance(data.get("patchText"), str):
            for match in PATCH_FILE_LINE.finditer(data["patchText"]):
                parsed.add_file(match.group(1).strip())
        if name in SHELL_TOOLS:
            if isinstance(data.get("workdir"), str) and data["workdir"]:
                parsed.cwds.add(data["workdir"])
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            parsed.add_commits(metadata.get("output"), when)
            for block in state.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    parsed.add_commits(block.get("text"), when)
    if full:
        parsed.items.append(Item("tool", when, tool_summary(name, data), name))


def _event(parsed: Parsed, kind: str, data: dict, when: float) -> Item | None:
    if kind == "compaction":
        return Item("event", when, "Context compacted; the conversation continues from a summary")
    if kind == "model-switched":
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        parsed.add_model(model.get("id"))
        return Item("event", when, f"Model switched to {model.get('id', '?')}")
    if kind == "agent-switched":
        return Item("event", when, f"Agent switched to {data.get('agent', '?')}")
    if kind == "location-switched":
        location = data.get("location") if isinstance(data.get("location"), dict) else {}
        directory = location.get("directory")
        if isinstance(directory, str) and directory:
            parsed.cwds.add(directory)
            return Item("event", when, f"Moved to {directory}")
    return None


def parse(conn: sqlite3.Connection, row: sqlite3.Row, full: bool = False) -> Parsed:
    title = row["title"] or ""
    parsed = Parsed(
        source="opencode",
        id=row["id"],
        title="" if DEFAULT_TITLE.match(title) else title,
        directory=row["directory"] or "",
        cost=float(row["cost"] or 0),
        archived=bool(row["time_archived"]),
    )
    if parsed.directory:
        parsed.cwds.add(parsed.directory)
    parsed.add_model(_json(row["model"]).get("id"))
    parsed.add_tokens(
        input=row["tokens_input"],
        output=row["tokens_output"],
        reasoning=row["tokens_reasoning"],
        cache_read=row["tokens_cache_read"],
        cache_write=row["tokens_cache_write"],
    )
    messages = conn.execute(
        "SELECT type, time_created, data FROM session_message WHERE session_id = ? ORDER BY seq", (row["id"],)
    )
    for kind, created, raw in messages:
        when = created / 1000
        data = _json(raw)
        if kind == "user":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                parsed.touch(when)
                parsed.prompts.append(shorten(text.strip(), TEXT_CHARS))
                if full:
                    parsed.items.append(Item("user", when, parsed.prompts[-1]))
        elif kind == "assistant":
            parsed.touch(when)
            parsed.assistant_messages += 1
            model = data.get("model") if isinstance(data.get("model"), dict) else {}
            parsed.add_model(model.get("id"))
            for part in data.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    if full:
                        parsed.items.append(Item("assistant", when, shorten(part["text"], TEXT_CHARS)))
                elif part.get("type") == "tool":
                    _tool(parsed, part, when, full)
        else:
            item = _event(parsed, kind, data, when)
            if item and full:
                parsed.items.append(item)
    return parsed


class OpenCodeStore:
    """Parses each top-level session once and again only when its row changes."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self._cache: dict[str, tuple[tuple[int, int], Parsed]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.db.is_file()

    def summaries(self) -> list[Parsed]:
        result = []
        with closing(connect(self.db)) as conn:
            rows = conn.execute(f"SELECT {COLUMNS} FROM session_v2").fetchall()
            children = Counter(row["parent_id"] for row in rows if row["parent_id"])
            for row in rows:
                if row["parent_id"]:
                    continue
                key = (row["time_updated"], children[row["id"]])
                with self._lock:
                    cached = self._cache.get(row["id"])
                if cached and cached[0] == key:
                    parsed = cached[1]
                else:
                    parsed = parse(conn, row)
                    parsed.subagents = children[row["id"]]
                    with self._lock:
                        self._cache[row["id"]] = (key, parsed)
                if parsed.prompts or parsed.assistant_messages:
                    result.append(parsed)
        with self._lock:
            for gone in set(self._cache) - {row["id"] for row in rows}:
                del self._cache[gone]
        return result

    def detail(self, session_id: str) -> Parsed | None:
        if not SESSION_ID.match(session_id):
            return None
        with closing(connect(self.db)) as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
            if row is None or row["parent_id"]:
                return None
            parsed = parse(conn, row, full=True)
            parsed.subagents = conn.execute(
                "SELECT COUNT(*) FROM session_v2 WHERE parent_id = ?", (session_id,)
            ).fetchone()[0]
            return parsed
