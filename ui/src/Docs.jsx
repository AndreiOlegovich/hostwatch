import { useEffect, useState } from "react";
import { CopyButton } from "./CopyButton.jsx";
import { getJSON } from "./lib.js";

// Where each page gets its information. `info` names the /api/info entry that says whether it is there.
const SOURCES = [
  {
    page: "Claude Code",
    what: "Session transcripts, one JSON Lines file per session",
    where: (home) => `${home}/.claude/projects/*/*.jsonl`,
    how: "Mounted read-only into the container",
    info: "claude",
  },
  {
    page: "OpenCode",
    what: "Sessions, messages and tool calls (SQLite)",
    where: (home) => `${home}/.local/share/opencode/opencode.db`,
    how: "Mounted read-only; the rest of that folder, auth.json included, is hidden",
    info: "opencode",
  },
  {
    page: "Sessions",
    what: "Commits made while a session was active, and commits git printed inside it",
    where: () => "Each git repository a session worked in",
    how: "Mounted read-only; read with git log",
  },
  {
    page: "Disks",
    what: "Disks, partitions, labels, file systems, sizes and free space",
    where: () => "lsblk",
    how: "Run by the host helper",
    info: "agent",
  },
  {
    page: "Disks, USB cards",
    what: "Where each partition is mounted, and whether read-only",
    where: () => "findmnt",
    how: "Run by the host helper",
    info: "agent",
  },
  {
    page: "Disks, USB cards",
    what: "Mount, unmount and power off, the same service Files uses",
    where: () => "udisks (udisksctl)",
    how: "Run by the host helper; no password for drives of the logged-in user",
    info: "agent",
  },
  {
    page: "USB cards",
    what: "Plug-ins and disconnects, device names, write protection, I/O and file system errors",
    where: () => "Kernel log in the systemd journal (journalctl -k -b <boot>)",
    how: "Read by the host helper, one boot at a time",
    info: "agent",
  },
  {
    page: "USB cards",
    what: "Labels of cards seen, when, and which to keep read-only",
    where: (home) => `${home}/.local/state/hostwatch/cards.json`,
    how: "Written by the host helper",
  },
  {
    page: "USB cards",
    what: "What the kernel log said about USB cards in earlier boots, so it is read once",
    where: (home) => `${home}/.local/state/hostwatch/kernel-history.json`,
    how: "Written by the host helper; delete it to read the journal again",
  },
];

