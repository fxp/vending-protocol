# vending-protocol

实测过、面向 AI Agent 的**自动贩卖机/无人零售场所**协议与 skill 集合。

不是"教程"也不是"通用 SDK"。每个目录都是一台**真机被反复打过**之后留下的工作笔记 + 可直接安装的 Claude Code skill。

> 自动贩卖机的官方文档常有 10+ 处错漏，firmware 经常和 spec 对不上。本仓库的价值在于**把错的标出来 + 给可直接 copy 的 recipe**，让下一个 Agent / 工程师不再踩同样的坑。

## 仓库结构

```
vending-protocol/
├── devices/                          一台具体机器 / 一个具体云平台
│   ├── weimi-vending-api/            微米 (weimi24.com) 云端管理：VMS-WM900XY / WM500 / WM600 / WM22 系列
│   │                                 涵盖 Web 后台 + 第三方 REST API + webhook 回调
│   └── vending-machine-control/      某星 XX800 / WM800 系列下位机直连串口控制
│                                     覆盖 RS232/RS485 + EE 协议帧 + 出货/扫描/状态查询
├── venues/                           场所级协议（小店收银 / 智能货柜组 / 无人店）
│                                     目前还空。等场地实测过再填。
└── install.sh                        一键 symlink 所有 skill 到 ~/.claude/skills/
```

> 目录名 == skill 的 frontmatter `name:`，方便 `install.sh` 直接 symlink。

每个 `devices/<name>/` 目录都是一个独立的 Claude Code skill，结构：

```
<name>/
├── SKILL.md                     skill 入口（frontmatter + 触发关键词 + 主体内容）
├── INSTALL.md                   可选：安装说明
├── references/                  详细协议表 / 端点字典 / 已知问题
├── scripts/                     可执行客户端 / 探测工具
└── assets/                      可选：例子数据、固件 dump、波形截图
```

## 已覆盖

### `devices/weimi-vending-api/` — 微米云贩卖机

| 触发场景 | 解决 |
|---|---|
| "测一下微米机器" / "weimi24" | Web 后台导航 + Mobile 端 Motor test 操作流 |
| "VMS-WM900XY" / "云台 API" | 第三方 `/ext/...` 接口 30+ 条参考 |
| "下单后云台不动" | 2 分钟超时签名 → RX/TX 接反 头号怀疑点 |
| "怎么集成进我的系统" | `/ext/notify-shipment` + webhook 回调 + 幂等接收 |

测试设备：`6226030503`（VMS-WM900XY 云台机，绑 zhipu 账号）。

### `devices/vending-machine-control/` — WM800 串口协议

| 触发场景 | 解决 |
|---|---|
| "WM800 怎么连" | USB→RS232/RS485 + 9600 8N1 + 地址 probe |
| "让它出货" | `0x28` 出货流程 + `0xE1` 主动上报监听 |
| "扫货道" | `0x2B` 步数表 / `0x21` 物理扫描 |
| "文档说 X 实际 Y" | 10+ 处文档错漏 + firmware 黑名单 |

> 黑名单（永远不要发）：`0x3D`、`0x23`、`0xBC`、`0x2C`、`0x04`（出货预检）、`0x35 type=1`。详见 `vending-machine-control/references/known-issues.md`。

### `venues/` — 占位

小店收银台 / 智能货柜组合 / 无人店的整店级协议——等到现场实测过再填。

## 安装

把所有 skill 软链接到 Claude Code 的 skill 目录：

```bash
git clone https://github.com/<owner>/vending-protocol.git
cd vending-protocol
./install.sh
```

`install.sh` 做的事：

```bash
mkdir -p ~/.claude/skills
for d in devices/*/; do
  name=$(basename "$d")
  ln -sfn "$(pwd)/$d" "$HOME/.claude/skills/$name"
done
```

软链而不是 copy——这样 `git pull` 拉到的更新立即对所有 Claude Code 会话生效。

## 触发方式

进 Claude Code 后任一方式：

1. **关键词触发**（推荐）：在对话里提"微米机器"、"VMS-WM900XY"、"WM800 出货"等关键词，Claude 自动识别并加载对应 skill。
2. **显式调用**：`/weimi-vending-api` 或 `/vending-machine-control`。

每个 skill 的 `SKILL.md` frontmatter 都列了完整触发词。

## 贡献

加新设备 / 新场所：

1. 在 `devices/` 或 `venues/` 下新建目录
2. 至少包含 `SKILL.md`（参考现有的 frontmatter 格式）
3. 在 README "已覆盖" 区加一行
4. PR

**质量要求**：必须是**实测过**的内容。"我猜文档大概是这样"的不收。每条不准的规格都要标 ⚠️ 加证据。

## 设计原则

- **真机优先**：实测 > 文档 > 推测。文档错的地方明确标出来。
- **面向 Agent**：每条建议都写给"下一个不熟悉这台机的 Agent"看，不是给人当教科书。
- **快速判断流程**：每个 SKILL.md 开头给一棵"用户说 X → 看 §Y"决策树。
- **安全护栏**：涉及电机/云台/门锁的动作都要"先确认现场状态再下发"——绝不静默执行物理动作。

## License

MIT
