"""Shapes and text helpers shared by the Claude Code and OpenCode readers."""

from __future__ import annotations

import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

# git prints "[main 1a2b3c4] Subject" (or "[detached HEAD …]", "(root-commit)")
# after every commit, amend, cherry-pick and revert.
COMMIT_LINE = re.compile(
    r"^\[(?:detached HEAD|[^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.+)$",
    re.MULTILINE,
)
PATCH_FILE_LINE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)

TOPIC_CHARS = 140
TEXT_CHARS = 6000
TOOL_CHARS = 240


@dataclass
class Commit:
    hash: str
    subject: str
    time: float
    author: str = ""
    repo: str = ""
    in_transcript: bool = False
    in_window: bool = False
    in_history: bool = True

    def to_json(self) -> dict[str, object]:
        return {
            "hash": self.hash,
            "short": self.hash[:8],
            "subject": self.subject,
            "time": iso(self.time),
            "author": self.author,
            "repo": self.repo,
            "in_transcript": self.in_transcript,
            "in_window": self.in_window,
            "in_history": self.in_history,
        }


@dataclass
class Item:
    """One transcript row: user, assistant, tool, command or event."""

    kind: str
    time: float | None
    text: str
    name: str = ""

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "time": iso(self.time), "text": self.text, "name": self.name}


@dataclass
class Parsed:
    """Everything one session file or row yields, before commits are attached."""

    source: str
    id: str
    title: str = ""
    directory: str = ""
    branch: str = ""
    prompts: list[str] = field(default_factory=list)
    assistant_messages: int = 0
    tool_counts: Counter[str] = field(default_factory=Counter)
    files: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    cwds: set[str] = field(default_factory=set)
    times: list[float] = field(default_factory=list)
    transcript_commits: list[Commit] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    cost: float | None = None
    subagents: int = 0
    archived: bool = False
    items: list[Item] = field(default_factory=list)

    def touch(self, when: float | None) -> None:
        if when:
            self.times.append(when)

    def add_file(self, path: object) -> None:
        if isinstance(path, str) and path and path not in self.files:
            self.files.append(path)

    def add_model(self, model: object) -> None:
        if isinstance(model, str) and model and model not in self.models:
            self.models.append(model)

    def add_tokens(self, **counts: int) -> None:
        for key, value in counts.items():
            self.tokens[key] = self.tokens.get(key, 0) + int(value or 0)

    def add_commits(self, text: object, when: float | None) -> None:
        if not isinstance(text, str) or "] " not in text:
            return
        for match in COMMIT_LINE.finditer(text):
            short = match.group(1)
            if all(c.hash != short for c in self.transcript_commits):
                self.transcript_commits.append(
                    Commit(short, match.group(2).strip(), when or 0.0, in_transcript=True, in_history=False)
                )


def iso(when: float | None) -> str | None:
    return datetime.fromtimestamp(when, UTC).isoformat().replace("+00:00", "Z") if when else None


def parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def segments(times: list[float], gap: float) -> list[tuple[float, float]]:
    """Split activity timestamps into runs separated by more than `gap` seconds."""
    runs: list[tuple[float, float]] = []
    for when in sorted(times):
        if runs and when - runs[-1][1] <= gap:
            runs[-1] = (runs[-1][0], when)
        else:
            runs.append((when, when))
    return runs


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str, limit: int = TOPIC_CHARS) -> str:
    return shorten(" ".join(text.split()), limit)


def topics(prompts: list[str]) -> list[str]:
    """The prompts that read as a request: the first one plus any of four words or more."""
    seen: list[str] = []
    for index, prompt in enumerate(prompts):
        first = prompt.strip().splitlines()[0] if prompt.strip() else ""
        line = one_line(first if len(first) >= 40 else prompt)
        if not line or (index and len(line.split()) < 4):
            continue
        if line not in seen:
            seen.append(line)
    return seen


def resume_command(source: str, directory: str, session_id: str) -> str:
    tool = f"claude --resume {session_id}" if source == "claude" else f"opencode --session {session_id}"
    return f"cd {shlex.quote(directory)} && {tool}" if directory else tool


def tool_summary(name: str, data: object) -> str:
    """A one-line view of a tool call's input for the transcript."""
    if not isinstance(data, dict):
        return ""
    for key in ("command", "file_path", "filePath", "path", "notebook_path", "pattern", "url", "query", "skill", "id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return one_line(value, TOOL_CHARS)
    for key in ("description", "prompt", "patchText"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return one_line(value, TOOL_CHARS)
    return one_line(name, TOOL_CHARS)
