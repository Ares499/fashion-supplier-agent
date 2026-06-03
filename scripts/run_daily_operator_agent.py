from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, time as day_time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from supplier_bot.config import config
from supplier_bot.alerts import send_receive_channel_failure_alert
from supplier_bot.desktop_receiver import (
    capture_selector_selections,
    capture_waiting_supplier_images,
)
from supplier_bot.desktop_outbox import load_outbox, write_outbox
from supplier_bot.desktop_sender import check_desktop_automation, send_pending_desktop_outbox
from supplier_bot.inbox_events import process_pending_inbox_events
from supplier_bot.scheduler import is_supplier_rest_day
from supplier_bot.storage import Store
from supplier_bot.receive_recovery import (
    diagnose_receive_channel,
    receive_recovery_required,
    reconcile_receive_recovery,
    record_receive_channel_failure,
    record_receive_channel_recovery,
)
from supplier_bot.wecom import WeComClient
from supplier_bot.wecom_archive import poll_message_archive_into_inbox
from supplier_bot.workflow_engine import WorkflowEngine


LOG_DIR = ROOT / "logs"
STATE_PATH = ROOT / "data/runtime/daily_operator_state.json"
RECEIVE_HEALTH_MAX_AGE_SECONDS = 30 * 60

def should_stop_for_receive_channel(runtime_mode: str, receive_channel_ok: bool) -> bool:
    return runtime_mode in {"official", "hybrid"} and not receive_channel_ok


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    with (LOG_DIR / "supplier_bot_daily.log").open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")


def _server_sync_command() -> list[str] | None:
    if not config.server_sync_target:
        return None
    command = ["rsync", "-az"]
    if config.server_sync_ssh_key:
        command.extend(["-e", f"ssh -i {config.server_sync_ssh_key} -o StrictHostKeyChecking=no"])
    return command


def _server_sync_parts() -> tuple[str, str] | None:
    if not config.server_sync_target or ":" not in config.server_sync_target:
        return None
    host, path = config.server_sync_target.split(":", 1)
    if not host or not path:
        return None
    return host, path.rstrip("/")


def run_remote_archive_poll_once(run_date: date) -> dict:
    parts = _server_sync_parts()
    if not parts:
        return {"ok": False, "detail": "未配置 SERVER_SYNC_TARGET，无法触发服务器补拉"}
    host, remote_root = parts
    command = ["ssh"]
    if config.server_sync_ssh_key:
        command.extend(["-i", config.server_sync_ssh_key])
    command.extend(
        [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            host,
            f"cd {remote_root} && scripts/run_wecom_archive_poll_once.sh {run_date.isoformat()}",
        ]
    )
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        return {
            "ok": True,
            "detail": "服务器官方收图补拉成功",
            "stdout_tail": completed.stdout[-2000:],
        }
    except Exception as exc:
        stdout = getattr(exc, "stdout", "") or ""
        stderr = getattr(exc, "stderr", "") or ""
        return {
            "ok": False,
            "detail": f"服务器官方收图补拉失败：{exc}",
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        }


def _snapshot_sent_outbox_tasks(run_date: date) -> dict:
    path = config.data_dir / "tasks" / run_date.isoformat() / "desktop_outbox.json"
    return {task.task_id: task for task in load_outbox(path) if task.status == "sent"}


def _restore_sent_outbox_tasks(run_date: date, sent_before_sync: dict) -> bool:
    if not sent_before_sync:
        return False
    path = config.data_dir / "tasks" / run_date.isoformat() / "desktop_outbox.json"
    tasks = load_outbox(path)
    by_task_id = {task.task_id: task for task in tasks}
    changed = False
    for task_id, sent_task in sent_before_sync.items():
        current = by_task_id.get(task_id)
        if not current:
            tasks.append(sent_task)
            changed = True
            continue
        if current.status != "sent" or current.sent_at != sent_task.sent_at:
            current.status = "sent"
            current.sent_at = sent_task.sent_at
            current.metadata.update(sent_task.metadata)
            changed = True
    if changed:
        write_outbox(path, tasks)
    return changed


