"""The host helper's kernel log reading and issue rules, on lines copied from a real journal."""

from __future__ import annotations

import agent

PLUG_IN = [
    "usb 1-5: new high-speed USB device number 11 using xhci_hcd",
    "usb 1-5: New USB device found, idVendor=0951, idProduct=1666, bcdDevice= 1.10",
    "usb 1-5: Product: DataTraveler 3.0",
    "usb 1-5: Manufacturer: Kingston",
    "usb 1-5: SerialNumber: 1C1B0D0194C9E85149282497",
    "usb-storage 1-5:1.0: USB Mass Storage device detected",
    "scsi host3: usb-storage 1-5:1.0",
    "sd 3:0:0:0: [sdc] 967311360 512-byte logical blocks: (495 GB/461 GiB)",
    "sd 3:0:0:0: [sdc] Write Protect is off",
    "sd 3:0:0:0: [sdc] Attached SCSI removable disk",
]
MOUSE = [
    "usb 1-2: new full-speed USB device number 12 using xhci_hcd",
    "usb 1-2: Product: MX Vertical Advanced Ergonomic Mouse",
    "usb 1-2: SerialNumber: 60C0AD3797813CA2",
]


def parse(lines: list[tuple[float, str]]) -> agent.BootParser:
    parser = agent.BootParser("boot")
    for when, line in lines:
        parser.feed(when, line)
    return parser


def at(start: float, lines: list[str]) -> list[tuple[float, str]]:
    return [(start, line) for line in lines]


def test_storage_connection_is_read_and_mice_are_not_cards() -> None:
    parser = parse(at(100, PLUG_IN) + at(110, MOUSE))
    [card] = parser.storage()
    assert (card.serial, card.name, card.kname) == ("1C1B0D0194C9E85149282497", "Kingston DataTraveler 3.0", "sdc")
    assert card.size == 967311360 * 512
    assert card.write_protect is False
    assert card.disconnected is None
    assert len(parser.connections) == 2


def test_errors_right_after_a_disconnect_still_belong_to_the_card() -> None:
    lines = at(100, PLUG_IN) + [
        (200, "usb 1-5: USB disconnect, device number 11"),
        (200, "I/O error, dev sdc, sector 240047184 op 0x0:(READ) flags 0x80700 phys_seg 1 prio class 2"),
        (200, "device offline error, dev sdc, sector 240047440 op 0x0:(READ) flags 0x80700 phys_seg 1 prio class 2"),
    ]
    lines += at(201, PLUG_IN) + [(203, "FAT-fs (sdc1): Volume was not properly unmounted. Some data may be corrupt.")]
    parser = parse(lines)
    first, second = parser.storage()
    assert first.disconnected == 200
    assert [event.kind for event in first.events] == ["read", "read"]
    assert [event.kind for event in second.events] == ["dirty"]

    [issue] = agent.build_issues(parser.storage())
    assert issue["title"] == "Disconnected by itself while being read"
    assert issue["severity"] == "medium"
    assert issue["details"] == ["2 failed reads", "back 1 s later", "file system left unclean"]


def test_a_failed_copy_counts_each_write_once() -> None:
    lines = at(100, PLUG_IN) + [
        (150, "device offline error, dev sdc, sector 437390576 op 0x1:(WRITE) flags 0x104000 phys_seg 8 prio class 2"),
        (150, "Buffer I/O error on dev sdc1, logical block 33956, lost async page write"),
        (151, "FAT-fs (sdc1): unable to read inode block for updating (i_pos 6990823430)"),
    ]
    [issue] = agent.build_issues(parse(lines).storage())
    assert issue["title"] == "Copy to the card failed"
    assert issue["severity"] == "high"
    assert issue["details"] == ["1 failed write, data not saved", "1 file system error"]


def test_a_write_protected_card_refuses_writes_rather_than_losing_a_copy() -> None:
    protected = [line.replace("Write Protect is off", "Write Protect is on") for line in PLUG_IN]
    lines = at(100, protected) + [
        (150, "FAT-fs (sdc1): error, corrupted directory (invalid entries)"),
        (150, "critical target error, dev sdc, sector 44288 op 0x1:(WRITE) flags 0x0 phys_seg 1 prio class 2"),
        (150, "Buffer I/O error on dev sdc1, logical block 1, lost async page write"),
    ]
    [issue] = agent.build_issues(parse(lines).storage())
    assert issue["title"] == "File system errors"
    assert issue["details"] == ["1 write refused", "1 file system error"]


def test_pulling_a_card_out_is_not_an_issue() -> None:
    lines = at(100, PLUG_IN) + [(500, "usb 1-5: USB disconnect, device number 11")] + at(530, PLUG_IN)
    assert agent.build_issues(parse(lines).storage()) == []


def test_turning_on_write_protection_is_reported_once() -> None:
    protected = [line.replace("Write Protect is off", "Write Protect is on") for line in PLUG_IN]
    lines = at(100, PLUG_IN) + [(200, "usb 1-5: USB disconnect, device number 11")]
    lines += at(300, protected) + [(400, "usb 1-5: USB disconnect, device number 11")] + at(500, protected)
    issues = agent.build_issues(parse(lines).storage())
    assert [issue["kind"] for issue in issues] == ["protect"]


def test_harmless_file_system_messages_are_ignored() -> None:
    assert agent.classify("FAT-fs (sdb1): utf8 is not a recommended IO charset for FAT filesystems") is None
    assert agent.classify("EXT4-fs (sdb1): mounted filesystem with ordered data mode") is None
    assert agent.classify("EXT4-fs error (device sdb1): ext4_find_entry:1455: inode #2: reading directory") == (
        "fs",
        "sdb1",
    )


def test_volume_rules() -> None:
    root = {"path": "/dev/sda4", "name": "sda4", "fstype": "ext4", "mountpoints": ["/var/snap/x", "/"]}
    free = {"path": "/dev/sda6", "name": "sda6", "fstype": "ntfs", "label": "Free", "mountpoints": [None]}
    msr = {"path": "/dev/sda2", "name": "sda2", "fstype": None, "mountpoints": [None]}
    swap = {"path": "/dev/sda5", "name": "sda5", "fstype": "swap", "mountpoints": ["[SWAP]"]}
    flags = {"/dev/sda4": False}
    assert agent.volume(root, flags)["system"] and not agent.volume(root, flags)["can_unmount"]
    assert agent.volume(free, flags)["can_mount"] and agent.volume(free, flags)["readonly"] is None
    assert not agent.volume(msr, flags)["can_mount"]
    assert agent.volume(swap, flags)["system"]
