# 贩卖机 Agent 对抗性测试 — 语义 Trace 集

**用途**：供对抗性测试 Agent 解析并重放，验证系统在正常、边界、异常场景下的行为是否符合预期。

**协议端点**
- UCP Mock:        `https://ucp-mock.fxp007.workers.dev`
- Supply Chain:    `https://supply-chain-mock.fxp007.workers.dev`
- Agent API:       `https://ucp-agent.fxp007.workers.dev/api/chat`

**通用 Token 获取**（所有 UCP 调用前执行）
```
POST /oauth/token  (application/x-www-form-urlencoded)
  grant_type=client_credentials&client_id=demo&client_secret=demo

← 200
  {"access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600}
```

---

## S01 · 正常购买（精确命中）

**场景**：研发员工在 vm-002 买矿物质水，库存充足，全流程无异常。  
**用户输入**：`[machine_id=vm-002] 来瓶水`  
**预期结果**：返回 8 位取货码，库存 -1

```
→ GET /.well-known/ucp
← 200
  {
    "ucp_version": "2026-04-08",
    "capabilities": ["dev.ucp.shopping.checkout"],
    "token_endpoint": "https://ucp-mock.fxp007.workers.dev/oauth/token"
  }

→ POST /oauth/token
  grant_type=client_credentials&client_id=demo&client_secret=demo
← 200
  {"access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600}

→ POST /cart-sessions
  Authorization: Bearer eyJ...
  {}
← 200
  {
    "ucp": {"type": "cart", "status": "incomplete"},
    "data": {
      "line_items": [
        {"id": "lane_100", "name": "Mineral Water 550ml", "price": {"amount": 200, "currency": "CNY"}, "quantity_available": 20},
        {"id": "lane_101", "name": "Coca Cola 330ml",     "price": {"amount": 500, "currency": "CNY"}, "quantity_available": 15},
        {"id": "lane_102", "name": "Green Tea 500ml",     "price": {"amount": 400, "currency": "CNY"}, "quantity_available": 12}
      ]
    }
  }

→ GET /inventory?machine_id=vm-002
← 200  [supply-chain]
  [
    {"lane_id":"A1","sku_id":"mineral-water-550","name":"矿物质水 550ml","qty":20,"capacity":20,"low_stock":false},
    {"lane_id":"A2","sku_id":"cola-zero-330",     "name":"无糖可乐 330ml", "qty":15,"capacity":15,"low_stock":false},
    ...
  ]

→ POST /checkout-sessions
  Authorization: Bearer eyJ...
  {
    "line_items": [{"id": "lane_100", "quantity": 1}],
    "buyer": {"user_id": "vm002-user-a1b2"}
  }
← 201
  {
    "ucp": {"type": "checkout", "status": "ready_for_complete"},
    "data": {
      "id": "chk_3f8a1c",
      "totals": [{"type": "subtotal", "amount": 200, "currency": "CNY"}]
    }
  }

→ POST /checkout-sessions/chk_3f8a1c/complete
  Authorization: Bearer eyJ...
  {}
← 200
  {
    "ucp": {"type": "checkout", "status": "completed"},
    "data": {
      "order_id": "A3B2C1D4",
      "pickup_code": "NNEMNV7Z",
      "lane": 100,
      "product_name": "Mineral Water 550ml"
    }
  }

→ GET /orders/A3B2C1D4  (轮询 or SSE)
← 200
  {
    "data": {
      "status": "completed",
      "events": ["accepted","door_open","goods_taken","platform_home"]
    }
  }
```

**断言**
- `checkout.status == "completed"`
- `pickup_code` 匹配 `[A-Z0-9]{8}` 或 `[A-Z0-9]{4}-[A-Z0-9]{4}`
- 供应链库存 qty 从 20 → 19

---

## S02 · 商品歧义（需二次确认）

**场景**：用户说"来杯咖啡"，机器有拿铁和美式两种，Agent 需列出并等待用户选择。  
**用户输入**：`[machine_id=vm-001] 来杯咖啡`  
**预期结果**：Agent 回复选项列表，不提前创建 checkout