def sync_server_data_to_local(run_date: date | None = None) -> None:
    command = _server_sync_command()
    if not command:
        return
    sent_before_sync = _snapshot_sent_outbox_tasks(run_date) if run_date else {}
    remote = config.server_sync_target.rstrip("/") + "/data/"
    local = str(config.data_dir) + "/"
    try:
        subprocess.run(command + [remote, local], check=True, capture_output=True, text=True, timeout=120)
        if run_date and _restore_sent_outbox_tasks(run_date, sent_before_sync):
            append_log("server sync pull merged local sent desktop outbox state")
        append_log(f"server sync pull ok: {remote} -> {local}")
    except Exception as exc:
        append_log(f"server sync pull error: {exc}")


def sync_today_tasks_to_server(run_date: date) -> None:
    command = _server_sync_command()
    if not command:
        return
    local_dir = config.data_dir / "tasks" / run_date.isoformat()
    if not local_dir.exists():
        return
    remote = config.server_sync_target.rstrip("/") + f"/data/tasks/{run_date.isoformat()}/"
    try:
        subprocess.run(command + [str(local_dir) + "/", remote], check=True, capture_output=True, text=True, timeout=120)
        append_log(f"server sync push ok: {local_dir}/ -> {remote}")
    except Exception as exc:
        append_log(f"server sync push error: {exc}")


