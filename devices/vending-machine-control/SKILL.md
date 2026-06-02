---
name: vending-machine-control
description: 控制 某星XX800 / WM800 类自动售货机下位机（RS232 串口 + 二进制协议）。涵盖出货、复位、扫描、传感器查询、订单状态。包含一份内嵌协议库 + 探测脚本 + 已验证的 firmware 黑名单。TRIGGER when 用户提到 WM800 / 某星XX800 / 售货机下位机 / vending machine controller / 出货指令 / 串口协议 + 售货机 / 0x28 出货 / CSV 协议文档对不上。SKIP 通用售货机硬件问题、其它品牌（如 Crane、AEMP、MDB 协议）、上位机 UI 开发。
---

# 某星XX800 / WM800 售货机下位机控制

## 角色定位

你是一名熟悉 某星XX800 / WM800 售货机协议的工程师助手。用户给你一台机器和串口连接，你的任务是：**让机器按指令出货、查状态、扫货道**，避开已知 firmware 坑。

> **重要**：本 skill 基于一台**真实 firmware** 的实测数据。文档（`巨星 WM800 控制协议.csv`）有 10+ 处错漏，**不要**完全相信文档。永远以本 skill 的 `references/known-issues.md` 为准。

## 快速判断流程

```
用户说话 → 判断需求类型
    ├─ 我有一台机器，怎么连？        → §1 连接 + 验证
    ├─ 让它出货                      → §2 出货流程
    ├─ 扫描所有货道                  → §3 货道扫描
    ├─ 看传感器/状态                 → §4 状态查询
    ├─ 它没反应了                    → §5 故障恢复
    ├─ 文档说 X 但实际 Y             → 查 references/known-issues.md
    └─ 不知道某条指令能不能用        → 查 references/protocol.md 命令矩阵
```

## §1 连接 + 验证

### 硬件需求
- USB → RS232/RS485 转串口适配器（**CP2102 / CH340 / FT232** 都行）
- 9600 8N1
- 一根直连线插到下位机的串口接口

### 软件准备
```bash
pip3 install --user --break-system-packages pyserial
```

⚠️ **Mac 多 python 坑**：macOS 上常有多个 python（`/usr/bin/python3` 自带 / homebrew `/opt/homebrew/bin/python3`）。`pip3 install` 装到哪个，后面运行就必须用哪个。验证：
```bash
which python3 pip3                            # 看这俩是同一套吗
python3 -c "import serial; print(serial.__file__)"   # 能 import 就对了
```
不对的话用绝对路径，如 `/opt/homebrew/bin/python3 probe.py ...`。

### 找端口
```bash
ls /dev/tty.usbserial-* /dev/cu.usbserial-*   # macOS
ls /dev/ttyUSB* /dev/ttyACM*                  # Linux
# Windows: 设备管理器看 COM 号
```

### 第一次握手

**永远先发 `0x05` 查状态**——这是最便宜的"设备活着吗"测试。用 `assets/probe.py`（**自带端口自动检测**）：

```bash
cd ~/.claude/skills/vending-machine-control/assets
python3 probe.py                              # 自动找端口
python3 probe.py --port /dev/tty.usbserial-XXXX  # 或指定
python3 probe.py --addrs 0x0,0x5,0xA          # 或试别的地址
```

probe 默认遍历地址 0x00 / 0x01 / 0x02 / 0x03。

### 关键参数（永远从这里开始）

| 参数 | 值 | 备注 |
|---|---|---|
| 波特率 | 9600 | 8N1 |
| 起始字节 | `0xEE` 上→下 / `0xFF` 下→上 | |
| 版本号 | `0x01` | 永远填这个 |
| 设备地址 | **看拨码开关**（常见 `0x00000000`） | **不要**信文档默认 `0x01` |
| CRC | **设备不校验** | 填什么都接受，建议填 0x0000 或 XMODEM 大端 |

完整帧格式：

```
[start:1B] [ver:1B=0x01] [addr:4B BE] [cmd:1B] [len:2B BE] [data:N B] [crc:2B]
```

`len` 仅指 data 长度，不含 CRC。

## §2 出货流程

### 标准出货

```python
import time, struct
from wm800 import WM800Client

c = WM800Client("/dev/tty.usbserial-XXXX", addr=0x00)

LANE = 100  # 货道编号是十进制平面编号，不是层+列。具体编号问商家
order_id = int(time.time() * 1000).to_bytes(8, "big")

status = c.dispense(LANE, order_id)
if status == 0:
    print("出货指令接受成功")
```

### **关键注意事项**

1. **不要用 `0x04` 预检** — 它永远报"无电机"，但 `0x28` 实际能成功。直接发 `0x28`。
2. **超时给到 60s** — 平台从原点移动到货道+抓货+下降需要时间。
3. **必须监听 `0xE1` 主动上报** — 完整事件链：
   ```
   0x28 应答 status=0x00     ← 指令接受
   0xE1 action=0x01          ← 取货门打开
   0xE1 action=0x03          ← 货物已被取走
   0xE1 action=0x04          ← 平台回原点
   ```
4. **必须 ACK `0xE1`** — 否则下位机重发 3 次。`WM800Client.on_report` 默认自动 ACK。
5. **终态查 `0x30 订单状态`** — 8 字节订单号回传，得到 `(出货 ok, 取货 ok)`。

### 出货失败码

`0x28` 应答的 status 字节非 0 表示失败。常见值：
- `0x00` 成功
- `0x08` 货道空 / 无法抓取（实测过）
- 其它值见 `references/protocol.md` "0x28 返回码表"

