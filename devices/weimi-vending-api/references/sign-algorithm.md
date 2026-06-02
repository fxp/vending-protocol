# SIGN 算法

## 状态

⚠️ **待补**：官方 ShowDoc (`http://docs.weimi24.com:61900/web/#/33/1569`) 的"接口说明"页只给了 5 个签名头的字段名（`Client-Type / SIGN / TIMESTAMP / NONCE / APP_ID`），**没有公开 SIGN 的计算公式**。

要拿到正式实现，必须找微米对接人索取：
1. `APP_ID` + `APP_SECRET`
2. SIGN 计算公式（语言无关的描述）
3. 一份 demo（Java / Python / Node 任一）+ 一个能验证签名是否正确的 echo 接口

## 接到 demo 后这里要写什么

按这个模板填：

```
算法：<MD5 / HMAC-SHA256 / RSA / ...>

入参组装规则：
  1. <例如：把 APP_ID, TIMESTAMP, NONCE, body 串起来>
  2. <例如：按字典序排序>
  3. <例如：拼上 APP_SECRET>

伪代码：
  payload = APP_ID + TIMESTAMP + NONCE + sha256(body_json)
  SIGN = HMAC_SHA256(APP_SECRET, payload).hex().upper()

实测向量（拿对接人提供的示例数据填，写完一定要跑过 echo 验证）：
  APP_ID=...
  APP_SECRET=...
  TIMESTAMP=...
  NONCE=...
  body=...
  期望 SIGN=...
```

## 常见 ShowDoc 中国 IoT 平台的"参考"模式（不一定准）

只是猜测，**不要直接用**：

```python
# 推测：常见做法是 MD5/SHA256 over sorted_params + secret
def sign(app_id, timestamp, nonce, body, app_secret):
    parts = [app_id, timestamp, nonce, body or ""]
    raw = "".join(parts) + app_secret
    return hashlib.sha256(raw.encode()).hexdigest().upper()
```

确认前不要拿来跑。

## 验证套路

拿到对接人 demo 后：

1. 抄一份到 `scripts/weimi_client.py` 里的 `sign()`
2. 用 demo 里的"已知向量"（known input → known sig）做单元测试
3. 用最便宜的 GET（`/ext/device-profile?deviceCodes=空串`）连一次 prod，code=200 才算调通
4. 调通后更新 `SKILL.md` 把 "⚠️ SIGN 算法待补" 段落删掉
