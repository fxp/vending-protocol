# 安装这个 Skill

## 选项 A：放进个人 Claude Code skills 目录（推荐）

```bash
mkdir -p ~/.claude/skills
cp -r vending-machine-control ~/.claude/skills/
```

然后在 Claude Code 里输入 `/vending-machine-control` 触发，或者让 Claude 在你描述场景时自动识别（描述 frontmatter 已写好关键词）。

## 选项 B：作为 plugin 一部分

放到你的 plugin 目录的 `skills/` 下。

## 触发条件

skill 的 frontmatter 描述里写了 TRIGGER 关键词，Claude 看到下列任一关键词会自动调用：

- `WM800` / `某星XX800` / `售货机下位机`
- `vending machine controller`
- `出货指令` 或 `0x28 出货`
- `串口协议` + `售货机`
- `CSV 协议文档对不上`（这个机器协议常见问题）

## 不触发的场景

- 通用售货机硬件问题（机械故障 / 上料 / 钱币）
- 其他协议族：Crane、AEMP、MDB、CCTalk
- 上位机 UI 开发（Android / Kiosk 端）

## 文件清单

```
vending-machine-control/
├── SKILL.md                      ← Claude 主入口（带 frontmatter）
├── INSTALL.md                    ← 本文件
├── references/
│   ├── protocol.md              ← 全 22 条指令详细帧格式
│   ├── known-issues.md          ← 10 处文档/firmware 错漏
│   └── recipes.md               ← 拿来即用的代码片段
├── assets/
│   ├── wm800.py                 ← 协议库（CRC + 帧编解码 + Client）
│   ├── probe.py                 ← 只读探测，自动找设备地址
│   └── test_wm800.py            ← 交互式测试菜单
└── scripts/
    └── visual_capture.py        ← Photo Booth + screencapture 视觉验证
```

## 验证 skill 装对了

```bash
# 在 Claude Code 里：
/vending-machine-control

# 或者直接问问题：
"我有一台 某星XX800 售货机，怎么连？"
```

Claude 应该会引用 SKILL.md 的 §1 连接流程。
