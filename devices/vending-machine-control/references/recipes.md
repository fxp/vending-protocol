# 配方手册

> 拿来就能用的代码片段。所有片段假设 `wm800.py` 在 PYTHONPATH 中。

## R1 — 第一次连接验证（30 秒诊断）

```python
from wm800 import WM800Client

c = WM800Client("/dev/tty.usbserial-XXXX", addr=0x00, read_timeout=2.0)

# 1. 心跳
status = c.query_status()
print(f"status = 0x{status:02X}  (0x00=空闲)")

# 2. 版本
v = c.query_version().rstrip(b"\x00").decode("ascii", "replace")
print(f"electronics: {v}")

c.close()
```

**判断**：
- 拿到 `status` 且 `version` 是 ASCII 字符串 → 协议 OK
- 超时 → 检查地址（用 `probe.py` 遍历 0x00-0x03）和波特率
- 拿到帧但解码错误 → 检查 CRC 实现（设备不校验输入但应答用 XMODEM 大端）

## R2 — 安全出货（生产用）

```python
import time, struct
from wm800 import WM800Client, FrameError

def dispense(c, lane, max_wait=90):
    """出货 + 等待全流程上报 + 终态确认。返回 (ok, reason)"""
    order = int(time.time() * 1000).to_bytes(8, "big")
    payload = struct.pack(">H", lane) + order

    # 1. 发出货
    try:
        f = c.request(0x28, payload, timeout=60.0)
    except TimeoutError:
        return False, "timeout"

    if len(f.data) < 9 or f.data[8] != 0:
        return False, f"status=0x{f.data[8]:02X}"  # 见 protocol.md 0x28 返回码

    # 2. 等待 0xE1 上报链：0x01 开门 → 0x03 取货 → 0x04 回原点
    seen = set()
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = c._read_one_frame()  # 内置自动 ACK 0xE1
            if r.cmd == 0xE1 and len(r.data) >= 9:
                action = r.data[8]
                seen.add(action)
                c._ack_report(r)
                if 0x04 in seen:  # 平台回原点 = 整个流程结束
                    break
        except TimeoutError:
            continue

    # 3. 终态查 0x30
    try:
        f = c.request(0x30, order, timeout=2.0)
        dispense_ok = f.data[0] == 0
        picked_ok = f.data[1] == 0
        return (dispense_ok and picked_ok), {
            "actions_seen": sorted(seen),
            "dispense_ok": dispense_ok,
            "picked_ok": picked_ok,
        }
    except (FrameError, TimeoutError) as e:
        return False, f"order query failed: {e}"
```

## R3 — 启动初始化（建立货道映射）

```python
def boot(c):
    """上电后的标准初始化序列。"""
    # 1. 看状态
    s = c.query_status()
    if s != 0x00:
        # 异常状态先复位
        c.reset_to_origin()  # 0x2A，<1s

    # 2. 查全货道步数（不用物理扫描，已存配置）
    f = c.request(0x2B, b"", timeout=2.0)
    layers = f.data[0]
    per_layer = list(f.data[1:1+layers])
    print(f"  {layers} 层，每层 {per_layer}，共 {sum(per_layer)} 货道")

    # 3. （可选）跑全扫描验证电机就位 — 注意耗时 200s+
    # data = c.reset_door_and_scan(reset_door=1, scan_mode=1, timeout=300)
    # 仅在每天首次启动 / 维护时跑

    return {"layers": layers, "per_layer": per_layer}
```

## R4 — 故障恢复

```python
def recover(c):
    """设备无响应时的标准恢复路径。"""
    import time

    # 1. 被动监听 60s — 长操作可能还在跑
    print("  passive listen 60s...")
    deadline = time.time() + 60
    saw_anything = False
    while time.time() < deadline:
        try:
            f = c._read_one_frame()
            saw_anything = True
            print(f"  late frame: cmd=0x{f.cmd:02X}")
        except TimeoutError:
            continue

    # 2. 发 0x2A 复位
    print("  send 0x2A reset...")
    try:
        ok = c.reset_to_origin()
        if ok:
            print("  RECOVERED")
            return True
    except (TimeoutError, Exception) as e:
        print(f"  reset failed: {e}")

    # 3. 仍然无响应 → 物理断电
    print("  ⚠ DEVICE STUCK — physical power cycle required")
    return False
```