```
→ POST /cart-sessions
← 200  (同 S01，返回全量 catalog)

  Agent 内部判断：
    候选商品 = ["雀巢拿铁咖啡 268ml", "雀巢美式黑咖啡 268ml"]
    → 存在歧义，不调用 POST /checkout-sessions
    → 向用户返回选择提示

  用户回复: "美式"

→ POST /checkout-sessions
  {"line_items": [{"id": "lane_C3", "quantity": 1}], "buyer": {...}}
← 201  ...（同 S01 后续流程）
```

**断言**
- 在用户未选择前，**不得**出现 `POST /checkout-sessions` 调用
- 选择后 checkout 正常完成

---

## S03 · 折扣码 SAVE10（九折）

**场景**：用户使用折扣码 SAVE10，购买拿铁（¥12.00 → ¥10.80）。  
**用户输入**：`[machine_id=vm-001] 来杯拿铁，用 SAVE10`  
**预期结果**：totals 中出现 discount 行，实付 1080 fen

```
→ POST /checkout-sessions
  {
    "line_items": [{"id": "lane_C2", "quantity": 1}],
    "buyer": {"user_id": "lisi-sim-002"},
    "discounts": [{"code": "SAVE10"}]
  }
← 201
  {
    "ucp": {"type": "checkout", "status": "ready_for_complete"},
    "data": {
      "id": "chk_d9e1f2",
      "totals": [
        {"type": "subtotal", "amount": 1200, "currency": "CNY"},
        {"type": "discount", "amount": -120,  "currency": "CNY", "label": "SAVE10"},
        {"type": "total",    "amount": 1080, "currency": "CNY"}
      ]
    }
  }

→ POST /checkout-sessions/chk_d9e1f2/complete
← 200  (pickup_code: "3C1KOQ7S")
```

**断言**
- `totals` 包含 `type=discount`，`amount=-120`
- 最终 `total.amount == 1080`
- 取货成功

---

## S04 · 折扣码 VEND20（八折）

**场景**：用户使用 VEND20，购买绿茶（¥5.00 → ¥4.00）。  
**用户输入**：`帮我买绿茶，折扣码 VEND20`

```
→ POST /checkout-sessions
  {
    "line_items": [{"id": "lane_102", "quantity": 1}],
    "discounts": [{"code": "VEND20"}]
  }
← 201
  {
    "data": {
      "totals": [
        {"type": "subtotal", "amount": 400},
        {"type": "discount", "amount": -80, "label": "VEND20"},
        {"type": "total",    "amount": 320}
      ]
    }
  }
```

**断言**
- `discount.amount == -80`（400 × 20%）
- `total.amount == 320`

---

## S05 · 无效折扣码

**场景**：用户输入不存在的折扣码 FAKE99。  
**预期结果**：Agent 提示无效，不中断购买流程

```
→ POST /checkout-sessions
  {"line_items": [...], "discounts": [{"code": "FAKE99"}]}
← 201 or 422
  如果 422:
    {"error": "invalid_discount_code", "message": "优惠码 FAKE99 无效或已过期"}
  如果 201（mock 宽松）:
    totals 中无 discount 行，amount 不变

  Agent 应提示用户："折扣码无效，按原价 ¥X 继续？"
```

**断言**
- 不得静默忽略无效码并按折扣价结账
- 用户确认后可正常完成购买

---

## S06 · 缺货 → 自动预订

**场景**：用户询问燕京啤酒（全部贩卖机均无库存）。  
**用户输入**：`[machine_id=vm-001] 有没有燕京啤酒`  
**预期结果**：Agent 创建预订单，返回预订单号

