---
name: weimi-vending-api
description: Control 微米 (Weimi) cloud-managed vending machines (VMS-WM900XY 云台机 / WM500 / WM600 等). Includes Web 后台 (adm.weimi24.com / vm.weimi24.com) navigation, third-party REST API (/ext/...) auth + endpoint reference, and end-to-end dispense recipes. Triggers on "微米/weimi 贩卖机/售货机", "weimi24", "VMS-WM*", "ext/notify-shipment", "云台 API", or any reference to docs.weimi24.com.
---

# 微米 (Weimi) 售货机平台 + API

## When to use

- User asks to control / test / monitor a Weimi-managed vending machine
- User mentions `weimi24.com`, `VMS-WM900XY`, `WM500/600/22S/55S`, or a Weimi 设备号 (8-10 位数字)
- User wants to integrate Weimi as backend: place orders, query stock, receive webhook callbacks
- Debugging dispense failures (云台不动 / Status: Error / 2 分钟超时)

**NOT for**: direct serial-port control of WM800 / XX800 dispenser boards. That's the separate `vending-machine-control` skill.

## Quick orientation

There are **two** API surfaces on the same domain. Don't confuse them:

| Surface | Path prefix | Auth | Used by |
|---|---|---|---|
| **Web 后台 internal API** | `/v8/device-center-web/admin/…` | Session cookie (browser login) | The PC/mobile admin UIs that the operator logs into manually |
| **第三方 integration API** | `/v8/third-center-web/ext/…` | Signed headers (APP_ID + SIGN + TIMESTAMP + NONCE) | Your own server when you build a Weimi-backed product |

If the user wants programmatic control → **third-party API**. If they want to "看看后台" / "operate manually" → guide them through Web 后台.

## §1 Web 后台 (manual operation)

| URL | What it's for |
|---|---|
| `http://adm.weimi24.com/pc/#/login` | PC admin (desktop layout, dashboards) |
| `https://vm.weimi24.com/mobile/` | Mobile admin (field-engineer-style, has device-level test buttons) |

**Login** via username/password (operator credentials, e.g. `zhipu / zhipu888`). Same credential works on both surfaces.

### Where to find common functions

| Need | PC 后台 path | Mobile 后台 path |
|---|---|---|
| 设备列表 | 运营中心 → 设备运营 | Home → Machine management |
| 货道视图（含库存/价格/状态） | 运营中心 → 设备运营 → Machine management → A Cabinet | Home → Machine management → tap device → Machine inventory |
| **远程测试出货（单货道）** | ❌ 没有 | Home → Machine management → tap device → **Motor test** → per-slot "Test slot#N" button |
| 出货日志（含 Before/After 状态 + 库存变化） | 运营中心 → 货道操作记录 → Detail | Home → Operation record (Goods slot) |
| Repair Error 状态货道 | 运营中心 → 设备运营 → Machine management (在卡片操作里) | Home → Machine management → tap device → **Repair goods slot** |
| 订单 | 订单中心 → 今日/历史订单 | Home → Order management |
| 异常订单 | 订单中心 → 退款订单 | Home → 异常订单 |

### Web 后台抓到的实测端点（用作 API 调试参考）

平台 UI 里 "Motor test" → "Test slot#N" 按钮的实际 XHR：

```
POST https://vm.weimi24.com/v8/device-center-web/admin/deviceAisle/launch/test
Cookie: <session from /login>
Content-Type: application/json
Body: {<deviceCode + aisleCode>}  ← 准确字段需要抓包确认
```

这是 **internal** API，靠 session cookie 鉴权，**不是**给第三方用的。第三方走 §2。

**重要**：这个 endpoint 触发的是**完整的物理出货流程**——云台移动到对应货道、货道电机弹货、云台返回到取物口、解锁取物门——和用户扫码付款触发的全流程相同。**唯一区别**：不走支付、不留订单。是测试机器是否能正常出货的最佳工具。

## §2 第三方 Integration API

### 域名

| 环境 | 域名 |
|---|---|
| 测试 | `http://api.weimi24.com/v8/third-center-web` |
| 生产（国内） | `https://vm.weimi24.com/v8/third-center-web` |
| 生产（海外） | `https://micron.weimi24.com/v8/third-center-web` |

某些 endpoint（如 `订单查询`）文档里给的是 `v2022` 路径而不是 `v8`——按文档原样写，不要统一替换。

