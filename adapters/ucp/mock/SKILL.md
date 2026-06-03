---
name: ucp-mock
description: >
  本地运行的 UCP (Universal Commerce Protocol) Mock Server，模拟完整自动售货机购买流程。
  无需硬件，提供 OAuth 2.0、Cart / Checkout / Order 全套端点、SSE 实时事件流和可配置故障场景。
  供其他 Agent 做 UCP 协议集成测试、开发调试、端对端验证使用。
  TRIGGER when 用户/Agent 提到 "UCP mock"、"UCP 测试服务器"、"模拟 UCP"、"ucp-mock"、
  "本地 UCP server"、"测试 UCP 流程" 或需要一个不依赖真实硬件的 UCP 端点。
  SKIP 真实 WM800 串口控制（用 vending-machine-control）、微米云台（用 weimi-vending-api）、
  生产 UCP 适配器（用 ucp-adapter）。
---

# UCP Mock Server

## 角色定位

你是一个能启动并驱动本地 UCP Mock Server 的助手。目标是：**让调用方 Agent 在没有真实硬件的情况下，走完整 UCP 购买流程，拿到可验证的端点响应**。

每个步骤都要拿到 HTTP 响应并确认状态码和关键字段，不要假设成功。

---

## 快速决策树

```
用户说 → 看哪里
  启动服务器            → §1 启动
  拿 Token              → §2 认证
  看有什么商品          → §3 Cart
  下单 / 触发出货       → §4 Checkout
  看出货状态 / 事件     → §5 Order
  测试失败场景          → §6 场景表
  页面没有更新          → §7 故障排查
  清空所有状态重新测试  → §8 Admin
```

---

## §1 启动服务器

```bash
cd adapters/ucp/mock
pip install fastapi uvicorn pyjwt python-multipart httpx anyio pytest pytest-anyio -q
python server.py
# 输出: WM800 UCP Mock Server → http://localhost:8080
```

**验证已就绪**（收到 200 + ucp_version 字段才算活着）：

```bash
curl -s http://localhost:8080/.well-known/ucp | python3 -m json.tool
```

环境变量（可选覆盖）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `UCP_CLIENT_ID` | `demo` | OAuth client_id |
| `UCP_CLIENT_SECRET` | `demo` | OAuth client_secret |
| `BASE_URL` | `http://localhost:8080` | 写入 profile 的 base URL |
| `CONTINUE_URL` | `{BASE_URL}/fallback` | 错误响应里的 fallback URL |

---

## §2 认证（OAuth 2.0 client_credentials）

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/oauth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=demo" \
  -d "client_secret=demo" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "token: ${TOKEN:0:40}…"
```

之后所有请求都带：`-H "Authorization: Bearer $TOKEN"`

Token 有效期 3600 秒。超时重跑上面命令。

---

## §3 Cart — 浏览商品

```bash
curl -s -X POST http://localhost:8080/v1/cart \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**响应结构**：

```json
{
  "ucp": {"version": "2026-04-08", "capabilities": {...}},
  "continue_url": "http://localhost:8080/fallback",
  "status": "incomplete",
  "line_items": [
    {
      "id": "lane_100",
      "name": "Mineral Water 550ml",
      "price": {"amount": 200, "currency": "CNY"},
      "quantity_available": 1
    }
  ]
}
```

**关键字段**：
- `line_items[].id` — 下单时用这个作 `aisleCode`，格式 `lane_<数字>`
- `quantity_available: 0` — 这个货道没货（对应 lane 900 空货道场景）
- `continue_url` — 每条响应都有；API 流程失败时 agent 可重定向用户至此

---

## §4 Checkout — 创建并完成结账

### Step 1：创建 checkout

```bash
CHK=$(curl -s -X POST http://localhost:8080/v1/checkout \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "line_items": [{"id": "lane_101", "quantity": 1}],
    "buyer": {"email": "agent@example.com"},
    "payment": {
      "handler_id": "prepaid",
      "instrument": {"token": "paid-externally-ref-001"}
    }
  }')
echo $CHK | python3 -m json.tool
CHK_ID=$(echo $CHK | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

**必须字段**：
- `line_items[].id` — 从 Cart 拿到的 `lane_xxx`
- `payment.handler_id` — 固定填 `"prepaid"`
- `payment.instrument.token` — 非空字符串即可（mock 不验证支付真实性）

**payment 为空或 token 为空** → `status: "incomplete"`（不能进入 complete）  
**payment 正确** → `status: "ready_for_complete"`

### Step 2：触发出货（complete）

```bash
CMP=$(curl -s -X POST "http://localhost:8080/v1/checkout/$CHK_ID/complete" \
  -H "Authorization: Bearer $TOKEN")
echo $CMP | python3 -m json.tool
ORDER_ID=$(echo $CMP | python3 -c "import sys,json; print(json.load(sys.stdin)['order_id'])")
echo "order_id: $ORDER_ID"
```

**成功响应**：`status: "complete"` + `order_id: "16位十六进制"`
> ⚠️ UCP 规范要求此处状态值为 `"completed"`，当前实现用 `"complete"`（P0 合规差距）

**失败响应**（如 lane 901 离线）：`status: "error"` + `messages[0].code` + `continue_url`

---

## §5 Order — 追踪出货状态

### 轮询（推荐 Agent 用）

```bash
curl -s "http://localhost:8080/v1/order/$ORDER_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**响应结构**：

```json
{
  "ucp": {...},
  "continue_url": "...",
  "id": "edf0f90c659f4873",
  "checkout_id": "chk_abc123",
  "status": "incomplete | complete | error",
  "fulfillment": {
    "method": "pickup",
    "events": [
      {"type": "accepted",      "ts": 1780370131.0},
      {"type": "door_open",     "ts": 1780370134.0},
      {"type": "goods_taken",   "ts": 1780370139.0},
      {"type": "platform_home", "ts": 1780370143.0}
    ]
  }
}
```

