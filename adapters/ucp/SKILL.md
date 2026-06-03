---
name: ucp-adapter
description: >
  WM800 串口售货机的 UCP (Universal Commerce Protocol) 适配器。
  把 WM800 下位机暴露为 UCP 兼容的 REST 服务，让 AI Agent 通过标准化的
  Cart / Checkout / Order 流程控制出货。
  TRIGGER when 用户提到 UCP / ucp.dev / Google Commerce Protocol /
  "让 WM800 兼容 UCP" / "UCP adapter" / "universal commerce".
  SKIP 通用 WM800 串口调试（用 vending-machine-control skill）、
  微米云台 API（用 weimi-vending-api skill）。
---

# WM800 UCP Adapter

## 架构

```
UCP Client (AI Agent)
    │  OAuth 2.0 + REST
    ▼
server/app.py  (port 8080)
    │  asyncio.Lock 串口序列化
    ▼
server/gateway.py
    │  RS232 9600 8N1
    ▼
WM800 下位机
```

## 安装

```bash
cd adapters/ucp
pip install -r requirements.txt
cp catalog.example.json catalog.json
# 编辑 catalog.json，填入你的货道 → 商品映射
```

## 启动

```bash
export WM800_PORT=/dev/tty.usbserial-XXXX   # 必填
export WM800_ADDR=0x00                       # 默认 0x00
export UCP_CLIENT_ID=my-agent
export UCP_CLIENT_SECRET=my-secret
export UCP_JWT_SECRET=some-long-random-string

cd server
python app.py
```

## 当前端点（实现路径）

> ⚠️ 下列路径是当前实现路径，**尚未对齐 UCP 规范**（规范要求 `/checkout-sessions` 等，详见"已知限制"）。

### Step 1 — 获取 token

```bash
curl -X POST http://localhost:8080/oauth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=my-agent" \
  -d "client_secret=my-secret"
TOKEN=$(上面命令输出的 access_token)
```

### Step 2 — 浏览商品 (Cart)

```bash
curl -X POST http://localhost:8080/v1/cart \
  -H "Authorization: Bearer $TOKEN"
# → {"ucp":{...}, "continue_url":"...", "status":"incomplete", "line_items":[...]}
```

### Step 3 — 创建结账 (Checkout)

```bash
curl -X POST http://localhost:8080/v1/checkout \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"line_items":[{"id":"lane_100","quantity":1}],
       "payment":{"handler_id":"prepaid","instrument":{"token":"paid-abc"}}}'
# → {"status":"ready_for_complete", "id":"chk_abc..."}
CHK_ID=chk_abc...
```

### Step 4 — 触发出货 (Complete)

```bash
curl -X POST http://localhost:8080/v1/checkout/$CHK_ID/complete \
  -H "Authorization: Bearer $TOKEN"
# → {"status":"complete", "order_id":"3f7a1b2c..."}
ORDER_ID=3f7a1b2c...
```

### Step 5 — 追踪出货 (Order)

```bash
# 轮询
curl http://localhost:8080/v1/order/$ORDER_ID -H "Authorization: Bearer $TOKEN"

# SSE 实时流（先拿 cookie）
curl -X POST http://localhost:8080/internal/sse-token \
  -H "Authorization: Bearer $TOKEN" -c /tmp/ucp.txt
curl -N http://localhost:8080/v1/order/$ORDER_ID/stream -b /tmp/ucp.txt
```

## 快速判断流程

```
问题 → 看哪里
  机器连不上 / 串口无响应       → vending-machine-control skill §1
  catalog.json 没加载            → 检查 CATALOG_PATH 环境变量
  token 401                      → 检查 UCP_CLIENT_ID/SECRET
  checkout → complete 返回 503   → 看 gateway.py，0x28 返回非 0x00
  order.status 一直 incomplete   → 等 0xE1 goods_taken；超时看 known-issues.md
  UCP client 报 profile 错误     → GET /.well-known/ucp 手动验证
```

## UCP 合规状态

概念流程兼容，接口层有以下已知差距（详见 `references/mapping.md`）：

| 优先级 | 问题 | 说明 |
|---|---|---|
| P0 | 端点路径不符合规范 | 应为 `/checkout-sessions`，现为 `/v1/checkout` |
| P0 | 缺 `currency` / `totals` 字段 | Checkout/Order 响应必须含这两个字段 |
| P0 | status 值名称错误 | 应为 `completed`，现为 `complete` |
| P0 | `messages[].content` 字段名 | 应为 `content`，现为 `message` |
| P1 | `UCP-Agent` / `Idempotency-Key` 头未校验 | 规范要求服务端验证 |
| P1 | HTTP Message Signatures 未实现 | webhook 签名是强制要求，`signing_keys: []` |
| P2 | 缺 `GET/PUT /checkout-sessions` 和 `/cancel` | 规范要求完整 CRUD |
| P2 | Profile 结构与规范略有偏差 | `services` 格式、payment handler 缺 `spec`/`schema` |

## 其他已知限制

- `quantity_available` 恒为 1（精确库存需 0x24 全扫描，~200s）
- 一次结账只支持一个货道（`line_items[0]`）
- Webhook 推送未实现，客户端需 poll 或用 SSE
- Checkout 状态仅保存在内存，重启丢失

## 参考文件

| 文件 | 内容 |
|------|------|
| `references/mapping.md` | UCP ↔ WM800 字段映射全表 |
| `catalog.example.json` | 货道商品配置模板 |
| `server/gateway.py` | WM800 异步封装 + 0xE1 事件收集 |
| `server/app.py` | UCP REST 端点实现 |
| `server/auth.py` | OAuth 2.0 JWT 工具 |
