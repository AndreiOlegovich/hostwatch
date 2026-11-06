#!/usr/bin/env python3
"""Hostwatch host helper: lists disks and USB cards, mounts them, and reads the kernel log.

    python3 agent.py --socket /run/user/1000/hostwatch/agent.sock --state ~/.local/state/hostwatch

The container can't mount anything (it runs read-only with no capabilities), so this runs on
the host as a systemd user service, started by ./hostwatch.py up. udisks lets a process of the
logged-in user mount, unmount and power off drives without a password, the same as Files does.
It answers plain HTTP with JSON on a Unix socket only this user can open, and remembers which
cards to keep read-only in a small JSON file. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,PARTTYPENAME,MOUNTPOINTS,MODEL,VENDOR,SERIAL,TRAN,RM,RO,FSSIZE,FSUSED,FSAVAIL"
)
SYSTEM_MOUNTS = {"/", "/boot", "/efi", "/home", "/usr", "/var", "/opt", "/srv", "/tmp", "[SWAP]"}
NOT_MOUNTABLE = {"swap", "crypto_LUKS", "LVM2_member", "linux_raid_member", "BitLocker", "zfs_member", "bcache"}
WATCH_SECONDS = 2.0
RETRY_SECONDS = 30.0
# Unmounting waits for writes still on their way to the card.
UDISKS_TIMEOUT = 600

# Kernel log lines about USB devices, the SCSI disks they become, and their errors.
JOURNAL_GREP = (
    r"usb \d|usb-storage|scsi host\d|sd \d+:\d+:\d+:\d+: \[|"
    r"I/O error|error, dev |FAT-fs|EXT4-fs (error|warning)|ntfs3\("
)
USB_LINE = re.compile(r"^usb (\d+-[\d.]+): (.*)$")
NEW_DEVICE = re.compile(r"^new (.+?) USB device number \d+ using ")
FOUND = re.compile(r"^New USB device found, idVendor=(\w+), idProduct=(\w+)")
DISCONNECT = re.compile(r"^USB disconnect, device number \d+")
RESET = re.compile(r"^reset .+? USB device number \d+ using ")
STORAGE = re.compile(r"^usb-storage (\d+-[\d.]+):[\d.]+: USB Mass Storage device detected")
SCSI_HOST = re.compile(r"^scsi host(\d+): (?:usb-storage (\d+-[\d.]+):|uas)")
SD_LINE = re.compile(r"^sd (\d+):\d+:\d+:\d+: \[(\w+)\] (.*)$")
BLOCKS = re.compile(r"^(\d+) (\d+)-byte logical blocks")
BLOCK_ERROR = re.compile(r"error, dev (\w+), sector \d+ op 0x[0-9a-f]+:\((\w+)\)")
BUFFER_ERROR = re.compile(r"^Buffer I/O error on dev (\w+), logical block \d+(, lost async page write)?")
FS_LINE = re.compile(r"^(FAT-fs|exFAT-fs|EXT4-fs error|EXT4-fs warning|ntfs3) ?\((?:device )?(\w+)\):? (.*)$")
FS_HARMLESS = ("not a recommended IO charset",)
FS_DIRTY = ("not properly unmounted", "volume is dirty")

# Issues: errors closer together than BURST_GAP are one issue; a disconnect counts as the card
# dropping off when errors come within DROP_WINDOW of it, or it is back within BACK_WITHIN
# (people take longer than that to pull a card out and push it back).
BURST_GAP = 60.0
DROP_WINDOW = 15.0
BACK_WITHIN = 5.0
LINES_PER_ISSUE = 8
# Bump when the parser changes, so the cached history is read again.
CACHE_VERSION = 1


class AgentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def iso(when: float | None) -> str | None:
    return datetime.fromtimestamp(when, UTC).isoformat().replace("+00:00", "Z") if when else None


def run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def number(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Block devices ---------------------------------------------------------------------------


def lsblk() -> list[dict]:
    try:
        done = run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(500, f"cannot run lsblk: {exc}") from exc
    if done.returncode:
        raise AgentError(500, f"lsblk failed: {done.stderr.strip()}")
    return json.loads(done.stdout).get("blockdevices", [])


def mount_flags() -> dict[str, bool]:
    """Device path -> True when every mount of it is read-only."""
    try:
        done = run(["findmnt", "-J", "-l", "-o", "SOURCE,OPTIONS"])
        filesystems = json.loads(done.stdout or "{}").get("filesystems", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    flags: dict[str, bool] = {}
    for fs in filesystems:
        # A bind mount shows as /dev/sda4[/usr/share/hunspell].
        source = (fs.get("source") or "").split("[", 1)[0]
        if source.startswith("/dev/"):
            flags[source] = flags.get(source, True) and (fs.get("options") or "").split(",", 1)[0] == "ro"
    return flags


def is_card(dev: dict) -> bool:
    return dev.get("type") == "disk" and dev.get("tran") == "usb" and bool(number(dev.get("size")))


def is_disk(dev: dict) -> bool:
    return (
        dev.get("type") == "disk"
        and dev.get("tran") != "usb"
        and not str(dev.get("name", "")).startswith(("zram", "loop"))
        and bool(number(dev.get("size")))
    )


def card_key(dev: dict) -> str:
    return (dev.get("serial") or "").strip() or f"{dev.get('vendor') or ''}-{dev.get('model') or ''}".strip()


def card_name(dev: dict) -> str:
    return " ".join(part.strip() for part in (dev.get("vendor") or "", dev.get("model") or "") if part.strip())


def volumes_of(dev: dict) -> list[dict]:
    """The partitions of a disk, or the disk itself when it holds a file system directly."""
    return dev.get("children") or ([dev] if dev.get("fstype") else [])


def volume(dev: dict, flags: dict[str, bool]) -> dict:
    mountpoints = [point for point in dev.get("mountpoints") or [] if point]
    fstype = dev.get("fstype") or ""
    system = fstype == "swap" or any(point in SYSTEM_MOUNTS or point.startswith("/boot/") for point in mountpoints)
    mountable = bool(fstype) and fstype not in NOT_MOUNTABLE and not system
    return {
        "device": dev["path"],
        "name": dev["name"],
        "label": dev.get("label") or "",
        "fstype": fstype,
        "kind": dev.get("parttypename") or "",
        "size": number(dev.get("size")),
        "mountpoints": mountpoints,
        "readonly": flags.get(dev["path"]) if mountpoints else None,
        "fssize": number(dev.get("fssize")),
        "fsused": number(dev.get("fsused")),
        "fsavail": number(dev.get("fsavail")),
        "system": system,
        "can_mount": mountable and not mountpoints,
        "can_unmount": mountable and bool(mountpoints),
    }


def why_not(view: dict, action: str) -> str:
    if view["system"]:
        return f"{view['device']} is a system partition"
    if not view["fstype"]:
        return f"{view['device']} has no file system"
    if view["fstype"] in NOT_MOUNTABLE:
        return f"{view['device']} holds {view['fstype']}, which Hostwatch doesn't mount"
    if action == "mount":
        return f"{view['device']} is already mounted at {', '.join(view['mountpoints'])}"
    return f"{view['device']} is not mounted"


def udisks(action: str, device: str, readonly: bool = False) -> str:
    args = ["udisksctl", action, "--no-user-interaction", "-b", device]
    if readonly:
        args += ["-o", "ro"]
    try:
        done = run(args, timeout=UDISKS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise AgentError(504, f"udisksctl {action} {device} did not finish in {UDISKS_TIMEOUT // 60} minutes") from exc
    except OSError as exc:
        raise AgentError(500, f"cannot run udisksctl: {exc}") from exc
    # "Error unmounting /dev/sdb1: GDBus.Error:org.freedesktop.UDisks2.Error.DeviceBusy: target is busy"
    text = re.sub(r"GDBus\.Error:[\w.]+: ", "", (done.stderr or done.stdout).strip())
    if done.returncode:
        raise AgentError(409, text or f"udisksctl {action} {device} failed")
    return text or f"{device}: {action} done"


def remount(device: str, readonly: bool) -> None:
    """udisks has no remount: unmount and mount again with the new options."""
    udisks("unmount", device)
    try:
        udisks("mount", device, readonly)
    except AgentError as exc:
        raise AgentError(exc.status, f"{device} was unmounted, but mounting it again failed: {exc}") from exc


def current_boot() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    except OSError:
        return ""


# Kernel log ------------------------------------------------------------------------------


@dataclass
class Event:
    time: float
    kind: str  # lost_write, write, read, fs, dirty or reset
    line: str


@dataclass
class Connection:
    """One time a USB device appeared on a port, until it went away, as the kernel log tells it."""

    boot: str
    port: str
    connected: float
    speed: str = ""
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    storage: bool = False
    kname: str = ""
    size: int | None = None
    write_protect: bool | None = None
    disconnected: float | None = None
    boot_ended: float | None = None
    events: list[Event] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.serial or f"{self.vendor_id}:{self.product_id}"

    @property
    def name(self) -> str:
        return " ".join(part for part in (self.manufacturer, self.product) if part) or f"USB {self.key}"


def message_text(value: object) -> str:
    if isinstance(value, list):  # journald sends non-UTF-8 messages as a byte array
        return bytes(value).decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def classify(message: str) -> tuple[str, str] | None:
    """(event kind, kernel device name) for an error line about a disk, else None."""
    if match := BLOCK_ERROR.search(message):
        return ("read" if match[2] == "READ" else "write"), match[1]
    if match := BUFFER_ERROR.match(message):
        return ("lost_write" if match[2] else "read"), match[1]
    if match := FS_LINE.match(message):
        text = match[3]
        if any(part in text for part in FS_HARMLESS):
            return None
        return ("dirty" if any(part in text for part in FS_DIRTY) else "fs"), match[2]
    return None


class BootParser:
    """Turns one boot's kernel log into the USB connections it describes."""

    def __init__(self, boot: str) -> None:
        self.boot = boot
        self.connections: list[Connection] = []
        self.cursor = ""
        self._ports: dict[str, Connection] = {}
        self._hosts: dict[str, Connection] = {}
        self._disks: dict[str, Connection] = {}

    def read(self) -> str | None:
        """Read what the kernel logged in this boot since the last read; return an error, if any."""
        args = ["journalctl", "-k", "-b", self.boot, "-o", "json", "--no-pager", f"--grep={JOURNAL_GREP}"]
        if self.cursor:
            args.append(f"--after-cursor={self.cursor}")
        try:
            done = run(args, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"cannot read the kernel log: {exc}"
        for line in done.stdout.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            self.feed(int(entry.get("__REALTIME_TIMESTAMP") or 0) / 1e6, message_text(entry.get("MESSAGE")))
            self.cursor = entry.get("__CURSOR") or self.cursor
        # journalctl exits 1 when nothing new matches.
        return f"cannot read the kernel log: {done.stderr.strip()}" if done.returncode > 1 else None

    def finish(self, ended: float) -> None:
        for conn in self._ports.values():
            conn.boot_ended = ended

    def storage(self) -> list[Connection]:
        return [conn for conn in self.connections if conn.storage]

    def feed(self, when: float, message: str) -> None:
        if match := USB_LINE.match(message):
            self._usb(match[1], match[2], when, message)
        elif match := STORAGE.match(message):
            if conn := self._ports.get(match[1]):
                conn.storage = True
        elif match := SCSI_HOST.match(message):
            # uas doesn't name the port: take the newest device that has no SCSI host yet.
            hosted = [id(conn) for conn in self._hosts.values()]
            conn = (
                self._ports.get(match[2])
                if match[2]
                else next((c for c in reversed(self._ports.values()) if id(c) not in hosted), None)
            )
            if conn:
                conn.storage = True
                self._hosts[match[1]] = conn
        elif match := SD_LINE.match(message):
            if conn := self._hosts.get(match[1]):
                conn.kname = match[2]
                self._disks[match[2]] = conn
                if blocks := BLOCKS.match(match[3]):
                    conn.size = int(blocks[1]) * int(blocks[2])
                elif match[3].startswith("Write Protect is "):
                    conn.write_protect = match[3].endswith(" on")
        elif found := classify(message):
            kind, device = found
            # The mapping outlives the disconnect: errors for a vanished card come right after it.
            if conn := self._disks.get(re.sub(r"^(sd[a-z]+)\d+$", r"\1", device)):
                conn.events.append(Event(when, kind, message))

    def _usb(self, port: str, text: str, when: float, message: str) -> None:
        if match := NEW_DEVICE.match(text):
            if old := self._ports.get(port):
                old.disconnected = old.disconnected or when
            conn = Connection(self.boot, port, when, speed=match[1])
            self._ports[port] = conn
            self.connections.append(conn)
            return
        conn = self._ports.get(port)
        if conn is None:
            return
        if match := FOUND.match(text):
            conn.vendor_id, conn.product_id = match[1], match[2]
        elif text.startswith("Product: "):
            conn.product = text[9:].strip()
        elif text.startswith("Manufacturer: "):
            conn.manufacturer = text[14:].strip()
        elif text.startswith("SerialNumber: "):
            conn.serial = text[14:].strip()
        elif DISCONNECT.match(text):
            conn.disconnected = when
            del self._ports[port]
        elif RESET.match(text):
            conn.events.append(Event(when, "reset", message))


def connection_from_json(data: dict) -> Connection:
    events = [Event(**event) for event in data.pop("events", [])]
    return Connection(**data, events=events)


class KernelLog:
    """USB storage connections from the kernel log: finished boots read once and cached, the current one live.

    Reading the whole journal at once takes minutes (1.5 GB is common), one boot about a second.
    The cache also keeps the history after the journal rotates old boots away.
    """

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.error: str | None = None
        self.live_error: str | None = None
        self.loading = True
        self.lock = threading.Lock()
        self._current = BootParser(current_boot())
        self._past: dict[str, list[Connection]] = {}
        try:
            saved = json.loads(cache.read_text())
            if saved.get("version") == CACHE_VERSION:
                self._past = {
                    boot: [connection_from_json(conn) for conn in conns] for boot, conns in saved["boots"].items()
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def load_history(self) -> None:
        """Read each finished boot the cache doesn't have yet."""
        try:
            done = run(["journalctl", "--list-boots", "-o", "json", "--no-pager"], timeout=60)
            boots = json.loads(done.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.error = f"cannot list boots: {exc}"
            boots = []
        try:
            for boot in boots:
                boot_id = boot.get("boot_id", "")
                if not boot_id or boot_id == self._current.boot or boot_id in self._past:
                    continue
                parser = BootParser(boot_id)
                if error := parser.read():
                    self.error = error
                    continue
                parser.finish(int(boot.get("last_entry") or 0) / 1e6)
                self._past = {**self._past, boot_id: parser.storage()}
                self._save()
        finally:
            self.loading = False

    def refresh(self) -> list[Connection]:
        """Read what the kernel logged since last time; return every USB storage connection in time order."""
        with self.lock:
            self.live_error = self._current.read()
        connections = [conn for conns in self._past.values() for conn in conns] + self._current.storage()
        return sorted(connections, key=lambda conn: conn.connected)

    def _save(self) -> None:
        boots = {boot: [asdict(conn) for conn in conns] for boot, conns in self._past.items()}
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": CACHE_VERSION, "boots": boots}))
        os.replace(temporary, self.cache)


# Issues ----------------------------------------------------------------------------------

COUNT_WORDS = (
    ("read", "failed read", "failed reads"),
    ("fs", "file system error", "file system errors"),
    ("reset", "USB reset", "USB resets"),
)
# A write-protected card turns every write away: nothing was being copied onto it.
REFUSED = {"write": "refused", "lost_write": "refused_page"}


def bursts(events: list[Event]) -> list[list[Event]]:
    groups: list[list[Event]] = []
    for event in sorted(events, key=lambda e: e.time):
        if groups and event.time - groups[-1][-1].time <= BURST_GAP:
            groups[-1].append(event)
        else:
            groups.append([event])
    return groups


def counted(events: list[Event]) -> list[str]:
    counts = Counter(event.kind for event in events)
    parts = []
    # The kernel logs one failed write twice: the disk error, then the lost page.
    if writes := max(counts["write"], counts["lost_write"]):
        lost = ", data not saved" if counts["lost_write"] else ""
        parts.append(f"{writes} failed write{'' if writes == 1 else 's'}{lost}")
    if refused := max(counts["refused"], counts["refused_page"]):
        parts.append(f"{refused} write{'' if refused == 1 else 's'} refused")
    return parts + [
        f"{counts[kind]} {one if counts[kind] == 1 else many}" for kind, one, many in COUNT_WORDS if counts[kind]
    ]


def stamped(event: Event) -> str:
    return f"{time.strftime('%d %b %H:%M:%S', time.localtime(event.time))}  {event.line}"


def make_issue(
    conn: Connection, kind: str, severity: str, title: str, start: float, events: list[Event], notes: list[str]
) -> dict:
    end = max([start, *(event.time for event in events)])
    return {
        "id": f"{conn.key}-{int(start * 1000)}-{kind}",
        "serial": conn.key,
        "card": conn.name,
        "device": conn.kname,
        "kind": kind,
        "severity": severity,
        "title": title,
        "started": iso(start),
        "ended": iso(end) if end - start >= 1 else None,
        "details": [*counted(events), *notes],
        "lines": [stamped(event) for event in events[:LINES_PER_ISSUE]],
        "more_lines": max(0, len(events) - LINES_PER_ISSUE),
    }


def burst_issue(conn: Connection, events: list[Event]) -> dict:
    kinds = {event.kind for event in events}
    if kinds & {"lost_write", "write"}:
        kind, severity, title = "write", "high", "Copy to the card failed"
    elif "fs" in kinds:
        made_ro = any("read-only" in event.line for event in events)
        kind, severity = "fs", "high"
        title = "File system errors; the kernel made the card read-only" if made_ro else "File system errors"
    elif "read" in kinds:
        kind, severity, title = "read", "medium", "Read errors"
    elif kinds & {"refused", "refused_page"}:
        kind, severity, title = "refused", "low", "Writes refused by the write-protected card"
    else:
        kind, severity, title = "reset", "low", "USB connection reset"
    return make_issue(conn, kind, severity, title, events[0].time, events, [])


def build_issues(connections: list[Connection]) -> list[dict]:
    by_card: dict[str, list[Connection]] = {}
    for conn in connections:
        by_card.setdefault(conn.key, []).append(conn)
    issues: list[dict] = []
    for conns in by_card.values():
        protect_was_off = False
        last_drop: dict | None = None
        for index, conn in enumerate(conns):
            following = conns[index + 1] if index + 1 < len(conns) else None
            if conn.write_protect is False:
                protect_was_off = True
            elif conn.write_protect and protect_was_off:
                protect_was_off = False
                note = "Worn-out flash cards do this to protect what is left on them."
                issues.append(
                    make_issue(
                        conn, "protect", "medium", "The card turned on its write protection", conn.connected, [], [note]
                    )
                )

            if dirty := [event for event in conn.events if event.kind == "dirty"]:
                if last_drop is not None:
                    last_drop["details"].append("file system left unclean")
                else:
                    note = "It was pulled out or lost power while mounted, so the last files written may be incomplete."
                    title = "Plugged in after an unsafe removal"
                    issues.append(make_issue(conn, "dirty", "low", title, dirty[0].time, dirty[:1], [note]))

            events = [event for event in conn.events if event.kind != "dirty"]
            if conn.write_protect:
                events = [Event(e.time, REFUSED.get(e.kind, e.kind), e.line) for e in events]
            groups = bursts(events)
            last_drop = None
            if conn.disconnected is not None:
                gone = conn.disconnected
                back = following.connected - gone if following and following.boot == conn.boot else None
                near = [g for g in groups if g[0].time - DROP_WINDOW <= gone <= g[-1].time + DROP_WINDOW]
                if near or (back is not None and back <= BACK_WITHIN):
                    events = [event for group in near for event in group]
                    kinds = {event.kind for event in events}
                    title = "Disconnected by itself"
                    if kinds & {"lost_write", "write"}:
                        title += " during a copy"
                    elif "read" in kinds:
                        title += " while being read"
                    notes = [f"back {back:.0f} s later"] if back is not None and back <= BACK_WITHIN else []
                    severity = "high" if kinds & {"lost_write", "write", "fs"} else "medium"
                    start = min([gone, *(event.time for event in events)])
                    last_drop = make_issue(conn, "disconnect", severity, title, start, events, notes)
                    issues.append(last_drop)
                    groups = [g for g in groups if g not in near]
            issues.extend(burst_issue(conn, group) for group in groups)
    return sorted(issues, key=lambda issue: issue["started"] or "", reverse=True)


# Saved state -----------------------------------------------------------------------------


class State:
    """Per-card memory that outlives the kernel log: names, labels, when seen, keep read-only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:
            self.cards: dict[str, dict] = json.loads(path.read_text()).get("cards", {})
        except (OSError, ValueError):
            self.cards = {}

    def get(self, key: str) -> dict:
        with self.lock:
            return dict(self.cards.get(key, {}))

    def all(self) -> dict[str, dict]:
        with self.lock:
            return {key: dict(card) for key, card in self.cards.items()}

    def update(self, key: str, **fields: object) -> None:
        with self.lock:
            card = self.cards.setdefault(key, {})
            if all(card.get(name) == value for name, value in fields.items()):
                return
            card.update(fields)
            self._save()

    def saw(self, key: str, name: str, size: int | None, labels: list[str], now: float) -> None:
        with self.lock:
            card = self.cards.setdefault(key, {})
            merged = list(dict.fromkeys([*labels, *card.get("labels", [])]))
            fields = {"name": name or card.get("name", ""), "size": size or card.get("size"), "labels": merged}
            if "first_seen" not in card:
                fields["first_seen"] = now
            if all(card.get(field_name) == value for field_name, value in fields.items()):
                return
            card.update(fields)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cards": self.cards}, indent=2, ensure_ascii=False))
        os.replace(temporary, self.path)


# The helper ------------------------------------------------------------------------------


class Agent:
    def __init__(self, state_dir: Path) -> None:
        self.state = State(state_dir / "cards.json")
        self.log = KernelLog(state_dir / "kernel-history.json")
        self.ops = threading.Lock()  # one udisks change at a time
        self.present: set[str] = set()
        self.keep_errors: dict[str, tuple[str, float]] = {}

    def disks(self) -> dict:
        flags = mount_flags()
        disks = [
            {
                "device": dev["path"],
                "name": dev["name"],
                "model": (dev.get("model") or "").strip(),
                "serial": (dev.get("serial") or "").strip(),
                "size": number(dev.get("size")),
                "tran": dev.get("tran") or "",
                "readonly": bool(dev.get("ro")),
                "volumes": [volume(part, flags) for part in volumes_of(dev)],
            }
            for dev in lsblk()
            if is_disk(dev)
        ]
        return {"disks": disks}

    def cards(self) -> dict:
        flags = mount_flags()
        devices = [dev for dev in lsblk() if is_card(dev)]
        connections = self.log.refresh()
        issues = build_issues(connections)
        issue_count = Counter(issue["serial"] for issue in issues)
        boot = current_boot()
        present = []
        for dev in devices:
            key = card_key(dev)
            conn = next(
                (c for c in reversed(connections) if c.key == key and c.boot == boot and c.disconnected is None), None
            )
            volumes = [volume(part, flags) for part in volumes_of(dev)]
            mounted = [v for v in volumes if v["mountpoints"]]
            if not mounted:
                state = "not mounted"
            elif all(v["readonly"] for v in mounted):
                state = "read-only"
            elif not any(v["readonly"] for v in mounted):
                state = "writable"
            else:
                state = "mixed"
            error = self.keep_errors.get(key)
            present.append(
                {
                    "serial": key,
                    "name": card_name(dev) or (conn.name if conn else ""),
                    "labels": [v["label"] for v in volumes if v["label"]],
                    "device": dev["path"],
                    "size": number(dev.get("size")),
                    "write_protected": bool(dev.get("ro")),
                    "protect_switch": bool(conn and conn.write_protect),
                    "readonly_pref": self.state.get(key).get("readonly"),
                    "state": state,
                    "connected": iso(conn.connected) if conn else None,
                    "speed": conn.speed if conn else "",
                    "volumes": volumes,
                    "keep_error": error[0] if error else None,
                    "issues": issue_count[key],
                }
            )
        known = self.known(connections, issue_count, {card["serial"] for card in present})
        return {
            "present": present,
            "known": known,
            "issues": issues,
            "log_error": self.log.live_error or self.log.error,
            "history_loading": self.log.loading,
        }

    def known(self, connections: list[Connection], issue_count: Counter[str], present: set[str]) -> list[dict]:
        cards: dict[str, dict] = {}
        for conn in connections:
            card = cards.setdefault(
                conn.key,
                {
                    "serial": conn.key,
                    "name": conn.name,
                    "labels": [],
                    "size": None,
                    "write_protected": None,
                    "connections": [],
                },
            )
            card["connections"].append(conn)
            card["size"] = conn.size or card["size"]
            if conn.write_protect is not None:
                card["write_protected"] = conn.write_protect
        for key, saved in self.state.all().items():
            card = cards.setdefault(
                key, {"serial": key, "name": "", "labels": [], "size": None, "write_protected": None, "connections": []}
            )
            card["name"] = card["name"] or saved.get("name", "")
            card["labels"] = saved.get("labels", [])
            card["size"] = card["size"] or saved.get("size")
            card["saved"] = saved
        out = []
        for key, card in cards.items():
            conns: list[Connection] = card.pop("connections")
            saved = card.pop("saved", {})
            starts = [c.connected for c in conns] + ([saved["first_seen"]] if saved.get("first_seen") else [])
            ends = [c.disconnected or c.boot_ended or c.connected for c in conns]
            ends += [saved["last_seen"]] if saved.get("last_seen") else []
            card.update(
                {
                    "present": key in present,
                    "insertions": len(conns),
                    "first_seen": iso(min(starts)) if starts else None,
                    "last_seen": iso(max(ends)) if ends else None,
                    "issues": issue_count[key],
                    "readonly_pref": saved.get("readonly"),
                    "connections": [
                        {
                            "connected": iso(c.connected),
                            "disconnected": iso(c.disconnected),
                            "boot_ended": iso(c.boot_ended),
                            "device": c.kname,
                            "speed": c.speed,
                        }
                        for c in reversed(conns[-100:])
                    ],
                }
            )
            out.append(card)
        return sorted(out, key=lambda card: (card["present"], card["last_seen"] or ""), reverse=True)

    def find_volume(self, device: str) -> tuple[dict, dict | None]:
        """A device path's lsblk entry and the USB card it is on, if any."""
        for dev in lsblk():
            if not (is_disk(dev) or is_card(dev)):
                continue
            for part in volumes_of(dev):
                if part["path"] == device:
                    return part, dev if is_card(dev) else None
        raise AgentError(404, f"no such disk or partition: {device}")

    def find_card(self, key: str) -> dict:
        for dev in lsblk():
            if is_card(dev) and card_key(dev) == key:
                return dev
        raise AgentError(404, "that card is not plugged in")

    def mount(self, device: str, readonly: bool) -> str:
        part, card = self.find_volume(device)
        view = volume(part, mount_flags())
        if not view["can_mount"]:
            raise AgentError(409, why_not(view, "mount"))
        if card is not None and self.state.get(card_key(card)).get("readonly"):
            readonly = True
        with self.ops:
            message = udisks("mount", device, readonly)
        log(f"mount {device}{' read-only' if readonly else ''}: {message}")
        return message

    def unmount(self, device: str) -> str:
        part, _ = self.find_volume(device)
        view = volume(part, mount_flags())
        if not view["can_unmount"]:
            raise AgentError(409, why_not(view, "unmount"))
        with self.ops:
            message = udisks("unmount", device)
        log(f"unmount {device}: {message}")
        return message

    def set_readonly(self, key: str, readonly: bool) -> str:
        dev = self.find_card(key)
        if not readonly and dev.get("ro"):
            raise AgentError(
                409, "The card itself is read-only (its own write protection), so it can't be made writable."
            )
        self.state.update(key, readonly=readonly)
        self.keep_errors.pop(key, None)
        log(f"card {key}: keep {'read-only' if readonly else 'writable'}")
        flags = mount_flags()
        changed = []
        with self.ops:
            for part in volumes_of(dev):
                view = volume(part, flags)
                if view["can_unmount"] and view["readonly"] is not readonly:
                    remount(view["device"], readonly)
                    changed.append(view["label"] or view["device"])
        mode = "read-only" if readonly else "writable"
        if changed:
            return f"Mounted {', '.join(changed)} again, {mode}."
        return f"It will be mounted {mode} from now on."

    def eject(self, key: str) -> str:
        dev = self.find_card(key)
        flags = mount_flags()
        with self.ops:
            for part in volumes_of(dev):
                if volume(part, flags)["mountpoints"]:
                    udisks("unmount", part["path"])
            udisks("power-off", dev["path"])
        log(f"card {key}: unmounted and powered off {dev['path']}")
        return f"{card_name(dev) or 'The card'} is switched off: safe to pull out."

    def watch(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # keep watching whatever one round runs into
                log(traceback.format_exc())
            time.sleep(WATCH_SECONDS)

    def tick(self) -> None:
        """Remember the cards plugged in, and remount read-only those the user wants kept that way."""
        now = time.time()
        flags = mount_flags()
        seen = set()
        for dev in lsblk():
            if not is_card(dev):
                continue
            key = card_key(dev)
            seen.add(key)
            volumes = [volume(part, flags) for part in volumes_of(dev)]
            self.state.saw(
                key, card_name(dev), number(dev.get("size")), [v["label"] for v in volumes if v["label"]], now
            )
            if self.state.get(key).get("readonly") and not dev.get("ro"):
                self.keep_readonly(key, volumes, now)
        for key in self.present - seen:
            self.state.update(key, last_seen=now)
            self.keep_errors.pop(key, None)
        self.present = seen

    def keep_readonly(self, key: str, volumes: list[dict], now: float) -> None:
        # Files mounts a card writable whenever it is plugged in or comes back by itself.
        writable = [v for v in volumes if v["can_unmount"] and v["readonly"] is False]
        if not writable:
            self.keep_errors.pop(key, None)
            return
        error = self.keep_errors.get(key)
        if (error and error[1] > now) or not self.ops.acquire(blocking=False):
            return
        try:
            for view in writable:
                remount(view["device"], True)
                log(f"{view['device']} (card {key}) was mounted writable; mounted it read-only again")
            self.keep_errors.pop(key, None)
        except AgentError as exc:
            self.keep_errors[key] = (str(exc), now + RETRY_SECONDS)
            log(f"cannot keep card {key} read-only: {exc}")
        finally:
            self.ops.release()


# HTTP on a Unix socket -------------------------------------------------------------------


def text_field(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise AgentError(400, f"missing {name}")
    return value


class Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        agent = self.server.agent
        routes = {"/disks": agent.disks, "/cards": agent.cards}
        self.answer(routes.get(self.path))

    def do_POST(self) -> None:  # noqa: N802
        agent = self.server.agent
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.send_json(400, {"detail": "the body must be a JSON object"})
            return
        routes = {
            "/mount": lambda: agent.mount(text_field(body, "device"), bool(body.get("readonly"))),
            "/unmount": lambda: agent.unmount(text_field(body, "device")),
            "/readonly": lambda: agent.set_readonly(text_field(body, "serial"), bool(body.get("readonly"))),
            "/eject": lambda: agent.eject(text_field(body, "serial")),
        }
        action = routes.get(self.path)
        self.answer(action and (lambda: {"message": action()}))

    def answer(self, produce: object) -> None:
        if produce is None:
            self.send_json(404, {"detail": "not found"})
            return
        try:
            self.send_json(200, produce())
        except AgentError as exc:
            self.send_json(exc.status, {"detail": str(exc)})
        except Exception as exc:
            log(traceback.format_exc())
            self.send_json(500, {"detail": f"host helper error: {exc}"})

    def send_json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # a Unix socket has no client address to log, and polling would flood the journal


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, agent: Agent) -> None:
        self.agent = agent
        super().__init__(str(path), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hostwatch host helper: disks and USB cards.")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True, help="folder for cards.json")
    args = parser.parse_args(argv)
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    args.socket.parent.chmod(0o700)
    args.socket.unlink(missing_ok=True)
    agent = Agent(args.state)
    old = os.umask(0o177)
    try:
        server = Server(args.socket, agent)
    finally:
        os.umask(old)
    threading.Thread(target=agent.watch, daemon=True).start()
    threading.Thread(target=agent.log.load_history, daemon=True).start()
    log(f"listening on {args.socket}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