## R5 — 长时间监听（被动模式）

适合对付 0x24 这种 200s 才回的指令，或者监控 0xE1/0xE2 主动上报。

```python
def listen(c, duration_s, on_frame=None):
    """被动监听，不主动发指令，自动 ACK 0xE1。"""
    import time
    end = time.time() + duration_s
    seen = []
    while time.time() < end:
        c.ser.timeout = max(0.05, end - time.time())
        try:
            f = c._read_one_frame()
            seen.append(f)
            if f.cmd == 0xE1 and len(f.data) >= 9:
                c._ack_report(f)
            if on_frame:
                on_frame(f)
        except TimeoutError:
            continue
    return seen
```

## R6 — CRC 不校验的偷懒发法

如果你不想引入 wm800.py，最小可用发包：

```python
import struct, serial

def send_raw(port, addr, cmd, data=b""):
    """裸发，CRC 全 0（设备不校验）。"""
    frame = (
        bytes([0xEE, 0x01]) +
        struct.pack(">I", addr) +
        bytes([cmd]) +
        struct.pack(">H", len(data)) +
        data +
        b"\x00\x00"  # CRC 偷懒
    )
    ser = serial.Serial(port, 9600, timeout=2.0)
    ser.write(frame)
    return ser.read(64)
```

⚠️ 仅用于一次性 debug。生产代码用 `WM800Client`（处理 buffer / 重组帧 / 自动 ACK）。

## R7 — 视觉验证（macOS）

```python
import subprocess, time

def visual_capture(out_dir, duration_s, interval_ms=500):
    """用 Photo Booth + screencapture 抓帧。"""
    # 确保 Photo Booth 在前台且未休眠
    subprocess.run(["osascript", "-e", 'tell application "Photo Booth" to activate'])
    time.sleep(2)

    import os
    os.makedirs(out_dir, exist_ok=True)

    end = time.time() + duration_s
    i = 0
    while time.time() < end:
        path = f"{out_dir}/t{i:04d}_{int((time.time()*1000) % 1000000):06d}.jpg"
        subprocess.run(["screencapture", "-x", "-t", "jpg", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        i += 1
        time.sleep(interval_ms / 1000)
    return i
```

⚠️ Photo Booth 长时间不交互会自动暂停（"Move mouse to resume"）。用一段时间需要手动点击窗口让它继续。

## R8 — 监听 0xE1/0xE2 主动上报（无指令模式）

```python
import time
from wm800 import build_frame, parse_frame, DEV_START

def passive_monitor(ser, addr, duration_s):
    """纯监听模式，自动 ACK 所有 0xE1。"""
    buf = b""
    end = time.time() + duration_s
    while time.time() < end:
        ser.timeout = max(0.05, end - time.time())
        chunk = ser.read(256)
        if not chunk:
            continue
        buf += chunk
        # 边读边 parse
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
                yield f
                if f.cmd == 0xE1 and len(f.data) >= 9:
                    ser.write(build_frame(addr, 0xE1, bytes([f.data[8]])))
            except Exception:
                pass
            i += total
        buf = buf[i:]
```

## 常见错误诊断

| 现象 | 可能原因 |
|---|---|
| `0x05` 一直超时 | 地址不对（用 probe.py）/ 波特率不对（必 9600） / 线没接 |
| `0x28` 返回 `0x08` | 货道空 / 抓取失败。检查实际货道有无商品 |
| `0xE1` 收不到 | 库没启用 `on_report` 或 `request()` 内的隐式监听被绕过 |
| `0x24` 启动后所有指令无响应 | 还在扫描（最多 202s）。被动监听等它回 |
| 偶尔丢帧 | RS232 抗干扰差，远距离用 RS485 或加屏蔽线 |
| 0x29 lane 100 报"无电机" 但 0x28 lane 100 能出货 | **正常**——0x29 echo 字段语义不明，只信第 3 字节状态。但要小心：CSV 里 lane→0x29 的对应也不准 |