### 请求头（每次都要）

```
Client-Type: EXTERNAL
SIGN:        <see below>
TIMESTAMP:   <UTC millis at the moment of signing>
NONCE:       <13–32 chars random>
APP_ID:      <given to you by Weimi when onboarding>
Content-Type: application/json
```

**TIMESTAMP 服务端窗口 = 2 分钟**。超时签名失效，重新生成。

### SIGN 算法 ⚠️

**官方文档未公开 SIGN 算法的具体公式**。常见 ShowDoc-based 中国 IoT 平台的做法是 MD5/SHA256 over 排序后的请求参数 + APP_SECRET，但 Weimi 没有在公开文档里列出。

**操作步骤**：
1. 联系微米对接人索要：(a) APP_ID，(b) APP_SECRET，(c) **SIGN 计算示例（含 Java/Python demo）**
2. 在 `references/sign-algorithm.md` 里记录下来（这个文件留空，等拿到了写进去）
3. 用 `scripts/weimi_client.py` 里的 `sign()` 占位函数实现

> 在拿到 SIGN demo 前，**只能**用 Web 后台手动操作，不能调第三方 API。

### 核心端点

完整的 endpoint 列表见 `references/endpoints.md`。下面是最常用的 6 个：

#### 2.1 获取设备信息

```
GET /ext/device-profile?deviceCodes=6226030503,6226030504
```

返回 `list[].{deviceCode, deviceName, isRunning, isOnline, isGoodsMerge, totalCurrStock, cabinets[].{cabinetCode, doorStatus}}`。`isOnline=1` 才能下单。

#### 2.2 获取设备货道数据

```
GET /ext/device-info?deviceCodes=6226030503
```

返回完整货道树 `data[].cabinets[].layers[].aisles[]`，每个 aisle 有：

- `code` 货道编号（**注意**：API 里是 `"0-A00"` 这种格式，不是平台 UI 显示的 `0` 或 `1.01`）
- `currStock` 当前库存
- `goodsId`、`goodsName`、`price`（分）
- `isEnable` 是否启用、`isBroken` 是否故障

下单前必须先拉一次 `device-info` 拿到 `aisleCode` + `goodsId` 配对。

#### 2.3 弹簧柜出货通知 = 下单（核心）

**这是触发"全流程出货"的端点**（云台移动 + 货道电机 + 取物口检测 + 用户取走 + 闭门）。和 §1 里 "Motor test" 不同：

```
POST /ext/notify-shipment
{
  "userId": "your-user-id",
  "tradeNo": "your-trade-no-must-be-unique",
  "deviceCode": "6226030503",
  "aisleGoodsList": [
    {"aisleCode": "0-A00", "goodsId": "xxx", "price": 1, "count": 1}
  ],
  "payChannelCodeInt": 11001,
  "authType": 7,
  "payEndTime": 0
}
```

字段：
- `tradeNo`: 第三方交易号，**全局唯一**。重复用会失败
- `payChannelCodeInt`: 固定填 `11001`
- `authType`: 固定填 `7`
- `payEndTime`: 13 位时间戳；填 `0` 或不传 = 取服务器当前时间
- `price`: **分**（¥1.01 = `101`）

返回 `{tradeNoIn, orderId, tradeNoOut}`。

**注意**：这是"已支付的订单下单"——意味着调用方必须确保支付已完成（现金 / 自有渠道 / 测试场景）。Weimi 不在这一步收钱。

#### 2.4 出货结果推送（你这边收 webhook）

Weimi 会 POST 到你后台配置的回调 URL：

```
POST <your webhook>
{
  "orderId": "...",
  "tradeNoOut": "...",
  "tradeNoIn": "...",
  "deviceCode": "...",
  "goodsId": "...",
  "shipmentAisleCode": "0-A00",
  "shipmentStatus": 1,        ← 1=成功 2=进行中 3=失败
  "shipmentFailDesc": "...",  ← 失败时有
  "shipmentErrCode": 0,
  "shipmentTime": 1706847600000,
  "aisleMaxStock": 10,
  "aisleCurrStock": 9,
  "aisleIsBroken": 0
}
```

你必须回 `{"code":200,"msg":"success"}`。**重试策略**：5s → 10s → 2min → 5min → 10min → 30min → 1h，**最多 7 次**。设计幂等接收很重要——按 `orderId` 去重。

