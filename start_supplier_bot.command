#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "未找到 .venv/bin/python，请先按 README 安装环境。"
  read "?按回车退出"
  exit 1
fi

mkdir -p logs
echo "$(date '+%Y-%m-%dT%H:%M:%S') desktop launcher opened" >> logs/supplier_bot_launcher.log
echo "检查企业微信桌面自动化权限..."
CHECK_OUTPUT="$(.venv/bin/python -m supplier_bot.cli check-desktop-automation)"
echo "$CHECK_OUTPUT"
if ! echo "$CHECK_OUTPUT" | .venv/bin/python -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)'; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') desktop launcher automation check warning: $CHECK_OUTPUT" >> logs/supplier_bot_launcher.log
  echo "桌面自动化检查未通过：桌面发送可能暂时不可用。"
  echo "每日选款助手仍会继续启动；服务器 SDK 收图、状态机推进和数据同步不依赖桌面窗口。"
  echo "如需自动发送，请稍后确认企业微信主窗口可见，并查看 logs/supplier_bot_daily.log。"
fi

echo "启动每日选款助手，日志：logs/supplier_bot_daily.log"
echo "$(date '+%Y-%m-%dT%H:%M:%S') desktop launcher starting daily operator agent" >> logs/supplier_bot_launcher.log
exec .venv/bin/python scripts/run_daily_operator_agent.py --ask-at 09:00 --auto-send-desktop --desktop-send-limit 10 --no-auto-poll-wecom-archive --no-auto-capture-supplier-desktop --sync-server-data
