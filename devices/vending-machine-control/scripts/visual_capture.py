#!/usr/bin/env python3
"""Visual verification: send a command, snapshot the screen multiple times.

Photo Booth must already be running with the camera pointing at the machine.
Uses macOS `screencapture` so no terminal camera permission is needed.
"""
from __future__ import annotations
import subprocess
import time
import sys
import struct
import os
import serial
from wm800 import build_frame, parse_frame, DEV_START

PORT = "/dev/tty.usbserial-0001"
ADDR = 0x00
OUT = "/tmp/wm800_cam"
os.makedirs(OUT, exist_ok=True)


def shoot(name):
    p = f"{OUT}/{name}.jpg"
    subprocess.run(["screencapture", "-x", "-t", "jpg", p], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def hex_(b): return " ".join(f"{x:02X}" for x in b)


def listen(ser, duration, label=""):
    """Read for `duration` seconds, parse frames, auto-ACK 0xE1."""
    end = time.time() + duration
    buf = b""
    frames = []
    while time.time() < end:
        ser.timeout = max(0.05, end - time.time())
        chunk = ser.read(256)
        if not chunk: continue
        buf += chunk
        i = 0
        while i < len(buf):
            if buf[i] != DEV_START:
                i += 1; continue
            if i + 9 > len(buf): break
            dlen = (buf[i+7] << 8) | buf[i+8]
            total = 9 + dlen + 2
            if i + total > len(buf): break
            try:
                f = parse_frame(buf[i:i+total])
                frames.append((time.time(), f))
                if f.cmd == 0xE1 and len(f.data) >= 9:
                    ser.write(build_frame(ADDR, 0xE1, bytes([f.data[8]])))
                print(f"  [{label}+{time.time()-end+duration:5.2f}s] cmd=0x{f.cmd:02X} data={hex_(f.data)}")
            except Exception:
                pass
            i += total
        buf = buf[i:]
    return frames


def case(label, cmd, payload, action_secs, frame_count=4):
    print(f"\n{'='*70}\n{label}\n{'='*70}")

    ser = serial.Serial(PORT, 9600, timeout=0.5)

    # baseline frame
    p = shoot(f"{label}_0_before")
    print(f"  baseline: {p}")

    # send command
    ser.reset_input_buffer()
    tx = build_frame(ADDR, cmd, payload)
    print(f"  TX cmd=0x{cmd:02X}: {hex_(tx)}")
    t0 = time.time()
    ser.write(tx)

    # take a frame immediately and then `frame_count - 1` evenly spaced during action
    interval = action_secs / max(1, frame_count - 1)
    captures = []
    for i in range(frame_count):
        target = t0 + i * interval
        # listen until target time
        remaining = target - time.time()
        if remaining > 0:
            listen(ser, remaining, label=label)
        captures.append(shoot(f"{label}_{i+1}_t{int((time.time()-t0)*1000)}ms"))
        print(f"  frame {i+1} @ +{(time.time()-t0):.2f}s: {captures[-1]}")

    # finish listening to action_secs
    rem = (t0 + action_secs) - time.time()
    if rem > 0:
        listen(ser, rem, label=label)

    # final frame
    final = shoot(f"{label}_final")
    print(f"  final: {final}")

    ser.close()
    return [p] + captures + [final]


def main():
    # Make sure Photo Booth is on top
    subprocess.run(["osascript", "-e", 'tell application "Photo Booth" to activate'])
    time.sleep(2)

    # Test 1: 0x65 open door — known good, used to validate methodology
    case("65_open_door", 0x65,
         b"\x00\x00\x00" + struct.pack(">H", 10) + b"\x01",
         action_secs=15, frame_count=5)

    # gap
    time.sleep(3)

    # Test 2: 0x3D platform motor — no reply earlier; see if motor actually moves
    case("3D_motor", 0x3D, b"\x01\x03", action_secs=8, frame_count=4)

    # 0x2A reset to recover position if 3D moved anything
    print("\n  → 0x2A reset to recover platform")
    ser = serial.Serial(PORT, 9600, timeout=2.0)
    ser.write(build_frame(ADDR, 0x2A, b""))
    listen(ser, 30, label="2A_reset")
    ser.close()

    # Test 3: 0x23 servo restart — should give visible/audible servo behavior
    case("23_servo", 0x23, b"\x02\x14", action_secs=30, frame_count=6)


if __name__ == "__main__":
    main()