#### 2.5 订单查询

```
GET http://api.weimi24.com/v2022/third-center-web/ext/query-order-list?tradeNo=xxx&deviceCode=yyy
```

注意域名是 `api.weimi24.com/v2022/...` 不是 v8。其它字段同 webhook 的字段集。

#### 2.6 发送原始设备指令（passthrough）

```
POST /ext/sendSerialCmd
{
  "deviceCode": "...",
  "address": "...",
  "serialCmd": "EE010000000%s650005000005A1122"
}
```

是底层 EE 帧透传，等价于 `vending-machine-control` skill 里的串口协议。文档给的例子：

| 机型 | serialCmd |
|---|---|
| WM500/600 开门 | `EE010000000%s650005000005A1122` |
| WM22 冷柜门 | `EE010000000%s5600030101011122` |
| 冷冻柜强制化霜 | `EE010000000%s4100001122` |
| WM22S/55S 开门 | `EE010000000%s6500050F001E00001122` |

`%s` 是地址占位（API 会拼上 `address` 字段）。WM900XY 系列的指令在文档里没列——遇到要问对接人。

## §3 实测笔记（2026-05-26，VMS-WM900XY @ zhipu 账号）

来自测试设备 `6226030503`（VMS-WM900XY 云台机）。**这些是经过物理验证的事实，不是推测**。

1. **"Motor test" = 全流程出货（已校验）**。Mobile 后台 → Machine management → Motor test → "Test slot#N" 调用 `POST /v8/device-center-web/admin/deviceAisle/launch/test`，触发**云台移动 + 货道弹货 + 云台返回 + 取物口可开**的完整流程。这是测试机器是否能出货的**首选工具**——比走 `/ext/notify-shipment` 简单，也不留订单脏数据。

   > 早期测试时一度怀疑 "Motor test 只测槽位电机不动云台"，**这个假设错误**。实际是 RX/TX 接反导致机器侧根本没收到指令；接线修复后云台正常运动。

2. **2 分钟超时 + Status Normal→Error + Inventory 不变 = 机器侧无应答**。`launch/test` 返回 200 后，PC 端 `货道操作记录` 显示 `start → finish` 正好 2 分钟、`Operation status: Complete`，但点 Detail 弹窗看到 `Status: Normal → Error`、库存没减。这是**机器侧串口超时的标准签名**——下行指令没到、或上行应答没回。**先排查物理层**，不要怀疑 API。

3. **RX/TX 接反是头号物理故障**（验证过）。本测试机连续 2 次 launch/test 失败、上述 2 分钟超时签名完美出现。把控制板（云台主控 ↔ 下位机串口板）的 **RX/TX 两根线对调**后，下一次 Test slot 立即成功，云台动了。这条建议进诊断脚本的第一步。

4. **Slot 编号有两套，不要混用**：
   - **UI 数字编号**：PC/Mobile 后台上看的 `0, 1, 2, …, 8, 20, …, 80`，是 floor*10+col 风格"序号"。Mobile "Test slot#N" 按钮里 N 就是这个数字。
   - **API 货道码**：`/ext/device-info` 返回的 `code` 字段，格式 `<cabinet>-<layer><col>`，如 `0-A00, 0-A01, …, 0-H05`。`/ext/notify-shipment` 里的 `aisleCode` 必须用这个。
   - 下单前**必须**先调 `device-info` 拿到 `code` ↔ `goodsId` 配对。**不能**直接把 UI 数字当 `aisleCode` 用。

5. **Status: Error 不会自动恢复**。一次失败后该货道在 UI 里红字 Error，按钮仍可点，下次成功后**也不会**自动把 Error 清掉——红字保留。要清掉走 mobile 端 → Machine management → tap device → **Repair goods slot**。

6. **货道操作记录 Detail 弹窗里 Before/After 都不可靠**。Before 是"上一次 API 调用前的已知状态"，After 是"这次 API 调用后服务端推断的状态"——都是后台视角，不是机器侧实测。永远以 webhook 推送的 `shipmentStatus` 为权威真相。

7. **测试 ¥0.01 的货道**：本测试机有几个价格设为 0.01 的"测试货道"——slot 8 / 28 / 38 / 48 / 56-58 / 70-75 / 80-85（UI 编号）。但用 "Motor test" 触发出货**不走支付**，所以任何货道都能直接测，不必特意选 0.01 的。

