# Hostwatch

A local dashboard for one Linux machine. It shows your Claude Code and OpenCode
sessions alongside the git commits made during them, plus the disks and USB cards
plugged into the computer. It runs in Docker, listens on `127.0.0.1` only, and reads
your files through read-only mounts.

## What it shows

| Page | What you get |
| --- | --- |
| **Claude Code**, **OpenCode** | Every session, newest first, grouped by day: title, topics, active time, prompts, tool calls, files changed, models, tokens and cost. Each session has its transcript (prompts only, conversation, or with tools), the commits made during it, and a copyable command to resume it. |
| **Disks** | Disks and partitions with labels, file systems, sizes, free space and mount state. Mount and unmount buttons. |
| **USB cards** | Cards plugged in now and seen before, with read-only and eject controls. A history built from the kernel log: plug-ins, disconnects, write protection, I/O and file system errors. |
| **Docs** | Where each page gets its data, and the terminal commands that do the same things. |

## Requirements

- Linux with a systemd user session (Ubuntu 24.04 is what it is used on)
- Docker, usable by your user without `sudo`
- Python 3.12 on the host, standard library only
- For the Disks and USB cards pages: udisks2 (`udisksctl`), util-linux (`lsblk`, `findmnt`) and the systemd journal

Node and uv are only needed for development: the Docker build installs them for itself.

## Quick start

```sh
git clone git@github.com:AndreiOlegovich/hostwatch.git
cd hostwatch
./hostwatch.py up
```

Then open <http://127.0.0.1:8766>.

`up` builds the image, starts the host helper as a systemd user service and
(re)starts the container. Neither one starts by itself after a reboot, so run `up`
again then. Right after boot on a spinning disk, the server can take about a minute
to answer the first time; `up` waits for it.

| Command | What it does |
| --- | --- |
| `./hostwatch.py up` | Build and start everything |
| `./hostwatch.py up --no-build` | Reuse the image built last time |
| `./hostwatch.py up --port 8766` | Choose the port (or set `HOSTWATCH_PORT`) |
| `./hostwatch.py up --grace 30` | Minutes after a session goes quiet that commits still count |
| `./hostwatch.py down` | Stop the container and the host helper |
| `./hostwatch.py logs [-f]` | Show (or follow) the container log |
| `./hostwatch.py logs --agent [-f]` | Show (or follow) the host helper log |

To start it from anywhere, add a shell function to `~/.bashrc`:

```sh
hostwatch() { ~/path/to/hostwatch/hostwatch.py "${@:-up}"; }
```

## How it works

```
browser ──► 127.0.0.1:8766 ──► container: FastAPI + React UI
                                  │  read-only mounts: transcripts, OpenCode DB, git repos
                                  └─ Unix socket ──► agent.py on the host (lsblk, udisksctl, journalctl)
```

**The container** (`api/`, `ui/`) reads session data and serves the UI. It runs as
your user, with a read-only root file system, all capabilities dropped and
`no-new-privileges`. It gets these mounts, all read-only and at their host paths:

- **Claude Code transcripts:** `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`).
- **The OpenCode database:** `~/.local/share/opencode/opencode.db` (or under
  `$XDG_DATA_HOME`). Everything else in that folder, `auth.json` included, is
  covered by empty mounts, so the container can't see it.
- **Git repositories:** each repository a session worked in. `up` finds them from
  the working directories in the transcripts and mounts the outermost repository
  root. It never mounts your home folder, even if that is itself a repository.

**The host helper** (`agent.py`) does what a locked-down container can't: list
disks, mount and unmount through udisks (the same service the Files app uses, so no
password is needed for your own drives), and read the kernel log. It runs as the
user service `hostwatch-agent` and answers on a socket in
`$XDG_RUNTIME_DIR/hostwatch/` that only your user can open; of `$XDG_RUNTIME_DIR`,
the container sees that folder only. It keeps a small amount of state in
`~/.local/state/hostwatch/`:

- `cards.json`: cards seen and which ones to keep read-only.
- `kernel-history.json`: what earlier boots' kernel logs said, so each boot is read
  once. Delete it to read the journal again.

**Commits per session** come from two sources. One is the commits git printed inside
the transcript. The other is the repository's history while the session was active:
activity is split into runs at gaps of more than an hour, and commits made during a
run, or within the grace period after it ends, count. Each commit says which way it
was found.

**Safety of the web page:** the server only accepts `Host: 127.0.0.1` or
`localhost`, which stops DNS rebinding. Requests that change something (mount,
unmount, read-only, eject) also need an `X-Hostwatch: 1` header and are refused
when `Sec-Fetch-Site` says they came from another site, so another site's page
can't send them.

## Development

```sh
uv sync                    # Python dependencies, including dev tools
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format

cd ui && npm ci
npm run dev                # UI with live reload; /api goes to the container on :8766
```

The project is laid out as follows:

| Path | Contents |
| --- | --- |
| `hostwatch.py` | Launcher: finds what to mount, starts the helper and the container |
| `agent.py` | Host helper: disks, udisks actions and the kernel log, over a Unix socket |
| `api/` | FastAPI app: Claude Code and OpenCode readers, commit matching, host helper client |
| `ui/` | React UI, built with Vite |
| `tests/` | pytest tests for the API and the host helper |
| `Dockerfile` | Two stages: builds the UI with Node, then runs the API on Python 3.12 |

## License

[MIT](LICENSE)