function sections(home, user) {
  const media = `/media/${user}`;
  return [
    {
      title: "Hostwatch",
      intro:
        "Run from the project folder. up builds the image, starts the host helper as a systemd user service and " +
        "(re)starts the container; neither starts by itself after a restart.",
      commands: [
        { cmd: "./hostwatch.py up", note: "Build and start everything at http://127.0.0.1:8766" },
        { cmd: "./hostwatch.py up --no-build --port 8766 --grace 30", note: "Reuse the last image; other port or grace minutes" },
        { cmd: "./hostwatch.py down", note: "Stop the container and the host helper" },
        { cmd: "./hostwatch.py logs -f", note: "Follow the container log" },
        { cmd: "./hostwatch.py logs -f --agent", note: "Follow the host helper log" },
        { cmd: "systemctl --user status hostwatch-agent", note: "Is the host helper running?" },
        { cmd: "curl -s http://127.0.0.1:8766/api/cards | python3 -m json.tool | less", note: "The data behind USB cards" },
        { cmd: "curl -s http://127.0.0.1:8766/api/disks | python3 -m json.tool", note: "The data behind Disks" },
        { cmd: "cd ui && npm run dev", note: "Work on the UI with live reload, talking to the running container" },
      ],
    },
    {
      title: "Disks: what is there and where it is mounted",
      commands: [
        { cmd: "lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS,MODEL,TRAN,RO", note: "Every disk and partition" },
        { cmd: "df -h", note: "Free space of everything mounted" },
        { cmd: `findmnt -no OPTIONS ${media}/IMG | cut -d, -f1`, note: "ro (read-only) or rw (writable)" },
        { cmd: "ls -l /dev/disk/by-label/ /dev/disk/by-id/", note: "Stable names: sdb and sdc swap around, these don't" },
      ],
    },
    {
      title: "Mount, unmount, power off",
      intro: "No sudo needed: these are the same actions as the Mount and Unmount buttons and the ⏏ in Files.",
      commands: [
        { cmd: "udisksctl mount -b /dev/disk/by-label/Free", note: `Mount; it opens at ${media}/<label>` },
        { cmd: "udisksctl mount -b /dev/disk/by-label/IMG -o ro", note: "Mount read-only from the start" },
        { cmd: "udisksctl unmount -b /dev/disk/by-label/IMG", note: "Unmount; close windows showing it first" },
        { cmd: `lsof +f -- ${media}/IMG`, note: 'Who keeps it busy when unmount says "target is busy"' },
        { cmd: "udisksctl power-off -b /dev/disk/by-id/usb-<name>-0:0", note: "Switch a USB card off: safe to pull out" },
      ],
    },
    {
      title: "Read-only and writable",
      intro:
        "The Read-only switch on USB cards unmounts and mounts again with ro, and remembers it: whenever the card " +
        "comes back it is mounted read-only again. The commands below last until the card is unplugged.",
      commands: [
        { cmd: `sudo mount -o remount,ro ${media}/IMG`, note: "Make a mounted disk read-only" },
        { cmd: `sudo mount -o remount,rw ${media}/IMG`, note: "Make it writable again" },
        { cmd: "sudo blockdev --setro /dev/sdX", note: "Read-only at the device level: even the kernel can't write" },
        { cmd: "sudo blockdev --setrw /dev/sdX", note: "Undo it (not possible when the card itself is write-protected)" },
        { cmd: "blockdev --getro /dev/sdX", note: "1 means read-only at the device level" },
      ],
    },
    {
      title: "USB cards and the kernel log",
      intro: "The USB cards page is built from these. Check the kernel log first whenever a card acts strange.",
      commands: [
        {
          cmd: "journalctl -k -f | grep --line-buffered -Ei 'usb|disconnect|I/O error|offline|FAT-fs|lost async|reset'",
          note: "Watch live while copying (Ctrl+C stops)",
        },
        {
          cmd: "journalctl -k --since today | grep -Ei 'disconnect|I/O error|offline|FAT-fs|lost async|reset'",
          note: "Today's trouble",
        },
        { cmd: "journalctl --list-boots", note: "Each start of the computer, with its number" },
        { cmd: "journalctl -k -b -1 | grep -i usb", note: "USB lines from the previous boot" },
        { cmd: "journalctl -k | grep -E 'SerialNumber|Write Protect|logical blocks'", note: "Which cards came in, and how big" },
      ],
      notes: [
        "USB disconnect that you didn't cause, I/O error, device offline error: the card dropped off.",
        "lost async page write: data on its way to the card was thrown away; a copy failed.",
        "FAT-fs … error or corrupted directory: the file system is damaged.",
        "Write Protect is on: the card refuses all writes; worn-out cards switch this on by themselves.",
        "Volume was not properly unmounted: it was pulled out or dropped off while mounted.",
      ],
    },
    {
      title: "Health checks",
      intro: "Unmount before checking. The commands here only read unless a note says otherwise.",
      commands: [
        { cmd: "sudo fsck.vfat -n /dev/disk/by-label/IMG", note: "Check a FAT32 card; -n changes nothing" },
        { cmd: "sudo ntfsfix -n /dev/disk/by-label/Free", note: "Check an NTFS partition; -n changes nothing" },
        { cmd: "sudo tune2fs -l /dev/sda4 | grep -iE 'state|errors'", note: "The Ubuntu partition: clean is good" },
        { cmd: "sudo smartctl -H -A /dev/sda", note: "Laptop disk health (smartmontools); USB sticks have no SMART" },
        { cmd: "sudo badblocks -sv -b 4096 /dev/sdX", note: "Read every sector of a card, hours; never add -w or -n" },
        {
          cmd: "sudo f3probe --destructive --time-ops /dev/sdX",
          note: "Is the card fake? Destroys everything on it",
          danger: true,
        },
      ],
    },
    {
      title: "Copying safely",
      intro: "Copy, check the copy, then delete. Never Move onto a USB card: a dropped card takes the file with it.",
      commands: [
        { cmd: `rsync -rt --partial --info=progress2 ${media}/IMG/DCIM ${media}/Free/backup/`, note: "Copy; run again to continue" },
        {
          cmd: `rsync -rtn --modify-window=1 --out-format='%n' ${media}/IMG/DCIM ${media}/Free/backup/ | grep -v '/$'`,
          note: "Quick check by names, sizes and dates; no output means it matches",
        },
        {
          cmd: `rsync -rcn --out-format='%n' ${media}/IMG/DCIM ${media}/Free/backup/ | grep -v '/$'`,
          note: "Full check, byte by byte (slow)",
        },
        { cmd: "sha256sum file.zip", note: "Fingerprint, to compare with a known one" },
        { cmd: "cmp fileA fileB", note: "Compare two files; no output means identical" },
      ],
    },
  ];
}