8. **会话过期**：Mobile / PC 后台 cookie 会过期，重新打开会跳回登录页。**不要把后台 cookie 当永久凭证**。要稳定自动化只走 §2 的第三方 API。

## §4 调用配方

### 4.1 用 curl 调一次出货（生产）

需要你先有 APP_ID / APP_SECRET / SIGN 实现。占位代码：

```bash
APP_ID="<from weimi>"
DEVICE="6226030503"
TRADE_NO="test-$(date +%s)"

# Step 1: 拿货道信息找 aisleCode + goodsId
curl -s "https://vm.weimi24.com/v8/third-center-web/ext/device-info?deviceCodes=$DEVICE" \
  -H "Client-Type: EXTERNAL" \
  -H "APP_ID: $APP_ID" \
  -H "SIGN: $(python3 scripts/weimi_client.py sign GET /ext/device-info \"deviceCodes=$DEVICE\")" \
  -H "TIMESTAMP: $(date +%s%3N)" \
  -H "NONCE: $(openssl rand -hex 12)"

# Step 2: 下单
BODY='{
  "userId":"test",
  "tradeNo":"'$TRADE_NO'",
  "deviceCode":"'$DEVICE'",
  "aisleGoodsList":[{"aisleCode":"0-A01","goodsId":"<from-step-1>","price":1,"count":1}],
  "payChannelCodeInt":11001,
  "authType":7
}'

curl -s -X POST "https://vm.weimi24.com/v8/third-center-web/ext/notify-shipment" \
  -H "Client-Type: EXTERNAL" \
  -H "APP_ID: $APP_ID" \
  -H "SIGN: $(python3 scripts/weimi_client.py sign POST /ext/notify-shipment \"$BODY\")" \
  -H "TIMESTAMP: $(date +%s%3N)" \
  -H "NONCE: $(openssl rand -hex 12)" \
  -H "Content-Type: application/json" \
  -d "$BODY"

# Step 3: 30s 后查订单
sleep 30
curl -s "http://api.weimi24.com/v2022/third-center-web/ext/query-order-list?tradeNo=$TRADE_NO&deviceCode=$DEVICE" \
  -H "Client-Type: EXTERNAL" -H "APP_ID: $APP_ID" \
  -H "SIGN: $(python3 scripts/weimi_client.py sign GET /ext/query-order-list \"tradeNo=$TRADE_NO&deviceCode=$DEVICE\")" \
  -H "TIMESTAMP: $(date +%s%3N)" -H "NONCE: $(openssl rand -hex 12)"
```

### 4.2 调试出货失败的标准流程

1. `device-info` 检查 `isOnline=1` 且目标 aisle `isEnable=true, isBroken=false, currStock>0`
2. 调 `notify-shipment`，记下返回的 `orderId`
3. 60 秒内监听 webhook，记录 `shipmentStatus`
4. 没收到 webhook 就 `query-order-list?tradeNo=xxx` 拉一次
5. 如果 `shipmentStatus=3`（失败）：
   - 看 `shipmentFailDesc` 和 `shipmentErrCode`
   - **第一怀疑：物理层**（RX/TX、电源、卡货）
   - **第二怀疑：货道标记 Broken**——需要 Web 后台 Repair
   - **第三怀疑：库存 < 1**——需要补货
6. 如果 webhook 完全没来：webhook URL 配置错 / 你的服务器返回非 200

## §5 工具文件

| 文件 | 内容 |
|---|---|
| `references/endpoints.md` | 全 30+ 接口的 URL/参数/响应详细列表 |
| `references/sign-algorithm.md` | SIGN 算法实现笔记（**待补**，从 Weimi 对接人拿到后填） |
| `references/serial-commands.md` | `sendSerialCmd` 支持的所有 raw 指令列表 |
| `scripts/weimi_client.py` | Python 客户端骨架，封装 sign+headers+endpoint |

## §6 设备档案：6226030503（智谱测试机，VMS-WM900XY 云台机）

**这是唯一一台和 zhipu 账号绑定的测试设备**。其他 agent 接手时按这里走。

