#!/usr/bin/env python3
"""Interactive test bench for the WM800 lower board.

Usage:
    python3 test_wm800.py --port /dev/tty.usbserial-XXXX
    python3 test_wm800.py --port /dev/tty.usbserial-XXXX --addr 1

Runs through a menu of common commands. Start with `status` and `version`
to validate the wiring + CRC variant before doing anything that moves motors.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from wm800 import WM800Client, FrameError, build_frame, crc16_modbus


# 0x05 status code legend (CSV section "0x04&0x05状态码")
STATUS_CODES = {
    0x00: "空闲",
    0x02: "无电机",
    0x12: "右/中/下限位未触碰",
    0x19: "左限位被触碰",
    0x1A: "上限位被触碰",
    0x1E: "取货门没关紧（微动或电机故障）",
    0x22: "平台竖红外被挡或损坏",
}


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def show_report(f):
    print(f"\n[REPORT] cmd=0x{f.cmd:02X} data={hexdump(f.data)}")


def cmd_self_test_crc():
    # XMODEM check value for ASCII "123456789" is 0x31C3.
    crc = crc16_modbus(b"123456789")
    print(f"CRC16-XMODEM self-test: 0x{crc:04X} (expected 0x31C3)")
    assert crc == 0x31C3, "CRC implementation is wrong!"
    print("OK\n")


def cmd_dump_request_frame(addr: int):
    # Print what we'd send for 0x05 query, so you can sniff & compare.
    f = build_frame(addr, 0x05, b"")
    print(f"0x05 query frame: {hexdump(f)}\n")


def cmd_version(c):
    data = c.query_version()
    print(f"version reply ({len(data)}B): {hexdump(data)}")


def cmd_status(c):
    code = c.query_status()
    name = STATUS_CODES.get(code, "未知")
    print(f"status = 0x{code:02X} ({name})")


def cmd_can_dispense(c):
    lane = int(input("lane (decimal, 255 to skip lane check): ") or "255")
    info = c.can_dispense(lane=lane)
    print(info)
    if not info["can_dispense"]:
        print("  reason: 0x{:02X} ({})".format(info["reason"], STATUS_CODES.get(info["reason"], "见协议表")))


def cmd_motor(c):
    lane = int(input("lane number (decimal): "))
    print("motor present" if c.query_motor(lane) else "NO motor on lane")


def cmd_reset(c):
    if input("Confirm reset to origin? [y/N] ").lower() != "y":
        return
    ok = c.reset_to_origin()
    print("reset:", "OK" if ok else "FAIL")


def cmd_dispense(c):
    lane = int(input("lane number (decimal): "))
    # 8B order id from current epoch ms (zero-padded)
    order = int(time.time() * 1000).to_bytes(8, "big")
    print(f"order_id = {hexdump(order)}")
    code = c.dispense(lane, order)
    print(f"dispense status code = 0x{code:02X}")


def cmd_open_door(c):
    code = c.open_pickup_door()
    print("door:", "OK" if code == 0 else f"FAIL (0x{code:02X})")


def cmd_sensors(c):
    print("photo:", hexdump(c.query_photo_sensors()))
    print("micro:", hexdump(c.query_microswitch()))
    for t, name in [(0, "竖红外"), (1, "横红外"), (2, "防夹手"), (3, "层定位")]:
        try:
            print(f"IR {name}: {c.query_ir(t)}")
        except Exception as e:
            print(f"IR {name}: err {e}")


MENU = [
    ("version", "0x01 查版本", cmd_version),
    ("status", "0x05 查状态", cmd_status),
    ("can", "0x04 是否能出货", cmd_can_dispense),
    ("motor", "0x29 查货道电机", cmd_motor),
    ("sensors", "0x34/0x35/0x39 传感器一览", cmd_sensors),
    ("door", "0x65 开取货门", cmd_open_door),
    ("reset", "0x2A 复位回原点", cmd_reset),
    ("dispense", "0x28 出货 (危险)", cmd_dispense),
]


def repl(c):
    while True:
        print("\n=== WM800 test ===")
        for k, desc, _ in MENU:
            print(f"  {k:8s} - {desc}")
        print("  q        - quit")
        choice = input("> ").strip().lower()
        if choice in ("q", "quit", "exit"):
            return
        for k, _, fn in MENU:
            if choice == k:
                try:
                    fn(c)
                except FrameError as e:
                    print(f"FRAME ERROR: {e}")
                except TimeoutError as e:
                    print(f"TIMEOUT: {e}")
                except Exception as e:
                    print(f"ERROR: {e!r}")
                break
        else:
            print(f"unknown: {choice}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", help="serial device, e.g. /dev/tty.usbserial-XXXX")
    p.add_argument("--addr", type=lambda x: int(x, 0), default=0x00000001,
                   help="device address (default 0x1, set per dip switches)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--dry-run", action="store_true",
                   help="run CRC self-test and print sample frame; do not open serial")
    args = p.parse_args()

    cmd_self_test_crc()
    cmd_dump_request_frame(args.addr)

    if args.dry_run or not args.port:
        if not args.port:
            print("(no --port given; exiting after dry-run)")
        return

    if not os.path.exists(args.port):
        print(f"port not found: {args.port}", file=sys.stderr)
        sys.exit(2)

    c = WM800Client(args.port, addr=args.addr, baud=args.baud, on_report=show_report)
    try:
        repl(c)
    finally:
        c.close()


if __name__ == "__main__":
    main()