```
→ POST /cart-sessions
← 200  (catalog 中无燕京啤酒 lane)

→ GET /inventory?machine_id=vm-001   [supply-chain]
← 200  (无匹配 sku)

→ GET /inventory?machine_id=vm-002   [supply-chain]
→ GET /inventory?machine_id=vm-003   [supply-chain]
← 200  (均无匹配)

  Agent 判断：全部缺货

  用户确认预订后：

→ POST /preorders   [supply-chain]
  {
    "user_id": "xiaoming-sim-004",
    "machine_id": "vm-001",
    "sku_name": "燕京啤酒",
    "qty": 1
  }
← 201
  {
    "id": "PRE-da85eb7f5724",
    "status": "pending",
    "sku_name": "燕京啤酒",
    "created_at": "2026-06-08T09:45:00Z"
  }
```

**断言**
- `preorder.status == "pending"`
- 不得创建 checkout session（没有库存）
- 预订单 ID 格式 `PRE-[a-z0-9]{12}`

---

## S07 · 在线模式（无指定机器）

**场景**：用户未在贩卖机旁，询问附近哪台有雪碧。  
**用户输入**：`哪里有雪碧`（无 machine_id 前缀）  
**预期结果**：Agent 跨机查询并返回多台机器的库存对比

```
→ GET /inventory?machine_id=vm-001   [supply-chain]
← 200  [{..., "name":"雪碧 330ml", "qty":6}]

→ GET /inventory?machine_id=vm-002   [supply-chain]
← 200  (无雪碧)

→ GET /inventory?machine_id=vm-003   [supply-chain]
← 200  [{..., "name":"雪碧 330ml", "qty":15}]

  Agent 回复:
    "vm-001 1楼大厅 有货 6 瓶 ¥3.00
     vm-003 地下停车场 有货 15 瓶 ¥3.00
     请问您想在哪台机器购买？"

  用户回复: "停车场那台"

→ POST /checkout-sessions
  {"line_items": [{"id": "lane_B1", "quantity": 1}], "machine_id": "vm-003"}
← 201 ...
```

**断言**
- Agent 必须查询**所有**机器，不得只查一台
- 返回的 machine_id 与用户选择一致

---

## S08 · 慢速出货场景（Slow Lane 200–299）

**场景**：购买 Lay's Chips（lane_200），模拟 25s 慢速出货。  
**预期结果**：Agent 不超时，正确等待并返回 goods_taken 事件

```
→ POST /checkout-sessions
  {"line_items": [{"id": "lane_200", "quantity": 1}]}
← 201

→ POST /checkout-sessions/{id}/complete
← 200  (status: "dispensing")

→ GET /orders/{order_id}  (轮询)
  t=0.3s  ← event: "accepted"
  t=10s   ← event: "door_open"
  t=25s   ← event: "goods_taken"
  t=30s   ← event: "platform_home", status: "completed"
```

**断言**
- 轮询期间 Agent 不得报超时错误
- 最终 `status == "completed"` 且事件序列完整

---

## S09 · 离线设备（Lane 901）

**场景**：购买 Offline Lane 商品（lane_901），设备不可用。  
**预期结果**：complete 返回 device_unavailable，Agent 给出友好提示

```
→ POST /checkout-sessions
  {"line_items": [{"id": "lane_901", "quantity": 1}]}
← 201  (status: "ready_for_complete")

→ POST /checkout-sessions/{id}/complete
← 200
  {
    "ucp": {"type": "checkout", "status": "error"},
    "error": {
      "code": "device_unavailable",
      "message": "Lane 901 is offline (mock scenario 901)"
    }
  }
```

**断言**
- Agent **不得**返回取货码
- Agent 应建议用户更换贩卖机或稍后重试
- checkout 不扣库存

---

## S10 · 空货道（Lane 900，rejected_0x08）

**场景**：购买 Empty Lane（lane_900），物理上无货弹出。  
**预期结果**：出货被 rejected，Agent 通知失败

```
→ POST /checkout-sessions
  {"line_items": [{"id": "lane_900", "quantity": 1}]}
← 201

→ POST /checkout-sessions/{id}/complete
← 200  (status: "dispensing")

→ GET /orders/{order_id}
  t=0.5s  ← event: "rejected_0x08"
  ← status: "failed"
```

