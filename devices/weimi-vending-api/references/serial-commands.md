# sendSerialCmd 原始指令参考

`POST /ext/sendSerialCmd` 让你把 raw 串口指令穿透到设备的下位机。等价于直接接串口发的 EE 帧。

## 帧结构

```
EE 01 00 00 00 00 %s <cmd-payload> 11 22
   └─┬─┘ └────┬────┘  └─┬─────────┘ └┬─┘
   ver  addr(4B)       payload      tail(2B?)
```

- `EE` 帧头
- `01` 协议版本
- `%s` API 会用 `address` 字段替换的占位
- 帧尾 `11 22` 是固定字节（不是 CRC，至少在这些示例里）

## 已知指令

### WM500 / WM600 — 开取物口门

```
EE010000000%s650005000005A1122
```

`65 00 05 00 00 05 A1 12 2` 是 payload（开门指令字 + 数据）。

### WM22 — 冷柜门控制

```
EE010000000%s5600030101011122
```

`56 00 03 01 01 01` 是开冷柜门的命令字 + 操作字节。

### 冷冻柜 — 强制化霜

```
EE010000000%s4100001122
```

`41 00 00` 是化霜命令。

### WM22S / WM55S — 开取物口门

```
EE010000000%s6500050F001E00001122
```

和 WM500/600 不同的子命令字。

## 未公开的机型

VMS-WM900XY（云台机）的 serialCmd 文档里**没列**。如果要走这条路：

1. 抓包 Web 后台移动端 "Motor test" 那一刻设备下行报文（如果有抓包权限）
2. 找微米对接人要 WM900XY 的指令表
3. 或者绕过 `sendSerialCmd`，走 `/ext/notify-shipment` 让平台自己生成正确的下行帧

后者是首选。`sendSerialCmd` 是"逃生通道"，业务流程优先用 `notify-shipment`。

## 风险

- `sendSerialCmd` 不会被业务层校验——可以发任意 EE 帧。
- 如果发了错误的命令字，可能让设备状态机进入异常分支（货道误动作 / 门状态错乱 / 报警）。
- 发指令后**没有同步应答**（返回体 `{}`）。要看结果只能：
  - 看 webhook 推送（仅业务相关命令有）
  - 查 `device-profile.doorStatus`（仅门状态相关）
  - 走 `/ext/sendSerialCmdAndWaitResp` 之类的同步版本（如果存在——文档里没列）

## 参考：WM800 协议对照

如果想理解 `serialCmd` 里的命令字含义，参考另一个 skill 的协议表：`~/.claude/skills/vending-machine-control/references/protocol.md`。命令字编号和子命令字大部分通用。

注意黑名单：WM800 firmware 有 10+ 条文档支持但实际不实现的指令（`0x3D`、`0x23`、`0xBC`、`0x2C` 等）。WM500/600/22/22S/55S 系列没单独验证，谨慎使用。
