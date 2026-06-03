# UCP ↔ WM800 字段映射

> **规范参考 / canonical UCP reference**：本表的"规范要求路径/字段"对照的是
> [agentic-commerce-skills](https://github.com/fxp/agentic-commerce-skills) 里记录的真实 UCP
> 能力与字段（`dev.ucp.shopping.checkout` 的端点、`status` 枚举、必需头等），那是协议形态的
> 抽象参考；本仓库是其在 WM800 真机上的落地实现与差距分析。

## 流程对比

> 左列为**当前实现路径**，括号内为 UCP 规范要求路径（P0 差距）。

```
当前实现 (Agent 视角)                  WM800 (机器视角)
────────────────────────────────────────────────────
POST /v1/cart                    →    0x2B 全货道步数 (lane list)
  (规范: POST /cart-sessions)         + catalog.json (名称/价格)

POST /v1/checkout                →    内存创建 checkout 对象
  (规范: POST /checkout-sessions)     (不触发串口，仅验证 token 非空)
  payment.handler_id=prepaid

POST /v1/checkout/{id}/complete  →    0x28 出货指令
  (规范路径结构正确，前缀错)            lane + 8-byte order_id
  status: ready_for_complete →
  status: complete ←                 0x28 应答 status=0x00
  (规范: status 应为 "completed")

GET /v1/order/{id}               →    _order_events[oid] 内存查询
  (规范: GET /orders/{id})            (由 0xE1 on_report 填充)

GET /v1/order/{id}/stream        →    SSE，实时推送 0xE1 事件
  (规范: GET /orders/{id}/stream)
```

## 字段映射表

### Cart → line_item

| UCP field              | 来源                          |
|------------------------|-------------------------------|
| `id`                   | `"lane_" + lane_number`       |
| `name`                 | `catalog.json lanes[N].name`  |
| `price.amount`         | `catalog.json lanes[N].price` |
| `price.currency`       | `catalog.json currency`       |
| `quantity_available`   | 固定 `1`（WM800 需 0x24 才知实际库存，耗时 200s）|

### Checkout

| UCP field              | 内部存储 / WM800            |
|------------------------|-----------------------------|
| `id`                   | `chk_<uuid12>` 本地生成     |
| `status`               | `incomplete` → `ready_for_complete` → `complete`（规范应为 `completed`）|
| `line_items[0].id`     | 解析出 lane 编号 → `0x28 payload` |
| `order_id` (完成后)    | 16 位 hex，对应 WM800 8-byte order_id |
| `currency`             | ⚠️ **缺失**，规范必填（ISO 4217，应为 `"CNY"`） |
| `totals`               | ⚠️ **缺失**，规范必填（subtotal + total 数组） |

### Order → 0xE1 事件

| UCP event type     | WM800 0xE1 action_code |
|--------------------|------------------------|
| `started`          | 内部虚拟 (0xFF)         |
| `accepted`         | 内部虚拟 (0x00)         |
| `door_open`        | 0x01                   |
| `door_closed`      | 0x02                   |
| `goods_taken`      | 0x03 ← **终态，order status=complete** |
| `platform_home`    | 0x04 ← **备用终态**     |

Order `status: complete` 条件：收到 `action=0x03 (goods_taken)`。
> ⚠️ UCP 规范要求用 `"completed"`，当前实现用 `"complete"`（P0 差距）。

## 合规差距全表（按优先级）

| 优先级 | UCP 功能 / 字段 | 状态 | 说明 |
|--------|----------------|------|------|
| P0 | 端点路径 | ❌ 不合规 | 应为 `/checkout-sessions`，现为 `/v1/checkout` |
| P0 | `currency` 字段 | ❌ 缺失 | Checkout/Order 响应必须含 ISO 4217 货币码 |
| P0 | `totals` 字段 | ❌ 缺失 | 必须含 subtotal + total 数组 |
| P0 | `status: completed` | ❌ 值错误 | 规范用 `"completed"`，现用 `"complete"` |
| P0 | `messages[].content` | ❌ 字段名错 | 规范用 `content`，现用 `message` |
| P0 | HTTP 201 for checkout 创建 | ❌ 返回 200 | 创建资源应返回 201 Created |
| P1 | HTTP Message Signatures (RFC 9421) | ⏭ 未实现 | Webhook 签名是强制要求；`signing_keys: []` |
| P1 | `UCP-Agent` 头校验 | ⏭ 未实现 | 平台 profile 应被验证 |
| P1 | `Idempotency-Key` 头 | ⏭ 未实现 | POST/PUT 必须支持幂等键（缓存 24h） |
| P2 | `GET /checkout-sessions/{id}` | ❌ 缺失 | 规范要求查询单个 checkout |
| P2 | `PUT /checkout-sessions/{id}` | ❌ 缺失 | 更新 buyer / fulfillment |
| P2 | `POST /checkout-sessions/{id}/cancel` | ❌ 缺失 | 取消结账 |
| P2 | Profile `services` 结构 | ⚠️ 偏差 | 应为 object，现为 array |
| P2 | Payment handler `spec`/`schema` URL | ⚠️ 缺失 | 规范要求 handler 包含规范文档 URL |
| — | `continue_url` | ✅ 已实现 | 每条响应都带；错误时指向 fallback URL |
| — | SSE 实时事件流 | ✅ 已实现 | HttpOnly cookie 认证，实时推送 0xE1 事件 |
| — | OAuth 2.0 client_credentials | ✅ 已实现 | JWT HMAC-SHA256，3600s TTL |
| — | 精确库存 | ⚠️ 近似 | 总是返回 1；精确值需 0x24（~200s） |
| — | 多商品一次结账 | ⚠️ 限制 | 只取 `line_items[0]`，WM800 单次出一货道 |
| — | 退款 / adjustments | ❌ 不支持 | WM800 无退货概念 |
| — | Identity Linking | ⏭ 跳过 | 仅 client_credentials，无用户级 OAuth |
