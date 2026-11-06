#!/usr/bin/env python3
"""Start, stop and inspect the Hostwatch container and its host helper.

    ./hostwatch.py up [--port 8766] [--grace 30] [--no-build]
    ./hostwatch.py down
    ./hostwatch.py logs [-f] [--agent]

Standard library only, so it runs on the host Python. The container gets read-only
mounts of the Claude Code transcripts, the OpenCode database (the rest of its folder,
auth.json included, is covered) and each git repository a session worked in, at their
host paths. Disks and USB cards come from agent.py, which runs on the host as the
systemd user service hostwatch-agent (the container can't mount anything) and answers
on a socket in $XDG_RUNTIME_DIR/hostwatch, the one folder of it the container sees.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing, suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "hostwatch"
AGENT = "hostwatch-agent"
AGENT_DIR_IN_CONTAINER = "/run/hostwatch"
CWD_FIELD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
DB_FILES = {"opencode.db", "opencode.db-wal", "opencode.db-shm", "opencode.db-journal"}


def claude_projects() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def opencode_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "opencode"


def agent_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(base) if base else Path(f"/run/user/{os.getuid()}")) / "hostwatch"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "hostwatch"


def stop_agent() -> None:
    subprocess.run(["systemctl", "--user", "stop", AGENT], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "reset-failed", AGENT], capture_output=True, check=False)


def start_agent() -> Path | None:
    """Run agent.py as a user service; return its socket folder, or None when it did not start."""
    if shutil.which("systemd-run") is None:
        print("warning: no systemd-run, so no disks or USB cards (sessions still work)", file=sys.stderr)
        return None
    folder = agent_dir()
    stop_agent()
    command = [sys.executable, str(HERE / "agent.py"), "--socket", str(folder / "agent.sock")]
    command += ["--state", str(state_dir())]
    done = subprocess.run(
        ["systemd-run", "--user", f"--unit={AGENT}", "--collect", "--quiet", *command],
        capture_output=True,
        text=True,
        check=False,
    )
    deadline = time.monotonic() + 10
    while done.returncode == 0 and time.monotonic() < deadline and not (folder / "agent.sock").exists():
        time.sleep(0.2)
    if done.returncode or not (folder / "agent.sock").exists():
        print(f"warning: the host helper did not start: {done.stderr.strip() or 'no socket'}", file=sys.stderr)
        print(f"  see journalctl --user -u {AGENT}; sessions still work", file=sys.stderr)
        return None
    return folder


def git(cwd: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def session_dirs(projects: Path, opencode: Path) -> set[str]:
    """Every working directory a Claude Code or OpenCode session recorded."""
    dirs: set[str] = set()
    for path in projects.glob("*/*.jsonl"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for raw in set(CWD_FIELD.findall(data)):
            with suppress(ValueError):
                dirs.add(json.loads(b'"' + raw + b'"'))
    db = opencode / "opencode.db"
    if db.is_file():
        try:
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
                dirs.update(row[0] for row in conn.execute("SELECT DISTINCT directory FROM session_v2") if row[0])
        except sqlite3.Error as exc:
            print(f"warning: cannot list OpenCode directories in {db}: {exc}", file=sys.stderr)
    return dirs


def repo_mounts(dirs: set[str], home: str) -> list[str]:
    """The repositories behind the directories (worktree and shared .git folder), outermost only."""
    found: set[str] = set()
    for directory in dirs:
        if not os.path.isdir(directory):
            continue
        out = git(directory, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
        for path in (out or "").splitlines():
            path = path.rstrip("/")
            if path in ("", home) or not path.startswith("/"):
                if path == home:
                    print(f"warning: not mounting {home}: the home folder itself is a git repository", file=sys.stderr)
                continue
            found.add(path)
    roots: list[str] = []
    for path in sorted(found):
        if not any(path.startswith(root + "/") for root in roots):
            roots.append(path)
    return roots


def mount(*fields: str) -> str:
    """A --mount value; csv quoting keeps commas in paths intact."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(fields)
    return buffer.getvalue()


def bind(source: Path | str, target: Path | str) -> str:
    return mount("type=bind", f"source={source}", f"target={target}", "readonly")


def opencode_mounts(opencode: Path) -> list[str]:
    """The OpenCode folder with everything but the database covered: auth.json, logs, snapshots."""
    mounts = [bind(opencode, opencode)]
    for entry in sorted(opencode.iterdir()):
        if entry.name in DB_FILES:
            continue
        if entry.is_dir():
            mounts.append(mount("type=tmpfs", f"target={entry}", "readonly"))
        else:
            mounts.append(bind("/dev/null", entry))
    return mounts