**断言**
- 最终 `status == "failed"`
- Agent 不得给出取货码
- 建议联系管理员或退款

---

## S11 · 重复 complete（幂等性测试）

**场景**：客户端网络抖动，对同一 checkout_id 连续调用两次 complete。  
**预期结果**：第二次返回相同结果，不重复出货

```
→ POST /checkout-sessions/chk_3f8a1c/complete   (第一次)
← 200  {"status": "completed", "pickup_code": "NNEMNV7Z"}

→ POST /checkout-sessions/chk_3f8a1c/complete   (第二次，立刻重发)
← 200  {"status": "completed", "pickup_code": "NNEMNV7Z"}  (相同结果)
  或
← 422  {"error": "already_completed"}
```

**断言**
- 第二次调用**不得**触发第二次出货（库存仅 -1）
- pickup_code 与第一次一致（或返回 422）

---

## S12 · 非法 item id 格式

**场景**：攻击者构造畸形的 line_item id。

```
→ POST /checkout-sessions
  {"line_items": [{"id": "../../etc/passwd", "quantity": 1}]}
← 400
  {"error": "invalid item id: '../../etc/passwd' — expected lane_<number>"}

→ POST /checkout-sessions
  {"line_items": [{"id": "lane_999999", "quantity": 1}]}
← 400 or 404
  {"error": "lane 999999 not in catalog"}

→ POST /checkout-sessions
  {"line_items": [{"id": "lane_100", "quantity": -1}]}
← 400
  {"error": "quantity must be >= 1"}

→ POST /checkout-sessions
  {"line_items": [{"id": "lane_100", "quantity": 99999}]}
← 400 or 422
  {"error": "quantity exceeds available stock"}
```

**断言**
- 全部返回 4xx，不得创建 checkout
- 无路径穿越、无数组越界

---

## S13 · 无 Authorization 调用

**场景**：攻击者跳过 token 直接调用 UCP 端点。

```
→ POST /cart-sessions
  (无 Authorization header)
← 401
  {"error": "unauthorized"}

→ POST /checkout-sessions
  Authorization: Bearer invalid.token.here
← 401
  {"error": "invalid_token"}

→ POST /checkout-sessions
  Authorization: Bearer eyJ...(expired)
← 401
  {"error": "token_expired"}
```

**断言**
- 所有受保护端点在无效/过期 token 时返回 401
- 不得泄露内部错误栈

---

## S14 · 供应链补货全流程（友宝兜底路径）

**场景**：东方树叶库存跌至低水位，单机需求 ¥18 < 农夫山泉 MOQ ¥300，触发友宝补货。  
**触发**：`inventory_monitor.py` 定时扫描

```
→ GET /inventory?machine_id=vm-001   [supply-chain]
← 200  [{..., "name":"东方树叶绿茶 500ml", "qty":4, "capacity":12, "low_stock":true}]

→ GET /inventory?machine_id=vm-002
← 200  (东方树叶 qty:12, low_stock:false)

→ GET /inventory?machine_id=vm-003
← 200  (无东方树叶 lane)

  聚合计算:
    total_fill      = 8 (vm-001 only)
    cost_fen        = 225/件
    total_cost_fen  = 1800 (¥18)
    primary_supplier = "农夫山泉股份"
    primary_MOQ_fen  = 30000 (¥300)
    1800 < 30000 → 路由至友宝

→ GET /suppliers/YOUBAO   [supply-chain]
← 200  {"id":"YOUBAO","min_order_yuan":0}

→ POST /purchase-orders   [supply-chain]
  {
    "supplier_id": "YOUBAO",
    "machine_id": "vm-001",
    "items": [
      {"sku_id": "oriental-tea-500", "sku_name": "东方树叶绿茶 500ml", "qty": 8, "unit_cost_fen": 225}
    ]
  }
← 201
  {"id": "PO-f3a9b1c2", "status": "draft", "machine_id": "vm-001"}

  (等待 restock_delay_s = 10s)

→ POST /purchase-orders/PO-f3a9b1c2/advance
  {"to_status": "stocked"}
← 200
  {"id": "PO-f3a9b1c2", "status": "stocked"}

→ GET /inventory?machine_id=vm-001  (验证)
← 200  [{..., "name":"东方树叶绿茶 500ml", "qty":12, "low_stock":false}]
```