**终态判断**：`status == "complete"`（或按规范应为 `"completed"`）或 `fulfillment.events` 中出现 `goods_taken`。

### SSE 实时流（浏览器 / 长连接）

```bash
# 先拿 SSE cookie，再连流
curl -s -X POST http://localhost:8080/internal/sse-token \
  -H "Authorization: Bearer $TOKEN" -c /tmp/ucp-cookies.txt > /dev/null

curl -N "http://localhost:8080/v1/order/$ORDER_ID/stream" \
  -b /tmp/ucp-cookies.txt
# data: {"ts": 1780370131.0, "action": 0, "name": "accepted"}
# data: {"ts": 1780370134.0, "action": 1, "name": "door_open"}
# data: {"ts": 1780370139.0, "action": 3, "name": "goods_taken"}
# data: {"event": "done"}
```

**action 码**：`0=accepted` `1=door_open` `2=door_closed` `3=goods_taken(终态)` `4=platform_home(终态)` `8=rejected(终态)`

---

## §6 场景表（按 lane 编号控制）

| lane | 商品 | 行为 | 出货结果 | 用途 |
|---|---|---|---|---|
| 100 | Mineral Water | 正常 | accepted→door_open(3s)→goods_taken(8s)→home(12s) | 正常流程测试 |
| 101 | Coca Cola | 正常 | 同上 | 正常流程测试 |
| 102 | Green Tea | 正常 | 同上 | 正常流程测试 |
| 200 | Lay's Chips | 慢 | accepted→door_open(10s)→goods_taken(25s)→home(30s) | 超时/等待测试 |
| 900 | Empty Lane | 空货道 | accepted → rejected_0x08(0.5s)，order.status=error | 出货失败路径测试 |
| 901 | Offline Lane | 离线 | complete 端点立即返回 error+continue_url，不创建 order | 设备离线路径测试 |

---

## §7 故障排查

### 出货后 order.status 一直 incomplete

1. 检查服务器是否还在运行：`curl -s http://localhost:8080/.well-known/ucp`
2. 正常场景 goods_taken 在 8s 后到，lane 200 慢场景在 25s 后到——先等够时间
3. 直接查事件：`curl -s http://localhost:8080/v1/order/$ORDER_ID -H "Authorization: Bearer $TOKEN"`

### SSE 连了但没数据

SSE 认证依赖 `sse_tok` cookie，必须先调 `/internal/sse-token`：
```bash
curl -X POST http://localhost:8080/internal/sse-token \
  -H "Authorization: Bearer $TOKEN" -c /tmp/ucp-cookies.txt
```

### checkout complete 返回 422

Checkout 的 `status` 不是 `ready_for_complete`。原因：创建时 `payment.instrument.token` 是空字符串或整个 `payment` 字段缺失。

### 端口冲突

```bash
lsof -ti:8080 | xargs kill -9
python server.py
```

---

## §8 Admin — 重置状态

```bash
# 清空所有 checkout 和 order 事件，重新开始
curl -X POST http://localhost:8080/admin/reset
```

适合在每轮自动化测试前调用。

---

## §9 完整 Agent 调用流程（可直接 copy-paste）

```bash
BASE=http://localhost:8080

# 1. Token
TOKEN=$(curl -s -X POST $BASE/oauth/token \
  -d "grant_type=client_credentials&client_id=demo&client_secret=demo" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. Cart
LANE_ID=$(curl -s -X POST $BASE/v1/cart -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['line_items'][0]['id'])")

# 3. Checkout
CHK_ID=$(curl -s -X POST $BASE/v1/checkout \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"line_items\":[{\"id\":\"$LANE_ID\",\"quantity\":1}],
       \"payment\":{\"handler_id\":\"prepaid\",\"instrument\":{\"token\":\"pay-$(date +%s)\"}}}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 4. Complete → dispense
ORDER_ID=$(curl -s -X POST "$BASE/v1/checkout/$CHK_ID/complete" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['order_id'])")

# 5. Poll until done
for i in $(seq 1 15); do
  STATUS=$(curl -s "$BASE/v1/order/$ORDER_ID" -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "[$i] order status: $STATUS"
  [ "$STATUS" = "complete" ] || [ "$STATUS" = "error" ] && break
  sleep 2
done
```

---

## UCP 合规状态

与官方 UCP 规范的已知差距（和生产适配器相同）：

| 优先级 | 问题 |
|---|---|
| P0 | 端点路径：`/v1/checkout` 应为 `/checkout-sessions` |
| P0 | `currency` / `totals` 字段缺失 |
| P0 | status `"complete"` 应为 `"completed"` |
| P0 | `messages[].message` 应为 `messages[].content` |
| P1 | HTTP Message Signatures 未实现（`signing_keys: []`） |
| P1 | `UCP-Agent` / `Idempotency-Key` 头未校验 |

详见 `../references/mapping.md`。

## 参考文件

| 文件 | 内容 |
|---|---|
| `server.py` | Mock server 完整实现（单文件） |
| `test_server.py` | **71 个**协议合规测试（sync + async anyio） |
| `conftest.py` | pytest anyio 配置（固定 asyncio 后端） |
| `../references/mapping.md` | UCP ↔ WM800 字段映射 + 合规差距全表 |
| `../SKILL.md` | 生产 UCP Adapter（需真实 WM800 硬件）的 skill |

---

*Mock server 状态仅在进程内存中，重启即清空。测试结束后 `lsof -ti:8080 | xargs kill`。*