def run_args(
    port: int, grace: float, projects: Path, opencode: Path, repos: list[str], agent: Path | None
) -> list[str]:
    home = str(Path.home())
    env = {
        "HOME": "/tmp",
        "HOST_HOME": home,
        "CLAUDE_PROJECTS_DIR": str(projects),
        "OPENCODE_DB": str(opencode / "opencode.db"),
        "COMMIT_GRACE_MINUTES": str(grace),
    }
    mounts = []
    if projects.is_dir():
        mounts.append(bind(projects, projects))
    if opencode.is_dir():
        mounts += opencode_mounts(opencode)
    mounts += [bind(repo, repo) for repo in repos]
    if agent:
        env["AGENT_SOCKET"] = f"{AGENT_DIR_IN_CONTAINER}/agent.sock"
        mounts.append(bind(agent, AGENT_DIR_IN_CONTAINER))
    args = ["docker", "run", "-d", "--name", NAME, "--user", f"{os.getuid()}:{os.getgid()}"]
    args += ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    args += ["-p", f"127.0.0.1:{port}:8000"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    for mount in mounts:
        args += ["--mount", mount]
    return [*args, NAME]


def container_running() -> bool:
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", NAME], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() == "true"


def wait_ready(url: str, seconds: float) -> bool:
    """Poll until the server answers, giving up early if the container stops.

    Right after boot on a spinning disk, loading Python and FastAPI from a cold page
    cache has taken close to a minute, so the limit is generous.
    """
    start = time.monotonic()
    told = False
    while time.monotonic() - start < seconds:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        if not container_running():
            return False
        if not told and time.monotonic() - start > 10:
            print("Waiting for the server to start (on a cold disk this can take a minute)...", flush=True)
            told = True
        time.sleep(0.5)
    return False


def up(args: argparse.Namespace) -> int:
    projects, opencode = claude_projects(), opencode_dir()
    if not projects.is_dir() and not opencode.is_dir():
        print(f"error: neither {projects} nor {opencode} exists", file=sys.stderr)
        return 1
    repos = repo_mounts(session_dirs(projects, opencode), str(Path.home()))
    if not args.no_build:
        print(f"Building image {NAME} (first build pulls Node and Python images)...")
        if subprocess.run(["docker", "build", "-t", NAME, str(HERE)], check=False).returncode:
            return 1
    agent = start_agent()
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
    if subprocess.run(run_args(args.port, args.grace, projects, opencode, repos, agent), check=False).returncode:
        return 1
    print("Read-only mounts:")
    for path in [projects, opencode, *repos, *([agent] if agent else [])]:
        print(f"  {path}")
    if agent:
        print(f"Host helper: running as the user service {AGENT} (disks and USB cards)")
    url = f"http://127.0.0.1:{args.port}"
    if not wait_ready(f"{url}/api/info", 180):
        if container_running():
            print(f"error: no answer on {url} after 3 minutes; see ./hostwatch.py logs", file=sys.stderr)
        else:
            print("error: the container stopped while starting; see ./hostwatch.py logs", file=sys.stderr)
        return 1
    print(f"\nHostwatch: {url}\nStop with ./hostwatch.py down")
    return 0


def down(_: argparse.Namespace) -> int:
    stop_agent()
    done = subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, text=True, check=False)
    print("Stopped." if done.returncode == 0 else done.stderr.strip())
    return done.returncode


def logs(args: argparse.Namespace) -> int:
    follow = ["-f"] if args.follow else []
    if args.agent:
        return subprocess.run(["journalctl", "--user", "-u", AGENT, "--no-pager", *follow], check=False).returncode
    return subprocess.run(["docker", "logs", *follow, NAME], check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hostwatch: read-only dashboard for this machine, in Docker.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up", help="build the image and (re)start the container")
    start.add_argument("--port", type=int, default=int(os.environ.get("HOSTWATCH_PORT", "8766")))
    start.add_argument(
        "--grace", type=float, default=30, help="minutes after a session goes quiet that commits still count"
    )
    start.add_argument("--no-build", action="store_true", help="reuse the image built last time")
    start.set_defaults(func=up)
    commands.add_parser("down", help="stop and remove the container and the host helper").set_defaults(func=down)
    show = commands.add_parser("logs", help="show the container log")
    show.add_argument("-f", "--follow", action="store_true")
    show.add_argument("--agent", action="store_true", help="the host helper's log instead of the container's")
    show.set_defaults(func=logs)
    args = parser.parse_args(argv)
    if shutil.which("docker") is None:
        print("error: docker is not installed", file=sys.stderr)
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