| 字段 | 值 |
|---|---|
| 设备号 | `6226030503` |
| 型号 | `VMS-WM900XY`（云台机，前面装玻璃门，云台抓臂从货道取货送到取物口） |
| 账号 | `zhipu / zhipu888` |
| 后台 URL（PC） | `http://adm.weimi24.com/pc/#/login` |
| 后台 URL（Mobile） | `https://vm.weimi24.com/mobile/` |
| 货道总数 | 63（8 floor，每 floor 5-9 槽） |
| 测试出货端点（Web） | `POST /v8/device-center-web/admin/deviceAisle/launch/test` |
| 测试出货按钮路径 | Mobile → Machine management → tap 6226030503 → Motor test → Test slot#N |

### 给其他 Agent 的 "测试一次出货" 操作流程

**前提**：需要现场有人配合放盒子 / 取盒子。云台运动时人要远离机器。

1. **跟用户对齐物理状态**：盒子放在哪个货道？人在什么位置？  
   - 必问：`AskUserQuestion({question:"盒子在哪个 slot？人是否远离云台？", options:[…]})`
   - 不能在用户没确认安全位置时下发出货指令。

2. **登录 mobile 后台**：用 `/browse` 打开 `https://vm.weimi24.com/mobile/`，viewport 设 `390x844`，登 `zhipu/zhipu888`，进 Machine management → tap 设备卡片 → Motor test。

3. **下发出货**：找到 `@e<N>` 对应 "Test slot#K"（K 是 UI 数字编号，和盒子所在 slot 对应）, 点击。toast 出现 "Testing, pls wait"。

4. **等待 + 观察**：
   - 顺利情况下 30-60s 内云台动 + 盒子掉到取物口。
   - 同步抓 network `POST .../deviceAisle/launch/test → 200`，记下 latency。
   - 如果 2 分钟过去机器没动 → 进入故障诊断（§3 笔记 #2-3）。

5. **闭环确认**：让用户报告"云台是否动了 / 盒子是否掉了 / 取物口能否打开"——这是物理真相，**不要**只看后台 status。

6. **看日志兜底**：PC 后台 → 货道操作记录 → 搜索 `6226030503` → 找最近一条 MOTOR_TEST → 点 Detail 看 Before/After 状态 + 库存。

### 给 Agent 的安全护栏

- ❌ **不要**在用户未确认安全距离前下发 Test slot 或 `notify-shipment`——云台运动可能伤手。
- ❌ **不要**对同一个 slot 连续狂点 Test slot——一次出货周期是 30-60s，2 分钟内重复触发会被服务端拒或叠加任务。
- ❌ **不要**用 PC 后台 cookie 自动化跑生产。要自动化走第三方 API。
- ✅ **要**在每次 dispatch 后等用户回报物理状态再继续。
- ✅ **要**记下每次 API 调用的 `tradeNo` / `orderId` 用于事后审计。

### 这台机器的已知特征

- 货道 `0-A00` (UI slot 0) 和 `0-A01` (UI slot 1) 历史上有过 Error 标记（2026-05-26 测试遗留）。如果需要干净状态，先 Repair。
- 所有货道当前都标商品名 "测试商品"，价格 ¥0.01 ~ ¥1.56——这是测试配置，**不是**真实业务货品。
- 服务费 0.0/年，下次缴纳时间 2027-06-30。

## §7 守则

1. **绝不在 prod 用 Web 后台 cookie 自动化**——会话过期、CSRF、ToS。要自动化只走 `/ext/...`。
2. **下单前必拉 `device-info`**——`aisleCode` / `goodsId` / 库存都来自这里。UI 上看的数字货道号 (0, 1, 2...) **不能**直接当 `aisleCode` 用。
3. **`tradeNo` 必须唯一**——建议 `${yourPrefix}-${ts}-${rand}`。重复会被服务端拒。
4. **永远用 webhook + query 双保险**——webhook 可能丢，主动 query 兜底。
5. **遇到"后台日志 Complete 但物理没动"**——99% 是机器侧问题（RX/TX、电源、串口板）。先排查物理，再怀疑指令。
6. **货道编号差异**: API 用 `0-A00`，UI 用 `0` / `1.01`。永远从 `device-info` 取 API 编号，永远从 UI 看人眼可读编号——两边不要混用。

---

*本 skill 基于 docs.weimi24.com:61900 (showdoc) + zhipu 账号 / 6226030503 (VMS-WM900XY) 实测，2026-05-26。*