function Command({ cmd, note, danger }) {
  return (
    <li className={`doc-command${danger ? " danger" : ""}`}>
      <div className="command">
        <code>{cmd}</code>
        <CopyButton text={cmd} label="Copy" className="btn small" />
      </div>
      {note && <p className="hint">{note}</p>}
    </li>
  );
}

export function DocsPage() {
  const [info, setInfo] = useState(null);
  useEffect(() => {
    getJSON("/api/info").then(setInfo, () => setInfo({}));
  }, []);
  const home = info?.home || "~";
  const user = home.split("/").filter(Boolean).pop() || "$USER";
  const available = (key) => {
    if (!key || !info) return null;
    return key === "agent" ? info.host_agent?.available : info.sources?.[key]?.available;
  };
  return (
    <main className="page">
      <div className="page-body">
        <header className="page-head">
          <h1>Docs</h1>
          <p className="meta">
            Where Hostwatch gets its information, and the commands for doing the same things in a terminal. Your
            longer manual is <code>{home}/Documents/USB_card_and_backups_manual.md</code>.
          </p>
        </header>

        <section>
          <h2>Where the information comes from</h2>
          <ul className="sources">
            {SOURCES.map((source) => {
              const state = available(source.info);
              return (
                <li key={source.what}>
                  <div className="source-head">
                    <span className="tag">{source.page}</span>
                    <strong>{source.what}</strong>
                    {state === true && <span className="tag rw">available</span>}
                    {state === false && <span className="tag gone">not available</span>}
                  </div>
                  <code>{source.where(home)}</code>
                  <p className="hint">{source.how}</p>
                </li>
              );
            })}
          </ul>
          <p className="hint">
            The container runs read-only with no privileges, so it can't mount anything or read the journal. Those
            jobs go to the host helper, <code>agent.py</code>, running as you; the container reaches it through{" "}
            <code>$XDG_RUNTIME_DIR/hostwatch/agent.sock</code>.
          </p>
        </section>

        {sections(home, user).map((section) => (
          <section key={section.title}>
            <h2>{section.title}</h2>
            {section.intro && <p className="doc-intro">{section.intro}</p>}
            <ul className="doc-commands">
              {section.commands.map((command) => (
                <Command key={command.cmd} {...command} />
              ))}
            </ul>
            {section.notes && (
              <ul className="doc-notes">
                {section.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
    </main>
  );
}
