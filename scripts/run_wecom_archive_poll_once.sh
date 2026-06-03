#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_DATE="${1:-$(date +%F)}"
HEALTH_PATH="data/runtime/wecom_archive_health.json"

write_archive_health() {
  local ok="$1"
  local detail="$2"
  mkdir -p "$(dirname "$HEALTH_PATH")"
  .venv/bin/python - "$ok" "$detail" "$RUN_DATE" "$HEALTH_PATH" <<'PY'
import json
import sys
from datetime import datetime
ok, detail, run_date, path = sys.argv[1:5]
payload = {
    "ok": ok == "true",
    "source": "server_wecom_archive",
    "detail": detail,
    "checked_at": datetime.now().isoformat(timespec="seconds"),
    "date": run_date,
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PY
}

set +e
POLL_OUTPUT="$(timeout "${ARCHIVE_POLL_TIMEOUT_SECONDS:-300}" .venv/bin/python -m supplier_bot.cli poll-wecom-archive --date "$RUN_DATE" 2>&1)"
POLL_STATUS=$?
set -e
printf '%s\n' "$POLL_OUTPUT"
if [ "$POLL_STATUS" -ne 0 ]; then
  DETAIL="$(printf '%s' "$POLL_OUTPUT" | tail -n 20 | tr '\n' ' ' | cut -c1-1000)"
  write_archive_health false "官方收图失败：$DETAIL"
  exit "$POLL_STATUS"
fi

set +e
INBOX_OUTPUT="$(timeout "${INBOX_PROCESS_TIMEOUT_SECONDS:-600}" .venv/bin/python -m supplier_bot.cli process-inbox-events 2>&1)"
INBOX_STATUS=$?
set -e
printf '%s\n' "$INBOX_OUTPUT"
if [ "$INBOX_STATUS" -ne 0 ]; then
  DETAIL="$(printf '%s' "$INBOX_OUTPUT" | tail -n 20 | tr '\n' ' ' | cut -c1-1000)"
  write_archive_health false "收图入库失败：$DETAIL"
  exit "$INBOX_STATUS"
fi
write_archive_health true "官方收图和入库本轮成功"

timeout "${WORKFLOW_ONCE_TIMEOUT_SECONDS:-300}" .venv/bin/python -m supplier_bot.cli run-workflow-once --date "$RUN_DATE"