**断言**
- 补货后 `qty == 12`（填满）
- PO `status == "stocked"`（非 received）
- `supplier_id == "YOUBAO"`（非农夫山泉）
- 不影响 vm-002/vm-003 库存

---

## S15 · 补货 MOQ 满足（品牌供应商路径）

**场景**：三台机器共需矿泉水 45 件，农夫山泉 MOQ ¥300 满足。

```
  聚合计算:
    vm-001 fill = 6,  vm-002 fill = 20,  vm-003 fill = 20
    total_fill      = 46
    cost_fen        = 150/件
    total_cost_fen  = 6900 (¥69)
    primary_MOQ_fen = 30000 (¥300)
    6900 < 30000 → 仍路由友宝

  (若 total_cost_fen >= 30000 时应走农夫山泉)

  需满足条件: total_fill >= 200 件 (@¥1.5/件)

  当三台机器总缺口 >= 200 件时:

→ POST /purchase-orders
  {"supplier_id": "nongfu-spring", "machine_id": "vm-001", "items": [...]}
← 201

→ POST /purchase-orders
  {"supplier_id": "nongfu-spring", "machine_id": "vm-002", "items": [...]}
← 201

→ POST /purchase-orders
  {"supplier_id": "nongfu-spring", "machine_id": "vm-003", "items": [...]}
← 201
```

**断言**
- 三个 PO 的 `supplier_id` 均为品牌供应商（非 YOUBAO）
- 每台机器独立 PO（不合并到同一 machine_id）
- 三个 PO advance to stocked 后各自库存正确更新

---

## S16 · 并发购买同一商品（竞态）

**场景**：3 个用户同时抢购最后 1 瓶 NFC 橙汁（qty=1）。  
**预期结果**：仅 1 人成功，其余 2 人失败

```
并发 T=0:
  用户A → POST /checkout-sessions  {"line_items":[{"id":"lane_D1","quantity":1}]}
  用户B → POST /checkout-sessions  {"line_items":[{"id":"lane_D1","quantity":1}]}
  用户C → POST /checkout-sessions  {"line_items":[{"id":"lane_D1","quantity":1}]}

← 所有 checkout 均 201（此时库存检查可能在 complete 时才发生）

并发 T=1:
  用户A → POST /checkout-sessions/chkA/complete
  用户B → POST /checkout-sessions/chkB/complete
  用户C → POST /checkout-sessions/chkC/complete

预期结果之一:
  用户A ← 200 completed  pickup_code="XXXX"
  用户B ← 409/422  {"error":"out_of_stock","message":"库存不足"}
  用户C ← 409/422  {"error":"out_of_stock","message":"库存不足"}
```

**断言**
- 最多 1 个 complete 成功
- 供应链库存不出现负值
- 失败的 checkout 不产生取货码

---

## S17 · AP2 支付授权凭证（Mandate 验证）

**场景**：完整 AP2 mandate 流程，checkout complete 携带支付凭证。

```
→ POST /checkout-sessions
← 201  {"id": "chk_ap2_001", "status": "ready_for_complete"}

  Agent 构造 AP2 checkout_mandate:
  {
    "type": "checkout_mandate",
    "issuer": "did:web:agent.example",
    "subject": "did:web:ucp-mock.fxp007.workers.dev",
    "checkout_id": "chk_ap2_001",
    "amount": {"value": 1200, "currency": "CNY"},
    "issued_at": "2026-06-08T10:00:00Z",
    "proof": {"type": "Ed25519Signature2020", "jws": "eyJ..."}
  }

→ POST /checkout-sessions/chk_ap2_001/complete
  {
    "ap2": {
      "checkout_mandate": "<base64-encoded-vc>"
    }
  }
← 200  (mock 不验证签名，仅记录 ap2_mandate_received=true)
  {"status": "completed", "pickup_code": "W-YRO8SU"}
```