def write_receive_channel_health(ok: bool, source: str, detail: str, run_date: date | None = None) -> None:
    path = config.data_dir / "runtime" / "wecom_archive_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": ok,
        "source": source,
        "detail": detail,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "date": run_date.isoformat() if run_date else "",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def receive_channel_health_ok(run_date: date, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    path = config.data_dir / "runtime" / "wecom_archive_health.json"
    if not path.exists():
        return False, "没有找到官方收图健康状态"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not payload.get("ok"):
            return False, str(payload.get("detail") or "官方收图最近一次失败")
        checked_at_raw = str(payload.get("checked_at") or "")
        checked_at = datetime.fromisoformat(checked_at_raw)
        if (now - checked_at).total_seconds() > RECEIVE_HEALTH_MAX_AGE_SECONDS:
            return False, f"官方收图健康状态过期：{checked_at_raw}"
        health_date = str(payload.get("date") or "")
        if health_date and health_date != run_date.isoformat():
            return False, f"官方收图健康状态不是今天：{health_date}"
        return True, str(payload.get("detail") or "官方收图最近成功")
    except Exception as exc:
        return False, f"官方收图健康状态不可读：{exc}"


def keep_awake() -> subprocess.Popen | None:
    try:
        return subprocess.Popen(["caffeinate", "-dimsu"])
    except FileNotFoundError:
        append_log("caffeinate unavailable; continuing without sleep guard")
        return None


def should_allow_daily_ask(
    now: datetime,
    ask_at: day_time,
    started_at: datetime,
    catch_up_today: bool = False,
) -> bool:
    if is_supplier_rest_day(now.date()):
        return False
    if now.time() < ask_at:
        return False
    if catch_up_today:
        return True
    if now.date() == started_at.date() and started_at.time() > ask_at:
        return False
    return True


def run_daily_plan(
    store: Store,
    run_date: date,
    state: dict,
    dry_run: bool,
    send_internal: bool,
    auto_send_desktop: bool,
    desktop_send_limit: int,
    auto_capture_supplier_desktop: bool,
    auto_capture_selector_desktop: bool,
    auto_poll_wecom_archive: bool,
    sync_server_data: bool,
    allow_ask: bool,
) -> None:
    receive_channel_ok = False
    receive_health_detail = "官方收图通道未完成健康检查"
    remote_catchup_result: dict = {}
    if sync_server_data:
        sync_server_data_to_local(run_date)
        receive_channel_ok, receive_health_detail = receive_channel_health_ok(run_date)
        if not receive_channel_ok:
            append_log(f"receive channel unhealthy after server sync: {receive_health_detail}")
            if config.runtime_mode in {"official", "hybrid"}:
                remote_catchup_result = run_remote_archive_poll_once(run_date)
                append_log(f"server archive catch-up: {remote_catchup_result}")
                sync_server_data_to_local(run_date)
                receive_channel_ok, receive_health_detail = receive_channel_health_ok(run_date)
                append_log(f"receive channel after catch-up: ok={receive_channel_ok} detail={receive_health_detail}")

    if auto_poll_wecom_archive and config.runtime_mode in {"official", "hybrid"}:
        client = WeComClient(config)
        if client.message_archive_configured():
            try:
                archive_result = poll_message_archive_into_inbox(config, store, config.data_dir, run_date)
                receive_channel_ok = True
                receive_health_detail = "官方收图本轮成功"
                write_receive_channel_health(True, "local_wecom_archive", "官方收图本轮成功", run_date)
                if archive_result.checked or archive_result.queued_events or archive_result.errors:
                    append_log(f"wecom archive receive: {archive_result.__dict__}")
            except Exception as exc:
                receive_channel_ok = False
                receive_health_detail = f"官方收图失败：{exc}"
                write_receive_channel_health(False, "local_wecom_archive", receive_health_detail, run_date)
                append_log(f"wecom archive receive error: {exc}")
        else:
            receive_health_detail = "会话内容存档参数不完整"
            write_receive_channel_health(False, "local_wecom_archive", receive_health_detail, run_date)
            append_log("wecom archive receive skipped: message archive config incomplete")
    if auto_capture_supplier_desktop and config.runtime_mode == "desktop":
        receive_channel_ok = True
    if should_stop_for_receive_channel(config.runtime_mode, receive_channel_ok):
        detail = receive_health_detail
        diagnostics = diagnose_receive_channel(config, run_date, detail, project_root=ROOT)
        failure_path = record_receive_channel_failure(config, run_date, detail, diagnostics)
        append_log(f"fatal receive channel unavailable: {detail}; failure={failure_path}")
        alert_result = send_receive_channel_failure_alert(config, detail, diagnostics=diagnostics)
        append_log(f"alert email: {alert_result.__dict__}")
        raise SystemExit(
            f"官方收图通道不可用，agent 已停止。原因：{detail}。"
            "修复 SDK/服务器收图后，再重新双击启动。"
        )

    if receive_recovery_required(config, run_date):
        append_log("receive recovery required: processing catch-up inbox events before workflow")
        recovery_inbox_result = process_pending_inbox_events(
            store,
            config.data_dir,
            root_dir=ROOT,
            report_finalize_time=config.report_finalize_time,
        )
        reconciliation = reconcile_receive_recovery(config, store, run_date)
        catchup_summary = {
            "remote_archive_catchup": remote_catchup_result,
            "processed": recovery_inbox_result.processed,
            "failed": recovery_inbox_result.failed,
            "created_product_ids": recovery_inbox_result.created_product_ids,
            "reply_product_ids": recovery_inbox_result.reply_product_ids,
            "errors": recovery_inbox_result.errors,
        }
        append_log(
            "receive recovery reconciliation: "
            f"ok={reconciliation.ok} detail={reconciliation.detail} counts={reconciliation.counts}"
        )
        if not reconciliation.ok:
            diagnostics = diagnose_receive_channel(config, run_date, reconciliation.detail, project_root=ROOT)
            failure_path = record_receive_channel_failure(config, run_date, reconciliation.detail, diagnostics)
            alert_result = send_receive_channel_failure_alert(config, reconciliation.detail, diagnostics=diagnostics)
            append_log(f"receive recovery blocked: failure={failure_path} alert={alert_result.__dict__}")
            raise SystemExit(
                f"官方收图恢复前对账未通过，agent 已停止。原因：{reconciliation.detail}。"
                "请处理 pending/failed/unknown 收件事件后再重新启动。"
            )
        recovery_path = record_receive_channel_recovery(
            config,
            run_date,
            receive_health_detail,
            catchup_summary,
            reconciliation,
        )
        append_log(f"receive recovery completed: {recovery_path}")

    effective_allow_ask = allow_ask and receive_channel_ok
    if allow_ask and not receive_channel_ok:
        append_log("supplier asks blocked: no healthy receive channel")
    allow_supplier_reminders = receive_channel_ok
    if not allow_supplier_reminders:
        append_log("supplier reminders blocked: no healthy receive channel")

    inbox_result = process_pending_inbox_events(
        store,
        config.data_dir,
        root_dir=ROOT,
        report_finalize_time=config.report_finalize_time,
    )
    if inbox_result.processed or inbox_result.failed:
        append_log(
            "inbox events: "
            f"processed={inbox_result.processed} failed={inbox_result.failed} "
            f"products={inbox_result.created_product_ids} errors={inbox_result.errors}"
        )

    result = WorkflowEngine(config, store).run_once(
        run_date,
        send_internal=send_internal and not dry_run,
        allow_ask=effective_allow_ask,
        allow_supplier_reminders=allow_supplier_reminders,
    )
    append_log(f"workflow run: {result.workflow_path} actions={','.join(result.actions)} summary={result.summary}")
    if dry_run:
        append_log("dry-run mode: official internal file sending disabled")

    if auto_capture_supplier_desktop and not dry_run:
        receive_result = capture_waiting_supplier_images(config.data_dir, run_date)
        if receive_result.checked or receive_result.captured or receive_result.errors:
            append_log(f"desktop receive: {receive_result.__dict__}")
        if receive_result.queued_events:
            inbox_result = process_pending_inbox_events(
                store,
                config.data_dir,
                root_dir=ROOT,
                report_finalize_time=config.report_finalize_time,
            )
            append_log(
                "desktop receive inbox events: "
                f"processed={inbox_result.processed} failed={inbox_result.failed} "
                f"products={inbox_result.created_product_ids} errors={inbox_result.errors}"
            )
            result = WorkflowEngine(config, store).run_once(
                run_date,
                send_internal=send_internal and not dry_run,
                allow_ask=effective_allow_ask,
                allow_supplier_reminders=allow_supplier_reminders,
            )
            append_log(f"workflow after desktop receive: {result.workflow_path} actions={','.join(result.actions)} summary={result.summary}")

    if auto_capture_selector_desktop and not dry_run:
        selection_result = capture_selector_selections(config.data_dir, run_date)
        if selection_result.checked or selection_result.selection_products or selection_result.errors:
            append_log(f"desktop selector receive: {selection_result.__dict__}")
        if selection_result.selection_products:
            result = WorkflowEngine(config, store).run_once(
                run_date,
                send_internal=send_internal and not dry_run,
                allow_ask=effective_allow_ask,
                allow_supplier_reminders=allow_supplier_reminders,
            )
            append_log(
                "workflow after selector receive: "
                f"{result.workflow_path} actions={','.join(result.actions)} summary={result.summary}"
            )

    if auto_send_desktop and not dry_run:
        outbox_path = config.data_dir / "tasks" / run_date.isoformat() / "desktop_outbox.json"
        send_result = send_pending_desktop_outbox(
            outbox_path,
            limit=desktop_send_limit,
            excluded_kinds=supplier_facing_outbox_exclusions(run_date, effective_allow_ask, allow_supplier_reminders),
        )
        append_log(f"desktop send: {send_result.__dict__}")
        if send_result.sent_task_ids:
            result_after_send = WorkflowEngine(config, store).run_once(
                run_date,
                send_internal=send_internal and not dry_run,
                allow_ask=effective_allow_ask,
                allow_supplier_reminders=allow_supplier_reminders,
            )
            append_log(
                "workflow after desktop send: "
                f"{result_after_send.workflow_path} actions={','.join(result_after_send.actions)} "
                f"summary={result_after_send.summary}"
            )
            if sync_server_data:
                sync_today_tasks_to_server(run_date)

    state[f"last_run:{run_date.isoformat()}"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)


def supplier_facing_outbox_exclusions(run_date: date, allow_ask: bool, allow_supplier_reminders: bool = True) -> list[str] | None:
    excluded = set()
    if not allow_ask:
        excluded.add("ask_supplier")
    if not allow_supplier_reminders:
        excluded.add("remind_supplier")
    if is_supplier_rest_day(run_date):
        excluded.update({"ask_supplier", "remind_supplier", "request_sample"})
    return sorted(excluded) or None


def main() -> None:
    parser = argparse.ArgumentParser(description="每日选款助手常驻 runner")
    parser.add_argument("--ask-at", default="09:00", help="每天生成询问任务的时间，格式 HH:MM")
    parser.add_argument("--poll-seconds", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-internal", action="store_true", help="用企业微信官方应用接口发送报表/运营表给角色表中的人员；桌面模式可不启用")
    parser.add_argument("--auto-send-desktop", action="store_true", help="自动消费 desktop_outbox 文本任务并通过企业微信桌面端发送")
    parser.add_argument("--desktop-send-limit", type=int, default=10, help="每轮最多自动发送多少条桌面队列任务")
    parser.add_argument(
        "--catch-up-ask-today",
        action="store_true",
        help="若启动时已经过 ask-at，也补发当天问款；默认不补发，避免真实供应商被当天立即打扰",
    )
    parser.add_argument("--no-auto-poll-wecom-archive", action="store_true", help="关闭企业微信会话内容存档官方拉取")
    parser.add_argument("--no-auto-capture-desktop", action="store_true", help="关闭全部企业微信桌面端会话截图识别")
    parser.add_argument("--no-auto-capture-supplier-desktop", action="store_true", help="关闭企业微信桌面端供应商收图，保留官方会话存档收图")
    parser.add_argument("--no-auto-capture-selector-desktop", action="store_true", help="关闭企业微信桌面端选款人回传识别")
    parser.add_argument("--sync-server-data", action="store_true", help="每轮先从服务器同步 data，桌面发送后把当天任务状态推回服务器")
    parser.add_argument("--no-caffeinate", action="store_true")
    args = parser.parse_args()

    hour, minute = [int(part) for part in args.ask_at.split(":", 1)]
    ask_at = day_time(hour, minute)
    started_at = datetime.now()
    catchup_skip_logged = False
    auto_capture_desktop = not args.no_auto_capture_desktop

    store = Store(config.db_path)
    store.init()
    state = load_state()
    caffeinate_proc = None if args.no_caffeinate else keep_awake()
    append_log(
        "daily operator agent started "
        f"ask_at={args.ask_at} poll={args.poll_seconds}s catch_up_today={args.catch_up_ask_today}"
    )
    if args.auto_send_desktop:
        desktop_check = check_desktop_automation()
        append_log(f"desktop automation check: {desktop_check.__dict__}")

    try:
        while True:
            now = datetime.now()
            allow_ask = should_allow_daily_ask(now, ask_at, started_at, args.catch_up_ask_today)
            if (
                not allow_ask
                and not catchup_skip_logged
                and not is_supplier_rest_day(now.date())
                and now.date() == started_at.date()
                and started_at.time() > ask_at
                and now.time() >= ask_at
            ):
                append_log(
                    f"ask catch-up disabled: process started after {args.ask_at}; "
                    "first supplier ask will run on the next eligible day"
                )
                catchup_skip_logged = True
            try:
                run_daily_plan(
                    store,
                    now.date(),
                    state,
                    dry_run=args.dry_run,
                    send_internal=args.send_internal,
                    auto_send_desktop=args.auto_send_desktop,
                    desktop_send_limit=args.desktop_send_limit,
                    auto_capture_supplier_desktop=auto_capture_desktop and not args.no_auto_capture_supplier_desktop,
                    auto_capture_selector_desktop=auto_capture_desktop and not args.no_auto_capture_selector_desktop,
                    auto_poll_wecom_archive=not args.no_auto_poll_wecom_archive,
                    sync_server_data=args.sync_server_data,
                    allow_ask=allow_ask,
                )
            except Exception as exc:
                append_log(f"runner iteration error: {exc}")
            time.sleep(args.poll_seconds)
    finally:
        if caffeinate_proc:
            caffeinate_proc.terminate()
            append_log("caffeinate stopped")


if __name__ == "__main__":
    main()
