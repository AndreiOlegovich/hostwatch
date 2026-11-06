"""Claude Code sessions: one JSONL transcript per session in ~/.claude/projects/<project>/."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .common import TEXT_CHARS, Item, Parsed, parse_iso, shorten, tool_summary

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
SHELL_TOOLS = {"Bash"}
COMPACTED = "This session is being continued from a previous conversation"
TAG = re.compile(r"<(command-name|command-args|bash-input)>(.*?)</\1>", re.DOTALL)
NOT_A_PROMPT = re.compile(r"^<(local-command-[a-z]+|bash-std(?:out|err)|task-notification|system-reminder)>")


def project_key(path: str) -> str:
    """Claude Code names a project folder after its launch directory, every other character a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def session_files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for path in root.glob("*/*.jsonl"):
        if SESSION_ID.match(path.stem):
            known = found.get(path.stem)
            if known is None or path.stat().st_mtime > known.stat().st_mtime:
                found[path.stem] = path
    return found


def classify(entry: dict, text: str, when: float | None) -> Item | None:
    """Sort a user entry's text into a prompt, a slash or ! command, an event, or noise."""
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    stripped = text.strip()
    if entry.get("isCompactSummary") or stripped.startswith(COMPACTED):
        return Item("event", when, "Context compacted; the conversation continues from a summary")
    if entry.get("isMeta") or kind not in (None, "human") or not stripped:
        return None
    tags = {match.group(1): match.group(2).strip() for match in TAG.finditer(stripped)}
    if "command-name" in tags:
        return Item("command", when, f"{tags['command-name']} {tags.get('command-args', '')}".strip())
    if "bash-input" in tags:
        return Item("command", when, "! " + tags["bash-input"])
    if NOT_A_PROMPT.match(stripped):
        return None
    return Item("user", when, shorten(stripped, TEXT_CHARS))


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _assistant(parsed: Parsed, message: dict, when: float | None, state: dict, full: bool) -> None:
    message_id = message.get("id")
    # One API response is written as one entry per content block, each carrying the same usage.
    if not message_id or message_id not in state["messages"]:
        state["messages"].add(message_id)
        parsed.assistant_messages += 1
        model = message.get("model")
        if isinstance(model, str) and not model.startswith("<"):
            parsed.add_model(model)
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        parsed.add_tokens(
            input=usage.get("input_tokens", 0),
            output=usage.get("output_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
            cache_write=usage.get("cache_creation_input_tokens", 0),
        )
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            if full:
                parsed.items.append(Item("assistant", when, shorten(block["text"], TEXT_CHARS)))
        elif block.get("type") == "tool_use":
            name = str(block.get("name") or "tool")
            data = block.get("input")
            parsed.tool_counts[name] += 1
            if name in EDIT_TOOLS and isinstance(data, dict):
                parsed.add_file(data.get("file_path") or data.get("notebook_path"))
            if name in SHELL_TOOLS:
                state["shell"].add(block.get("id"))
            if full:
                parsed.items.append(Item("tool", when, tool_summary(name, data), name))


def _user(parsed: Parsed, entry: dict, message: dict, when: float | None, state: dict, full: bool) -> None:
    content = message.get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        for block in content:
            if isinstance(block, dict) and block.get("tool_use_id") in state["shell"]:
                result = entry.get("toolUseResult")
                if isinstance(result, dict):
                    parsed.add_commits(result.get("stdout"), when)
                parsed.add_commits(_block_text(block.get("content")), when)
        return
    text = _block_text(content)
    if "<bash-stdout>" in text:
        parsed.add_commits(text, when)
    item = classify(entry, text, when)
    if item is None:
        return
    if item.kind == "user":
        parsed.prompts.append(item.text)
    if full:
        parsed.items.append(item)


def parse(path: Path, full: bool = False) -> Parsed:
    parsed = Parsed(source="claude", id=path.stem)
    state: dict[str, set] = {"messages": set(), "shell": set()}
    folder = path.parent.name
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "ai-title" and isinstance(entry.get("aiTitle"), str):
                parsed.title = entry["aiTitle"]
                continue
            if kind not in ("user", "assistant") or entry.get("isSidechain"):
                continue
            when = parse_iso(entry.get("timestamp"))
            parsed.touch(when)
            cwd = entry.get("cwd")
            if isinstance(cwd, str) and cwd:
                parsed.cwds.add(cwd)
                if not parsed.directory or (project_key(cwd) == folder != project_key(parsed.directory)):
                    parsed.directory = cwd
            branch = entry.get("gitBranch")
            if isinstance(branch, str) and branch and branch != "HEAD":
                parsed.branch = branch
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            if kind == "assistant":
                _assistant(parsed, message, when, state, full)
            else:
                _user(parsed, entry, message, when, state, full)
    subagents = path.with_suffix("") / "subagents"
    if subagents.is_dir():
        parsed.subagents = sum(1 for _ in subagents.glob("*.jsonl"))
    return parsed


class ClaudeStore:
    """Parses each transcript once and again only when its size or mtime changes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[tuple[int, int], Parsed]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.root.is_dir()

    def summaries(self) -> list[Parsed]:
        files = session_files(self.root)
        result = []
        for session_id, path in files.items():
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (stat.st_mtime_ns, stat.st_size)
            with self._lock:
                cached = self._cache.get(session_id)
            if cached and cached[0] == key:
                parsed = cached[1]
            else:
                parsed = parse(path)
                with self._lock:
                    self._cache[session_id] = (key, parsed)
            if parsed.prompts or parsed.assistant_messages:
                result.append(parsed)
        with self._lock:
            for gone in set(self._cache) - set(files):
                del self._cache[gone]
        return result

    def detail(self, session_id: str) -> Parsed | None:
        if not SESSION_ID.match(session_id):
            return None
        path = session_files(self.root).get(session_id)
        return parse(path, full=True) if path else None
