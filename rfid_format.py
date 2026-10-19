#!/usr/bin/env python3
"""Linux output-format tool for the 1a86:2366 "IC ID card reader" (Miao-Xiao-Zhi).

Reimplements the protocol of 只读改格式软件EN-Card_Reader.exe over hidraw.
Only the Python standard library is needed.

Protocol (vendor HID interface, 64-byte reports, no report IDs):
  query : F1                       -> reply F1 c0 c1 c2 c3 ver id(4)
  set   : F2 c0 c1 c2 c3           -> reply F2 ...
  c0 = regular (125 kHz EM/ID) format, c1 = IC-MF0 format,
  c2 = HID card format, c3 = flags (bit0 enter, bit1 WG comma, bit7 software-only)
"""
import argparse
import glob
import os
import select
import sys
import termios
import time

VID, PID = 0x1A86, 0x2366

REGULAR = [
    "10-digit hexadecimal (all 5 bytes)",
    "8-digit hexadecimal",
    "8-digit hexadecimal, reversed",
    "5-digit decimal (last 2 bytes)",
    "8-digit decimal (last 3 bytes)",
    "8-digit decimal (last 4 bytes)",
    "10-digit decimal (last 3 bytes)",
    "10-digit decimal (last 4 bytes)",
    "10-digit decimal, reversed (4 bytes)",
    "10-digit decimal + WG26",
    "10-digit decimal + space + WG26",
    "13-digit decimal",
    "WG26",
    "WG34",
]
IC_MF0 = [
    "14-digit hexadecimal",
    "14-digit hexadecimal, reversed",
    "first 5 bytes",
    "first 4 bytes",
    "last 5 bytes",
    "4 bytes starting with 88",
]
HID_CARD = [
    "11-digit hexadecimal",
    "12-digit hexadecimal",
    "Wiegand",
    "Wiegand + space + card format",
]
FLAG_ENTER, FLAG_WG_COMMA, FLAG_SOFTWARE_ONLY = 0x01, 0x02, 0x80


