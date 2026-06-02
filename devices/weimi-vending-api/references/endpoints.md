# 微米第三方 API 全端点参考

域名前缀（生产国内）：`https://vm.weimi24.com/v8/third-center-web`

特殊：`订单查询` 类用 `http://api.weimi24.com/v2022/third-center-web`（注意 `v2022` 而非 `v8`）。

所有请求需要 5 个签名头：`Client-Type / SIGN / TIMESTAMP / NONCE / APP_ID`。

## 设备相关

### GET `/ext/device-profile` — 获取设备信息

Query：`deviceCodes` (逗号分隔，空串=全部)

返回：
```json
{"list":[{
  "deviceCode":"6226030503",
  "deviceName":"",
  "isRunning": 1,           // 0=不在运营时间 1=运营中
  "isOnline": 1,            // 0=离线 1=在线
  "isGoodsMerge": 1,        // 0=不合并 1=合并货道
  "totalCurrStock": 0,
  "cabinets":[{
    "cabinetCode": "",
    "doorStatus": ""        // 仅供参考
  }]
}]}
```

### GET `/ext/device-info` — 获取设备货道数据

Query：`deviceCodes`

返回：`data[]` 每台设备的完整货道树。每个 aisle：
```json
{
  "id":"ccd4d266143c99129c0ecae31727980b",
  "cabinet": 0,
  "layer": "A",
  "code": "0-A00",          // API 用的货道编号
  "shippingMode": 1,
  "showName": "A1",
  "ctrlBoard": 0,
  "ctrlCmd": 100,
  "measurement": 1,
  "price": 1,               // 分
  "showPrice": 1,
  "maxStock": 10,
  "currStock": 9,
  "order": 0,
  "status": 1,
  "remark": "",
  "goodsId": "2a5b93b42777806223d530d9e281bbc9",
  "goodsName": "百事可乐",
  "goodsCode": "037dc1919ce44d9a8cb7531fb382cf87",
  "goodsCustomCode":"fb0263feb9a54d80b0630ec371981dfd",
  "barcode": "string",
  "currency": "1",
  "imgUrl": "...",
  "thumbnailUrl": "...",
  "isEnable": true,
  "isBroken": false,
  "weight": 0,
  "goodsTypeList":[{"id":"...","code":"drinks","name":"饮料"}]
}
```

### GET `/ext/query-stock` — 获取商品库存信息

（参数详见官方 ShowDoc）

## 订单相关

### POST `/ext/notify-shipment` — 弹簧柜出货通知（核心下单）

Body:
```json
{
  "userId": "qingxiao-1",
  "tradeNo": "tradeNo20220615-1",   // 全局唯一
  "deviceCode": "81260161",
  "aisleGoodsList": [
    {"aisleCode":"0-A0", "goodsId":"12", "price":100, "count":1},
    {"aisleCode":"0-A0", "goodsId":"132", "price":100, "count":1},
    {"aisleCode":"0-A1", "goodsId":"123", "price":100, "count":1}
  ],
  "payChannelCodeInt": 11001,        // 固定 11001
  "authType": 7,                     // 固定 7
  "payEndTime": 0                    // 0 = 服务器当前时间
}
```

返回：
```json
{
  "tradeNoIn": "812601611655345456079",     // 微米交易号
  "orderId": "06005d205267c9b1b670bfaa27208ffa",
  "tradeNoOut": "tradeNo20220615-1"
}
```

### Webhook：弹簧机出货结果推送

Weimi POST 到你的 webhook URL：
```json
{
  "orderId": "",
  "tradeNoOut": "",
  "tradeNoIn": "",
  "deviceCode": "",
  "goodsId": "",
  "shipmentAisleCode": "",
  "shipmentStatus": 1,           // 1=成功 2=进行中 3=失败
  "shipmentFailDesc": "",
  "shipmentTime": 0,             // 13 位时间戳
  "shipmentErrCode": 0,
  "aisleMaxStock": 0,
  "aisleCurrStock": 0,
  "aisleIsBroken": 0             // 0=正常 1=故障
}
```

你必须回 `{"code":200,"msg":"success"}`。

**重试**：5s → 10s → 2min → 5min → 10min → 30min → 1h，最多 7 次。所以 webhook 接收端必须**幂等**——按 `orderId` 去重。

### GET `/ext/query-order-list` — 订单查询

**特殊**：完整 URL `http://api.weimi24.com/v2022/third-center-web/ext/query-order-list`（v2022！）

Query: `tradeNo` (true) + `deviceCode` (true)

返回 `list[]` 每条订单：
```json
{
  "orderId": "06005d205267c9b1b670bfaa27208ffa",
  "orgId": "",
  "deviceCode": "",
  "payChannelCodeInt": 11001,
  "totalAmount": 100,             // 分
  "tradeNo": "",
  "userId": "",
  "detailVOList": [{
    "aisleCode": "",
    "goodsId": "",
    "goodsName": "",
    "payAmount": 100,
    "shipmentAisleCode": "",
    "shipmentStatus": 1,
    "shipmentFailDesc": "",
    "shipmentTime": 0,
    "shipmentErrCode": 0,
    "aisleMaxStock": 10,
    "aisleCurrStock": 9,
    "aisleIsBroken": 0
  }]
}
```

### `/ext/query-order` — 订单详情

`/ext/query-today-order` — 今日订单分页

`/ext/query-history-order` — 历史订单分页

`/ext/query-refund-order` — 退款订单分页

（参数 + 响应详见官方 ShowDoc 对应页）

## 设备控制

### POST `/ext/sendSerialCmd` — 发送原始串口指令

Body:
```json
{
  "deviceCode": "...",
  "address": "...",
  "serialCmd": "EE010000000%s650005000005A1122"
}
```

`%s` 是地址占位，由 `address` 字段拼入。

常用指令示例：

| 机型 + 动作 | serialCmd |
|---|---|
| WM500 / WM600 开门 | `EE010000000%s650005000005A1122` |
| WM22 冷柜门 | `EE010000000%s5600030101011122` |
| 冷冻柜强制化霜 | `EE010000000%s4100001122` |
| WM22S / WM55S 开门 | `EE010000000%s6500050F001E00001122` |

详见 `serial-commands.md`。WM900XY 系列没有公开示例——找对接人。

## 视觉柜（专门走 §X，本设备 VMS-WM900XY 不涉及）

- `/ext/visual/...` 一组接口（视觉商品识别、视觉商品建模、视觉 SKU 查询等）
- 设备非微米平台客户使用

## 重力柜 / 视觉柜专属

- `/ext/open-door` — 下单开门
- Webhook：关门结果推送

## 惠拼购（社群拼团）

- `/ext/huipingo/device-list`
- `/ext/huipingo/coupon-recharge`

## 兑换码

- `/ext/voucher/create` — 兑换码生成
- `/ext/voucher/detail` — 兑换码详情

## 商品

- `/ext/goods/query`
- `/ext/goods/upsert`

## 会员

- `/ext/member/wallet-transactions`

## 自定义会员订单 / 山西泽莱 / 迪拜沙滩床浆租赁

特定客户/场景的接口，本设备不涉及。

---

完整接口数量 30+ 条，本文件覆盖主要路径。需要具体某条的完整字段时去 ShowDoc 查：`http://docs.weimi24.com:61900/web/#/33/`。
