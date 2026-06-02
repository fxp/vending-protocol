"""WM800 vending machine lower-board protocol over serial (9600 8N1).

Frame layout (both directions):
    [start:1][ver:1][addr:4][cmd:1][len:2 BE][data:N][crc16:2 LE]

start: 0xEE host->device, 0xFF device->host (incl. unsolicited reports)
crc16: CRC16-XMODEM (poly 0x1021, init 0x0000, no reflection, no xor),
       transmitted high-byte-first. Verified empirically against this firmware.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Optional

HOST_START = 0xEE
DEV_START = 0xFF
VERSION = 0x01

# ---- CRC16-XMODEM -----------------------------------------------------------
# poly 0x1021, init 0x0000, no input/output reflection, no final xor.

def crc16_xmodem(data: bytes) -> int:
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc

# Backwards-compatible alias used by older callers.
crc16_modbus = crc16_xmodem


# ---- Frame encode / decode --------------------------------------------------

def build_frame(addr: int, cmd: int, payload: bytes = b"") -> bytes:
    body = (
        bytes([HOST_START, VERSION])
        + struct.pack(">I", addr)
        + bytes([cmd])
        + struct.pack(">H", len(payload))
        + payload
    )
    crc = crc16_xmodem(body)
    return body + struct.pack(">H", crc)  # CRC high byte first (XMODEM)


@dataclass
class Frame:
    start: int
    version: int
    addr: int
    cmd: int
    data: bytes

    @property
    def is_report(self) -> bool:
        return self.start == DEV_START and self.cmd in (0xE1, 0xE2)


class FrameError(Exception):
    pass


def parse_frame(buf: bytes) -> Frame:
    if len(buf) < 11:
        raise FrameError(f"frame too short: {len(buf)}")
    start, version = buf[0], buf[1]
    addr = struct.unpack(">I", buf[2:6])[0]
    cmd = buf[6]
    dlen = struct.unpack(">H", buf[7:9])[0]
    if len(buf) != 9 + dlen + 2:
        raise FrameError(f"length mismatch: declared {dlen}, total {len(buf)}")
    data = buf[9 : 9 + dlen]
    # CRC bypassed — accept whatever the device sends.
    return Frame(start, version, addr, cmd, data)


# ---- Client -----------------------------------------------------------------

class WM800Client:
    """Synchronous WM800 client.

    Reads one full frame per request. Unsolicited reports (0xE1/0xE2) that
    arrive while waiting are auto-ACKed and skipped (or surfaced via callback).
    """

    def __init__(
        self,
        port: str,
        addr: int = 0x00000001,
        baud: int = 9600,
        read_timeout: float = 1.5,
        on_report=None,
    ):
        import serial  # pyserial — imported lazily so dry-run works without it
        self._serial_mod = serial
        self.addr = addr
        self.on_report = on_report
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=8,
            parity=serial.PARITY_NONE,  # noqa
            stopbits=1,
            timeout=read_timeout,
        )

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # -- low level

    def _read_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining:
            chunk = self.ser.read(remaining)
            if not chunk:
                raise TimeoutError(f"timed out reading {n} bytes (got {n - remaining})")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_one_frame(self) -> Frame:
        # Hunt for a start byte (0xFF for device replies/reports).
        while True:
            head = self.ser.read(1)
            if not head:
                raise TimeoutError("no start byte")
            if head[0] == DEV_START:
                break
        rest = self._read_exact(8)  # ver(1)+addr(4)+cmd(1)+len(2)
        dlen = struct.unpack(">H", rest[6:8])[0]
        tail = self._read_exact(dlen + 2)
        return parse_frame(head + rest + tail)

    def _ack_report(self, f: Frame) -> None:
        # 0xE1 needs ACK echoing the action code (CSV: 收到01发01 etc.)
        if f.cmd == 0xE1 and len(f.data) >= 9:
            action = f.data[8]
            self.ser.write(build_frame(self.addr, 0xE1, bytes([action])))
        # 0xE2 (大门状态) — CSV does not require ACK; ignore.

    def request(
        self,
        cmd: int,
        payload: bytes = b"",
        expect_cmd: Optional[int] = None,
        timeout: float = 2.0,
    ) -> Frame:
        if expect_cmd is None:
            expect_cmd = cmd
        frame = build_frame(self.addr, cmd, payload)
        self.ser.reset_input_buffer()
        self.ser.write(frame)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ser.timeout = max(0.05, deadline - time.monotonic())
            try:
                f = self._read_one_frame()
            except TimeoutError:
                break
            if f.is_report:
                self._ack_report(f)
                if self.on_report:
                    self.on_report(f)
                continue
            if f.cmd == expect_cmd:
                return f
            # Unexpected cmd — surface for debugging
            raise FrameError(f"unexpected reply cmd 0x{f.cmd:02X}")
        raise TimeoutError(f"no reply for cmd 0x{cmd:02X}")

    # -- high-level wrappers (subset; add as needed)

    def query_version(self, board_type: int = 0) -> bytes:
        # 0x01: 1B board type (0=motor 1=platform) + 1B reserved
        return self.request(0x01, bytes([board_type, 0x00]), timeout=1.0).data

    def query_status(self) -> int:
        # 0x05: returns 1 byte status code
        f = self.request(0x05, b"", timeout=0.5)
        return f.data[0]

    def can_dispense(self, lane: int = 0xFF, push_if_door_open: int = 0) -> dict:
        # 0x04: lane (0xFF = skip lane check), push flag
        f = self.request(0x04, bytes([lane, push_if_door_open]), timeout=1.0)
        d = f.data
        return {
            "device_status": d[0],
            "has_goods": d[1],
            "can_dispense": d[2] == 0,
            "reason": d[3],
        }

    def query_motor(self, lane: int) -> bool:
        # 0x29: 2B lane number -> 2B lane echo + 1B status (0 has motor)
        f = self.request(0x29, struct.pack(">H", lane), timeout=1.0)
        return f.data[2] == 0

    def reset_to_origin(self) -> bool:
        # 0x2A: returns 1B (0 success)
        f = self.request(0x2A, b"", timeout=30.0)
        return f.data[0] == 0

    def reset_door_and_scan(self, reset_door: int = 1, scan_mode: int = 1,
                            timeout: float = 300.0) -> bytes:
        # 0x24: 1B door_reset + 1B scan_mode (1=scan all, 2=scan layers).
        # Full physical scan of 36 lanes empirically takes ~200s.
        f = self.request(0x24, bytes([reset_door, scan_mode]), timeout=timeout)
        return f.data

    def dispense(self, lane: int, order_id: bytes) -> int:
        """0x28 — issue dispense. Returns 1B status from <0x28 返回码>."""
        if len(order_id) != 8:
            raise ValueError("order_id must be 8 bytes")
        payload = struct.pack(">H", lane) + order_id
        f = self.request(0x28, payload, timeout=60.0)
        # reply: 8B order_id + 1B status
        return f.data[8]

    def query_order(self, order_id: bytes) -> tuple[int, int]:
        # 0x30: returns 1B dispense_ok + 1B picked_up_ok (0 = success)
        f = self.request(0x30, order_id, timeout=1.0)
        return f.data[0], f.data[1]

    def open_pickup_door(self, timeout_s: int = 30, normal: int = 1) -> int:
        # 0x65: 3B reserved + 2B no-pick timeout + 1B normal
        payload = b"\x00\x00\x00" + struct.pack(">H", timeout_s) + bytes([normal])
        f = self.request(0x65, payload, timeout=10.0)
        return f.data[0]

    def query_photo_sensors(self) -> bytes:
        return self.request(0x34, b"", timeout=1.0).data

    def query_ir(self, ir_type: int) -> int:
        # 0=平台竖红外 1=横红外 2=防夹手 3=层定位
        return self.request(0x35, bytes([ir_type]), timeout=1.0).data[0]

    def query_microswitch(self) -> bytes:
        return self.request(0x39, b"", timeout=1.0).data
