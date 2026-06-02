#!/usr/bin/env python3
"""Read-only probe: validates baud + CRC + address by trying safe queries.

Does NOT move motors / open doors / dispense.
"""
from __future__ import annotations
import argparse
import glob
import os
import sys
import time
import serial

from wm800 import build_frame, parse_frame, FrameError, DEV_START

BAUD = 9600
CANDIDATE_ADDRS = [0x00, 0x01, 0x02, 0x03]


def autodetect_port() -> str | None:
    """Return first plausible USB serial port, or None."""
    candidates = sorted(
        glob.glob("/dev/tty.usbserial-*")    # macOS
        + glob.glob("/dev/cu.usbserial-*")    # macOS (alternate)
        + glob.glob("/dev/ttyUSB*")           # Linux
        + glob.glob("/dev/ttyACM*")           # Linux (CDC)
    )
    return candidates[0] if candidates else None


def hex_(b): return " ".join(f"{x:02X}" for x in b)


def read_one_frame(ser, deadline):
    # hunt for 0xFF then read a full frame
    while time.monotonic() < deadline:
        ser.timeout = max(0.05, deadline - time.monotonic())
        head = ser.read(1)
        if not head:
            return None
        if head[0] != DEV_START:
            continue
        rest = ser.read(8)
        if len(rest) < 8:
            return None
        dlen = int.from_bytes(rest[7:9], "big")
        tail = ser.read(dlen + 2)
        if len(tail) < dlen + 2:
            return None
        try:
            return parse_frame(head + rest + tail)
        except FrameError as e:
            print(f"  [bad frame] {e}; raw={hex_(head+rest+tail)}")
            return None
    return None


def probe_addr(ser, addr):
    print(f"\n--- trying addr 0x{addr:08X} ---")
    for cmd, payload, label, timeout in [
        (0x05, b"", "0x05 query status", 0.5),
        (0x01, bytes([0x00, 0x00]), "0x01 query version (motor board)", 1.0),
        (0x01, bytes([0x01, 0x00]), "0x01 query version (platform board)", 1.0),
    ]:
        f = build_frame(addr, cmd, payload)
        ser.reset_input_buffer()
        ser.write(f)
        print(f"TX [{label}]: {hex_(f)}")
        deadline = time.monotonic() + timeout
        rep = read_one_frame(ser, deadline)
        if rep is None:
            print("  RX: <timeout>")
            continue
        print(f"  RX: cmd=0x{rep.cmd:02X} addr=0x{rep.addr:08X} data={hex_(rep.data)}")
        if rep.cmd == cmd:
            return True
    return False


def main():
    p = argparse.ArgumentParser(description="WM800 read-only probe")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--baud", type=int, default=BAUD)
    p.add_argument("--addrs", help="comma-separated hex addresses to try, e.g. 0,1,2,3",
                   default=",".join(f"0x{a:X}" for a in CANDIDATE_ADDRS))
    args = p.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("ERR: no serial port given and none auto-detected.", file=sys.stderr)
        print("     pass --port /dev/tty.usbserial-XXXX (macOS) or /dev/ttyUSB0 (Linux)", file=sys.stderr)
        return 2
    if not os.path.exists(port):
        print(f"ERR: port not found: {port}", file=sys.stderr)
        return 2

    addrs = [int(a, 0) for a in args.addrs.split(",")]
    print(f"opening {port} @ {args.baud} 8N1")
    ser = serial.Serial(port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0.3)

    # Drain any unsolicited reports first
    time.sleep(0.2)
    ser.reset_input_buffer()

    for addr in addrs:
        if probe_addr(ser, addr):
            print(f"\nGOT REPLY on addr 0x{addr:02X}.  Use --addr 0x{addr:X}.")
            ser.close()
            return 0

    print("\nNo reply on any common address.")
    print("Possible causes:")
    print("  - device not powered, or A/B (RS485) swapped")
    print("  - device address differs (check dip switches)")
    print("  - port wrong: try `ls /dev/tty.usbserial-* /dev/ttyUSB*`")
    ser.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