## §3 货道扫描

### 完整扫描

```python
data = c.reset_door_and_scan(reset_door=1, scan_mode=1, timeout=300)
```

### **关键注意事项**

1. **耗时 ~200 秒**（实测 202s）—— **timeout 必须 ≥ 300s**。
2. **应答字节 1 不是状态码**（CSV 写错），实际是层数。结构：
   ```
   [reserved:1B=0] [layers:1B] [motors_per_layer:N B]
   ```
3. **期间不要 flush input buffer** — 否则迟到的应答会被冲掉，再也收不到。
4. 失败后用 `0x2A` 复位，**不要用 `0x23`**（firmware 不实现）。

### 0x2B 全货道步数（更便宜的查询）

不用物理扫描，直接读已存储的步数表：

```python
f = c.request(0x2B, b"")
data = f.data
layers = data[0]               # 7
per_layer = list(data[1:1+layers])  # [3,5,5,5,6,6,6]
# 后面是 4 字节 × 总货道数 的 X 步数
```

**注意**：CSV 写有 "Y 步数" 块（180 字节），实际只有 152 字节，没有 Y 步数块。

## §4 状态查询

### 推荐组合

| 用途 | 指令 | 说明 |
|---|---|---|
| 心跳 / 是否空闲 | `0x05` | 100ms 内回，最便宜 |
| 微动开关（限位/伺服） | `0x39` | 6 个：上限/下限/防夹/大门/Y伺服/X伺服 |
| 光电传感器 | `0x34` | 5 个：右/左/上/中/下 |
| 红外（仅 type 0/2/3） | `0x35` | type=1 横红外 firmware 不支持 |
| 版本号 | `0x01` | 30B ASCII |

### 0x05 状态码（也用作 0x04）

- `0x00` 空闲
- `0x02` 无电机
- `0x12` 右/中/下限位未触碰
- `0x19` 左限位被触碰
- `0x1A` 上限位被触碰
- `0x1E` 取货门没关紧
- `0x22` 竖红外被挡或损坏

## §5 故障恢复

### 设备无响应

按这个顺序：
1. **被动监听 60s** — 长操作可能还在跑
2. **发 `0x2A` 复位** — CSV 说"强制状态机改为空闲"，实测有效
3. **物理断电重启** — 最后手段

### 永远不要做的事

- ❌ 用 `0x23` 重启伺服 — firmware **不实现**，只会让你等 22 秒一无所获
- ❌ 用 `0x3D` 直驱平台电机 — firmware **不实现**，无论 payload 怎么填
- ❌ 用 `0x2C` 改货道步数 — firmware **不实现**
- ❌ 用 `0xBC` 设置每层偏移 — firmware **不实现**
- ❌ 用 `0x04` 做出货预检 — 永远报无电机，会让你以为机器坏了

## §6 视觉验证（可选但强烈推荐）

协议层"成功"≠ 物理"成功"。如果有 macOS 设备：

1. 把 Photo Booth 对准机器，保持窗口活跃
2. Python 脚本同步 `screencapture -x -t jpg /tmp/frame.jpg` 抓帧
3. 平台抓手在视野里，能直观看到下降-抓货-返回

`scripts/visual_capture.py` 提供模板。

## §7 Firmware 黑名单（一定背下来）

| Cmd | 状态 | 处理 |
|---|---|---|
| `0x3D` 平台电机直驱 | 不实现 | 不用 |
| `0x23` 伺服重启 | 不实现 | 用 `0x2A` 替代 |
| `0xBC` 每层偏移 | 不实现 | 不用 |
| `0x2C` 改货道步数 | 不实现 | 不用 |
| `0x35 type=1` 横红外 | 不实现 | 仅查 type=0/2/3 |
| `0x04` 出货预检 | **有响应但语义错** | 不要信，直接 `0x28` |
| `0x29` 应答前 2 字节 | **有响应但 echo 不准** | 只信第 3 字节状态 |

## 参考文件

| 文件 | 内容 |
|---|---|
| `references/protocol.md` | 全 22 条指令详细帧格式 + 返回码表 |
| `references/known-issues.md` | 10 处文档/firmware 错漏的全数据 |
| `references/recipes.md` | 出货 / 扫描 / 监听 / 调试常见配方 |
| `assets/wm800.py` | 协议库（CRC + 帧编解码 + `WM800Client`） |
| `assets/probe.py` | 只读探测，自动找设备地址 |
| `assets/test_wm800.py` | 交互式菜单 |
| `scripts/visual_capture.py` | Photo Booth 视觉验证模板 |

## 工作守则

1. **永远先 probe 再发指令**。波特率/地址/CRC 都对了再做下一步。
2. **不动电机的指令优先**。先 `0x05` `0x39` `0x34` 摸清状态再考虑动作。
3. **看见报错 = 看协议 + 看物理**。两者必须一致才算"成功"。
4. **出货必须有订单号**。8 字节，建议用毫秒时间戳。一个订单号绑一次出货，便于事后查 `0x30`。
5. **firmware 黑名单宁可重背一遍**。生产代码出问题 99% 是依赖了不实现的指令。
6. **报告时引用具体字节**。"机器没反应"不行，要说"发 `EE 01 ... 28 ... [crc]`，60s 无应答，期间 `0x05` 也无应答"。

---

*本 skill 来自 NEWDEV № 01 现场报告。原始测试日 2026-04-30，问野（Roam）+ 朋友共同完成。*
