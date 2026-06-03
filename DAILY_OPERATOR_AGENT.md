# 每日选款 Agent 常驻方案

这个项目的核心不依赖聊天 session 长期开着。生产形态是一个本地或服务器常驻 runner：runner 是持续运行的小程序，会定时处理消息、推进状态机、生成报表和待发队列。

## 推荐形态

### 本地常驻程序

适合测试期或需要桌面端企业微信兜底时使用：

- 每天按联系人角色表生成问款任务。
- 处理 `data/inbox_events/pending/` 中的新图片和文字。
- 生成 `data/tasks/YYYY-MM-DD/daily_workflow.json`。
- 生成桌面待发队列 `desktop_outbox.json`。
- 桌面端发送失败或结果不确定时进入人工确认队列。
- 所有状态落到本地数据库和 `data/tasks/`，重启后可继续推进。

### 官方收图 + 桌面发送

适合已开通企业微信会话内容存档，但对外发送仍需要桌面端兜底的场景：

- 官方 SDK 拉取供应商图片、文字回复和选款人截图。
- 桌面端负责问款、发报表、寄样请求和运营表。
- 桌面发送的问款消息会带唯一批次码，便于从 SDK outgoing 消息证明官方联系人映射。

### 全官方收发

这是长期更稳定的方向：

- 发送和接收都使用企业微信官方接口。
- 同一个 `external_userid` 负责发送和接收，不再依赖桌面会话名到官方 ID 的翻译。
- 需要企业微信客户联系权限、可信 IP、域名和合规配置。

## 常驻运行

命令行启动：

```bash
.venv/bin/python scripts/run_daily_operator_agent.py \
  --ask-at 09:00 \
  --auto-send-desktop \
  --desktop-send-limit 10 \
  --no-auto-poll-wecom-archive
```

测试期也可以双击：

```text
start_supplier_bot.command
```

它会检查企业微信桌面自动化权限，并把日志写入 `logs/supplier_bot_daily.log`。日志目录被 `.gitignore` 排除，不应提交。

## 每日业务流程

```text
08:30 生成今日询问计划
09:00 分批向供应商要新款
09:00-14:00 持续接收图片/文字回复
收到图片 -> 等供应商最后一张图安静一段时间 -> 入库 -> 分类 -> 款式合并
14:00 对未回复供应商生成提醒
15:00 截止等待并生成选款报表
报表发给选款人 -> 等截图圈选回传
识别选款结果 -> 按供应商拆分寄样和商品信息请求
供应商回商品信息 -> 生成运营结构化表
```

状态机会保存在：

```text
data/tasks/YYYY-MM-DD/daily_workflow.json
```

主要状态：

```text
pending_ask -> ask_sent -> waiting_images -> images_received -> report_ready
-> report_sent -> selection_received -> sample_requested
-> supplier_info_received -> ops_table_sent -> done
```

## 手动调试命令

初始化或查看当天状态：

```bash
.venv/bin/python -m supplier_bot.cli init-daily-workflow --date 2026-05-24
.venv/bin/python -m supplier_bot.cli show-daily-workflow --date 2026-05-24
```

跑一次状态机：

```bash
.venv/bin/python -m supplier_bot.cli run-workflow-once --date 2026-05-24
```

查看桌面待发队列：

```bash
.venv/bin/python -m supplier_bot.cli show-desktop-outbox --date 2026-05-24 --pending-only
.venv/bin/python -m supplier_bot.cli show-desktop-outbox --date 2026-05-24 --next
```

手动标记已发送：

```bash
.venv/bin/python -m supplier_bot.cli mark-outbox-sent --date 2026-05-24 ask:2026-05-24:SUPPLIER_ID
```

## 企业微信接入边界

真实企业微信接入需要：

- 企业 `CorpID`
- 自建应用 `AgentID` 和 Secret
- 回调 URL、Token、EncodingAESKey
- 企业可信 IP 或可访问的公网服务
- 会话内容存档 Secret
- 会话内容存档 RSA 私钥
- 会话内容存档 C SDK 动态库
- 外部联系人会话存档合规授权

这些值只能写入 `.env` 或部署环境变量，不能提交到 GitHub。

## 为什么不是 Codex Skill

Skill 适合作为操作说明或维修手册，但不能 7x24 运行，也不能在电脑重启后自动恢复业务状态。

本项目的正式形态是：

```text
常驻 runner + 本地数据库 + 企业微信官方接口 + 桌面端兜底 + 人工确认边界
```

