"""
WM800 async gateway.

Wraps the synchronous WM800Client for use inside the UCP adapter.
All serial I/O is serialized through a single asyncio.Lock — concurrent
FastAPI handlers queue up and run one at a time.

Call setup() once at app startup, teardown() on shutdown.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "../../../devices/vending-machine-control/assets"),
)
from wm800 import WM800Client, Frame  # noqa: E402

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_client: Optional[WM800Client] = None
_lock: Optional[asyncio.Lock] = None

# order_id_hex (16 chars) → list of event dicts, in arrival order
_order_events: dict[str, list[dict]] = {}

_ACTION_NAMES: dict[int, str] = {
    0x01: "door_open",
    0x02: "door_closed",
    0x03: "goods_taken",
    0x04: "platform_home",
}
# Either of these signals the end of a dispense cycle.
_TERMINAL = {0x03, 0x04}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def setup(port: str, addr: int = 0x00) -> None:
    global _client, _lock
    _lock = asyncio.Lock()
    _client = WM800Client(port, addr=addr, on_report=_on_report)


def teardown() -> None:
    if _client:
        _client.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _on_report(frame: Frame) -> None:
    """Called by WM800Client whenever a 0xE1/0xE2 unsolicited report arrives."""
    if frame.cmd == 0xE1 and len(frame.data) >= 9:
        oid = frame.data[:8].hex()
        action = frame.data[8]
        _order_events.setdefault(oid, []).append({
            "ts": time.time(),
            "action": action,
            "name": _ACTION_NAMES.get(action, f"0x{action:02X}"),
        })


async def _serial(fn, *args):
    """Run a blocking WM800 call in a thread executor, serialized by lock."""
    async with _lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)


def _dispense_and_listen(lane: int, oid_hex: str,
                          listen_timeout: float = 120.0) -> int:
    """
    Blocking. Sends 0x28 then drains 0xE1 events until terminal state or timeout.

    0xE1 events that arrive *during* dispense() are captured via on_report.
    This function then keeps reading for events that arrive *after* the
    0x28 reply, up to listen_timeout seconds.

    Returns the 0x28 status byte (0x00 = accepted).
    """
    oid_bytes = bytes.fromhex(oid_hex)
    _order_events[oid_hex] = [{"ts": time.time(), "action": 0xFF, "name": "started"}]

    status = _client.dispense(lane, oid_bytes)
    _order_events[oid_hex].append({
        "ts": time.time(),
        "action": 0x00,
        "name": "accepted" if status == 0 else f"rejected_0x{status:02X}",
        "status_code": status,
    })

    if status != 0:
        return status

    # Keep reading until terminal 0xE1 event or timeout.
    deadline = time.monotonic() + listen_timeout
    while time.monotonic() < deadline:
        _client.ser.timeout = min(2.0, deadline - time.monotonic())
        try:
            f = _client._read_one_frame()
        except Exception:
            break
        if f.is_report:
            _client._ack_report(f)
            _on_report(f)
            if any(e["action"] in _TERMINAL
                   for e in _order_events.get(oid_hex, [])):
                break

    return status


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def health() -> dict:
    code = await _serial(_client.query_status)
    return {"status_code": f"0x{code:02X}", "idle": code == 0x00}


async def get_lanes() -> list[dict]:
    """Return lane list from the stored step table (0x2B, ~instant)."""
    raw = await _serial(_client.request, 0x2B, b"")
    data = raw.data
    n_layers = data[0]
    per_layer = list(data[1: 1 + n_layers])
    result, base = [], 100
    for layer_idx, motor_count in enumerate(per_layer):
        for col in range(motor_count):
            result.append({"lane": base + col, "layer": layer_idx + 1, "col": col})
        base += 100
    return result


async def start_dispense(lane: int, order_id_hex: str) -> None:
    """Fire-and-forget. Serial lock is held for up to ~120 s in the background."""
    asyncio.create_task(_serial(_dispense_and_listen, lane, order_id_hex))


def get_order_events(order_id_hex: str) -> list[dict]:
    return _order_events.get(order_id_hex, [])


def order_is_done(order_id_hex: str) -> bool:
    return any(e["action"] in _TERMINAL
               for e in _order_events.get(order_id_hex, []))