def find_device(keyboard=False):
    """Return the hidraw node of the reader's vendor (0xFF00) or keyboard interface."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device/uevent")) as f:
                uevent = f.read()
            with open(os.path.join(node, "device/report_descriptor"), "rb") as f:
                desc = f.read()
        except OSError:
            continue
        if f"{VID:08X}:{PID:08X}" not in uevent.upper():
            continue
        is_vendor = desc.startswith(b"\x06\x00\xff")  # Usage Page (Vendor 0xFF00)
        if is_vendor != keyboard:
            return "/dev/" + os.path.basename(node)
    return None


class ReaderTimeout(Exception):
    pass


class Reader:
    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            sys.exit(f"No permission to open {path}. Run with sudo, or install the udev rule "
                     f"(see 99-rfid-reader.rules).")

    def close(self):
        os.close(self.fd)

    def transact(self, payload, timeout=2.0):
        report = bytes(payload) + bytes(64 - len(payload))
        os.write(self.fd, b"\x00" + report)  # leading 0 = no report ID
        cmd = payload[0]
        while True:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                raise ReaderTimeout(f"Timeout waiting for reply to command {cmd:02X}")
            data = os.read(self.fd, 64)
            if data and data[0] == cmd:  # skip card-read packets
                return data

    def query(self):
        r = self.transact([0xF1])
        return list(r[1:5]), r[5], int.from_bytes(r[6:10], "big")

    def set_config(self, cfg):
        self.transact([0xF2] + cfg)


def keycode_name(code):
    if 0x04 <= code <= 0x1D:
        return chr(ord("A") + code - 0x04)
    if 0x1E <= code <= 0x27:
        return "1234567890"[code - 0x1E]
    if 0x59 <= code <= 0x62:
        return "KP" + "1234567890"[code - 0x59]
    return {0x28: "ENTER", 0x2C: "SPACE", 0x36: "COMMA", 0x58: "KP_ENTER",
            0x53: "NUMLOCK"}.get(code, f"0x{code:02X}")


def capture_card(kbd_fd, timeout=30.0):
    """Wait for a card swipe and return the keys the reader typed (US key names)."""
    keys, last = [], set()
    deadline = time.time() + timeout
    while True:
        wait = (deadline - time.time()) if not keys else 0.5
        if wait <= 0:
            return keys
        ready, _, _ = select.select([kbd_fd], [], [], wait)
        if not ready:
            return keys
        r = os.read(kbd_fd, 64)
        mods, pressed = r[0], [c for c in r[2:8] if c]
        for c in pressed:
            if c not in last:
                keys.append(("Alt+" if mods & 0x44 else "") + ("Shift+" if mods & 0x22 else "")
                            + keycode_name(c))
        last = set(pressed)


def probe(rd, cfg, kbd_path, tests):
    """Try each (label, config) in tests, record what the reader types, then restore cfg."""
    kbd_fd = os.open(kbd_path, os.O_RDONLY)
    results = []
    try:
        for label, test_cfg in tests:
            print(f"\n[{label}] cfg={test_cfg}", flush=True)
            try:
                rd.set_config(test_cfg)
                got, _, _ = rd.query()
            except ReaderTimeout:
                print("  reader did not answer -> value rejected, skipped")
                results.append((label, "(rejected by reader)"))
                time.sleep(1)
                continue
            if got != test_cfg:
                print(f"  (reader kept {got})")
            print("  Swipe a card (30 s)...", flush=True)
            keys = capture_card(kbd_fd)
            line = " ".join(keys) if keys else "(nothing)"
            print("  typed:", line)
            results.append((label, line))
    finally:
        for _ in range(5):
            try:
                rd.set_config(cfg)
                if rd.query()[0] == cfg:
                    break
            except ReaderTimeout:
                time.sleep(1)
        else:
            print(f"WARNING: could not restore the configuration {cfg}, run --raw-flags/--regular manually")
        os.close(kbd_fd)
        if sys.stdin.isatty():  # drop the swiped keystrokes so the shell doesn't run them
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    print("\nOriginal configuration restored.\n\nSummary:")
    for label, line in results:
        print(f"  {label:14} {line}")


def name(table, idx):
    return table[idx] if idx < len(table) else f"unknown ({idx})"


def print_config(cfg, version=None, dev_id=None):
    if version is not None:
        print(f"Reader v{version:02X}  ID:{dev_id:X}")
    print(f"  regular format : [{cfg[0]:2}] {name(REGULAR, cfg[0])}")
    print(f"  IC-MF0 format  : [{cfg[1]:2}] {name(IC_MF0, cfg[1])}")
    print(f"  HID format     : [{cfg[2]:2}] {name(HID_CARD, cfg[2])}")
    print(f"  enter          : {'on' if cfg[3] & FLAG_ENTER else 'off'}")
    print(f"  WG comma       : {'on' if cfg[3] & FLAG_WG_COMMA else 'off'}")
    print(f"  software only  : {'on' if cfg[3] & FLAG_SOFTWARE_ONLY else 'off'}"
          "  (on = no keyboard output!)")


def list_formats():
    for title, table in (("--regular", REGULAR), ("--ic", IC_MF0), ("--hid", HID_CARD)):
        print(title)
        for i, n in enumerate(table):
            print(f"  {i:2}  {n}")


def on_off(v):
    if v.lower() in ("on", "1", "yes", "true"):
        return True
    if v.lower() in ("off", "0", "no", "false"):
        return False
    raise argparse.ArgumentTypeError("use on/off")


def main():
    p = argparse.ArgumentParser(description="Set the output format of the 1a86:2366 USB RFID reader.")
    p.add_argument("--device", help="hidraw node (auto-detected by default)")
    p.add_argument("--list", action="store_true", help="list format numbers")
    p.add_argument("--hex10", action="store_true", help="shortcut for --regular 0 (10-digit hex, all 5 bytes)")
    p.add_argument("--regular", type=int, help="regular (125 kHz) card format number")
    p.add_argument("--ic", type=int, help="IC-MF0 card format number")
    p.add_argument("--hid", type=int, help="HID card format number")
    p.add_argument("--enter", type=on_off, help="send Enter after the ID (on/off)")
    p.add_argument("--wg-comma", type=on_off, help="WG comma (on/off)")
    p.add_argument("--software-only", type=on_off, help="disable keyboard output (on/off)")
    p.add_argument("--raw-flags", type=lambda v: int(v, 0), help="set the flag byte c3 directly (e.g. 0x05)")
    p.add_argument("--probe", action="store_true",
                   help="test the undocumented flag bits 2-6 one by one (swipe a card each time)")
    p.add_argument("--probe-formats", action="store_true",
                   help="test undocumented regular format numbers 14-20 (swipe a card each time)")
    a = p.parse_args()

    if a.list:
        list_formats()
        return

    path = a.device or find_device()
    if not path:
        sys.exit("Reader (1a86:2366) not found.")
    rd = Reader(path)
    try:
        return run(rd, a, path)
    except ReaderTimeout as e:
        sys.exit(str(e))
    finally:
        rd.close()


def run(rd, a, path):
    cfg, version, dev_id = rd.query()
    new = list(cfg)
    if a.hex10:
        a.regular = 0
    for i, (val, table) in enumerate(((a.regular, REGULAR), (a.ic, IC_MF0), (a.hid, HID_CARD))):
        if val is not None:
            if not 0 <= val < len(table):
                sys.exit(f"Invalid format number {val}, see --list")
            new[i] = val
    for flag, val in ((FLAG_ENTER, a.enter), (FLAG_WG_COMMA, a.wg_comma),
                      (FLAG_SOFTWARE_ONLY, a.software_only)):
        if val is not None:
            new[3] = (new[3] | flag) if val else (new[3] & ~flag & 0xFF)
    if a.raw_flags is not None:
        new[3] = a.raw_flags & 0xFF

    print(f"Device: {path}")
    print_config(cfg, version, dev_id)
    if a.probe or a.probe_formats:
        kbd = find_device(keyboard=True)
        if not kbd:
            sys.exit("Keyboard interface of the reader not found.")
        base = list(cfg)
        base[3] &= ~FLAG_SOFTWARE_ONLY & 0xFF  # keyboard output must be on
        tests = [("baseline", base)]
        if a.probe:
            tests += [(f"flag bit {b}", base[:3] + [base[3] | (1 << b)]) for b in range(2, 7)]
        if a.probe_formats:
            tests += [(f"format {n}", [n] + base[1:]) for n in range(14, 21)]
        print("\nTip: keep NumLock ON, the keys are read directly from the reader.")
        probe(rd, cfg, kbd, tests)
        return
    if new != cfg:
        rd.set_config(new)
        cfg, _, _ = rd.query()
        print("\nNew configuration:")
        print_config(cfg)
        if cfg != new:
            sys.exit("Warning: reader did not accept the new configuration.")


if __name__ == "__main__":
    main()
