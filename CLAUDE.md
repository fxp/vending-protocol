# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Field-tested protocols and Claude Code skills for vending machines / unmanned retail. Every claim has been verified on real hardware. Do not add anything that hasn't been physically tested — mark it `⚠️ 未实测` if you must speculate.

## Repo structure

```
devices/          One directory per physical device / cloud platform
  vending-machine-control/   WM800 lower-board direct serial (RS232)
  weimi-vending-api/         Weimi cloud (VMS-WM900XY / WM500 etc.)
adapters/
  ucp/            UCP (Universal Commerce Protocol) adapter
    server/       Production adapter wrapping the WM800 gateway
    mock/         Standalone UCP mock server — no hardware needed
    references/   UCP ↔ WM800 field mapping
venues/           Venue-level protocols (placeholder, not yet tested)
install.sh        Symlinks all SKILL.md directories into ~/.claude/skills/
```

Each `devices/<name>/` and `adapters/<name>/` directory **is** a Claude Code skill. The directory name = the `name:` in its `SKILL.md` frontmatter.

## Install skills

```bash
./install.sh          # symlinks every skill into ~/.claude/skills/
```

## UCP mock server (no hardware required)

```bash
cd adapters/ucp/mock
pip install fastapi uvicorn pyjwt python-multipart httpx anyio pytest pytest-anyio
python server.py      # → http://localhost:8080
```

**Run tests:**
```bash
cd adapters/ucp/mock
pytest test_server.py -v                        # all 71 tests
pytest test_server.py -v -k "TestSimulation"   # one class
pytest test_server.py -v -k "test_expired"     # one test
```

Tests are split into sync (use `TestClient`) and async (use `@pytest.mark.anyio` + `httpx.AsyncClient`). Simulation tests must be async — `asyncio.create_task` does not advance under `time.sleep`.

## UCP production adapter (requires WM800 hardware)

```bash
cd adapters/ucp/server
pip install -r ../requirements.txt
WM800_PORT=/dev/tty.usbserial-XXXX \
UCP_CLIENT_SECRET=secret \
python app.py        # → http://localhost:8080
```

Copy and fill `adapters/ucp/catalog.example.json` → `catalog.json` before starting.

## WM800 serial quick-probe

```bash
cd devices/vending-machine-control/assets
pip install pyserial
python probe.py                              # auto-detects port + address
python probe.py --port /dev/tty.usbserial-XXXX --addrs 0x0,0x1
```

## Architecture

### Two-layer design (WM800 path)

```
UCP client (AI agent / browser)
        │  OAuth 2.0 + REST
        ▼
adapters/ucp/server/app.py          (UCP REST endpoints)
        │  asyncio.Lock-serialized calls
        ▼
adapters/ucp/server/gateway.py      (async wrapper around WM800Client)
        │  RS232 9600 8N1
        ▼
devices/vending-machine-control/assets/wm800.py   (sync serial client)
        │  binary EE-frame protocol
        ▼
WM800 hardware
```

**Key constraint**: `WM800Client` is synchronous and the serial port is single-threaded. `gateway.py` wraps every call in `asyncio.Lock` + `run_in_executor`. One dispense cycle holds the lock for up to ~120 s. `start_dispense()` is fire-and-forget via `asyncio.create_task`.

### UCP mock server (mock/server.py)

Single-file standalone FastAPI app. No hardware. Identical REST surface to the production adapter. Uses `asyncio.sleep` to simulate dispense timing; post-event checkout status is updated inside `_run_scenario()` which runs as a background task.

**Scenario dispatch by lane number** (catalog hard-coded):

| Lane | Behaviour |
|------|-----------|
| 100–199 | Normal: accepted 0.3 s, door_open 3 s, goods_taken 8 s |
| 200–299 | Slow: door_open 10 s, goods_taken 25 s |
| 900 | Empty: rejected_0x08 at 0.5 s |
| 901 | Offline: `complete` endpoint returns `device_unavailable` error immediately |

### UCP protocol layer

The UCP flow maps to WM800 as follows:

| UCP step | WM800 operation |
|---|---|
| `POST /cart-sessions` | `0x2B` step-table read + `catalog.json` lookup |
| `POST /checkout-sessions` | In-memory only; no serial I/O |
| `POST /checkout-sessions/{id}/complete` | `0x28` dispense command |
| Poll `GET /orders/{id}` or SSE | `0xE1` unsolicited reports from device |

Every response carries `ucp` envelope + `continue_url`. SSE auth uses an HttpOnly `sse_tok` cookie issued by `POST /internal/sse-token` (because `EventSource` cannot send `Authorization` headers).

## UCP compliance status

Current implementation is **conceptually aligned but not fully spec-compliant**. Known gaps (from `adapters/ucp/references/mapping.md`):

- Endpoint paths should be `/checkout-sessions` (not `/v1/checkout`)
- Checkout status `complete` should be `completed` per spec
- Missing required response fields: `currency` (ISO 4217) and `totals` array
- `messages[].content` spec field vs our `messages[].message`
- HTTP Message Signatures (RFC 9421) — `signing_keys: []`, not implemented
- Missing `UCP-Agent` / `Idempotency-Key` header validation
- No `GET /checkout-sessions/{id}`, `PUT`, or `/cancel` endpoints

## WM800 firmware gotchas (hardware-tested, never remove)

**Firmware blacklist — never send:**

| Cmd | Why |
|---|---|
| `0x04` 出货预检 | Always reports "no motor" even for working lanes. Skip it; send `0x28` directly. |
| `0x3D` 平台电机直驱 | Not implemented in firmware. Silent, no response. |
| `0x23` 伺服重启 | Not implemented. Use `0x2A` reset instead. |
| `0xBC` 每层偏移 | Not implemented. |
| `0x2C` 改货道步数 | Not implemented. |
| `0x35 type=1` 横红外 | Not implemented; reply cmd shifts to `0x36`. |

**Other field notes:**
- CRC: device **does not validate** incoming CRC (but sends XMODEM-16 big-endian in replies). Safe to fill `0x0000`.
- Device address: **read from DIP switch**, default is `0x00000000` not `0x00000001` as in docs.
- `0x24` full scan takes ~202 s; set timeout ≥ 300 s and never flush the buffer mid-scan.
- `0x29` echo bytes are unreliable; only trust byte[2] for motor presence.
- `0x2B` response is 152 bytes (no Y-steps block despite CSV saying 180 bytes).

## Adding a new device skill

1. `mkdir devices/<name>/` with `SKILL.md`, `references/`, `scripts/`, `assets/`
2. `SKILL.md` frontmatter: `name:` must equal the directory name
3. Mark anything unverified with `⚠️ 未实测`
4. Add a row to README.md "已覆盖" table
5. Re-run `./install.sh`