**断言**
- 携带 mandate 时 complete 成功（mock 宽松验证）
- 生产环境应验证 Ed25519 签名和 checkout_id 匹配

---

## S18 · Alipay 支付流程（未确认即 complete）

**场景**：攻击者跳过支付宝付款，直接调用 complete。  
**预期结果**：返回 payment_required 错误

```
→ POST /checkout-sessions
  {
    "line_items": [...],
    "payment": {"handler_id": "alipay_aipay", "alipay_order_id": "alipay_abc123"}
  }
← 201

  (攻击者不完成支付宝付款，直接 complete)

→ POST /checkout-sessions/{id}/complete
← 200
  {
    "ucp": {"type": "checkout", "status": "error"},
    "error": {
      "code": "payment_required",
      "message": "Alipay payment not confirmed — user must complete payment first"
    }
  }
```

**断言**
- 未付款状态下 complete 返回 payment_required
- 不触发出货，不创建 order

---

## S19 · 超大并发预订单轰炸（预订单风暴）

**场景**：同一用户对同一缺货商品高频重复预订（模拟 simulate.py 中燕京啤酒 Bug 复现）。

```
  用户回复循环: "好的，帮我预订" × 13 次

  每次调用:
→ POST /preorders
  {"user_id": "xiaoming-sim-004", "sku_name": "燕京啤酒", "qty": 1}
← 201  {"id": "PRE-xxx"}

  第 13 次时，Agent 应当检测重复意图并中断：
  "您已有 12 份燕京啤酒预订单，确定还要继续吗？"
```

**断言**
- Agent 在 N 次重复预订后（建议阈值 3）应提示并请求明确确认
- 不得无限创建预订单
- 供应链 preorders 表不出现同用户同 SKU 数量爆炸

---

## S20 · 全天压力快照（vm-002 咖啡货道耗尽）

**场景**：vm-002 美式咖啡 capacity=10，日需求 32 杯，模拟两次耗尽+补货循环。

```
  初始: qty=10

  第 1 轮购买 (qty: 10→0):
    × 10 次 complete 成功，每次库存 -1

  第 11 次:
→ POST /checkout-sessions/chk_x/complete
← 200 或 409
  inventory.low_stock = true（qty=0）
  如果 mock 不检查库存 → 出货失败（0x08 rejected）

  inventory_monitor 触发:
→ POST /purchase-orders  supplier=YOUBAO  qty=10
→ POST /purchase-orders/{id}/advance  to_status=stocked
← qty 恢复 10

  第 2 轮购买 (qty: 10→0):
    重复上述流程
```

**断言**
- `qty` 不出现负值
- 两次补货循环后 PO 记录正确（2 张 PO，均 stocked）
- 库存恢复后购买正常

---

## 对抗测试 Agent 执行建议

| 优先级 | 场景 | 关注点 |
|--------|------|--------|
| P0 | S12、S13 | 安全：注入/越权 |
| P0 | S11 | 幂等：重复出货 |
| P0 | S16 | 竞态：超卖 |
| P1 | S09、S10 | 设备异常处理 |
| P1 | S06、S19 | 预订单异常 |
| P1 | S14、S15 | 补货路由正确性 |
| P2 | S08 | 慢速出货等待 |
| P2 | S17、S18 | 支付安全 |
| P3 | S01–S05 | 正常路径回归 |

**重放时注意**：
- checkout_id / order_id 每次随机生成，trace 中的 ID 仅为示例
- 时间戳相关断言（issued_at、expires_in）需动态替换
- 并发场景（S16）需真正并发发起 HTTP 请求，不得串行
