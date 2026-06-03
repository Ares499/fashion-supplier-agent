from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Sequence

from .desktop_outbox import DesktopOutboxTask, load_outbox, mark_outbox_failed_attempt, mark_outbox_sent, pending_outbox_tasks


@dataclass
class DesktopSendResult:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    sent_task_ids: List[str] = field(default_factory=list)
    sent_titles: dict = field(default_factory=dict)


@dataclass
class DesktopAutomationCheck:
    ok: bool
    wecom_running: bool
    system_events_ok: bool
    detail: str


ACCESSIBILITY_HELP = (
    "macOS 未允许当前启动进程控制键盘。请打开 系统设置 -> 隐私与安全性 -> 辅助功能，"
    "允许“终端”；如果列表里出现 osascript 或 Script Editor，也一起允许。授权后重新双击启动文件。"
)


def _screen_is_locked() -> bool:
    try:
        completed = subprocess.run(
            ["ioreg", "-n", "Root", "-d1"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    output = completed.stdout
    # IOConsoleLocked is the direct console-lock signal. Some macOS sessions keep
    # a stale CGSessionScreenIsLocked flag even when the user is back on console.
    return '"IOConsoleLocked" = Yes' in output


def check_desktop_automation() -> DesktopAutomationCheck:
    if _screen_is_locked():
        return DesktopAutomationCheck(
            False,
            False,
            False,
            "macOS 报告控制台已锁定；企业微信桌面端自动发送需要可操作桌面会话。",
        )

    running_script = '''
tell application "System Events"
  set hasWeCom to exists process "企业微信"
end tell
return hasWeCom
'''
    try:
        completed = subprocess.run(["osascript", "-e", running_script], check=True, capture_output=True, text=True, timeout=10)
        running = completed.stdout.strip().lower() == "true"
        if not running:
            return DesktopAutomationCheck(False, False, True, "企业微信未运行，请先登录并保持企业微信打开")
    except Exception as exc:
        return DesktopAutomationCheck(False, False, False, f"无法读取企业微信桌面状态：{exc}。{ACCESSIBILITY_HELP}")

    probe_script = '''
tell application "企业微信" to activate
delay 0.2
tell application "System Events"
  tell process "企业微信"
    if (count of windows) is 0 then error "企业微信没有可访问窗口；请打开企业微信主窗口，确认窗口没有被关闭、隐藏或放在其他桌面空间。" number 10002
    key code 53
  end tell
end tell
return "ok"
'''
    try:
        subprocess.run(["osascript", "-e", probe_script], check=True, capture_output=True, text=True, timeout=10)
        return DesktopAutomationCheck(True, True, True, "企业微信已运行，且 macOS 已允许桌面按键自动化")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        raw = stderr or stdout or str(exc)
        return DesktopAutomationCheck(False, True, False, f"企业微信已运行，但桌面按键自动化未授权：{raw}。{ACCESSIBILITY_HELP}")
    except Exception as exc:
        return DesktopAutomationCheck(False, True, False, f"企业微信已运行，但桌面按键自动化不可用：{exc}。{ACCESSIBILITY_HELP}")


def open_accessibility_settings() -> None:
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
        check=False,
    )


def send_pending_desktop_outbox(
    outbox_path: Path,
    limit: int = 1,
    kinds: Sequence[str] | None = None,
    excluded_kinds: Sequence[str] | None = None,
    dry_run: bool = False,
) -> DesktopSendResult:
    check = check_desktop_automation()
    if not dry_run and not check.ok:
        return DesktopSendResult(errors=[check.detail])

    tasks = pending_outbox_tasks(load_outbox(outbox_path))
    if kinds:
        wanted = set(kinds)
        tasks = [task for task in tasks if task.kind in wanted]
    if excluded_kinds:
        excluded = set(excluded_kinds)
        tasks = [task for task in tasks if task.kind not in excluded]
    if limit > 0:
        tasks = tasks[:limit]

    result = DesktopSendResult()
    for task in tasks:
        result.attempted += 1
        if dry_run:
            result.skipped += 1
            continue
        try:
            actual_title = send_wecom_task(task)
            result.sent += 1
            result.sent_task_ids.append(task.task_id)
            result.sent_titles[task.task_id] = actual_title
        except Exception as exc:
            error = str(exc)
            mark_outbox_failed_attempt(outbox_path, task.task_id, error)
            result.errors.append(f"{task.task_id}: {error}")

    if result.sent_task_ids:
        mark_outbox_sent(
            outbox_path,
            result.sent_task_ids,
            datetime.now(),
            metadata_by_task_id={
                task_id: {"actual_conversation_title": title}
                for task_id, title in result.sent_titles.items()
            },
        )
    return result


def send_wecom_task(task: DesktopOutboxTask) -> str:
    script = _apple_script(task.search_text or task.conversation_name, task.conversation_name, task.message, task.attachments)
    try:
        completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        detail = stderr or stdout or str(exc)
        raise RuntimeError(detail) from exc
    return completed.stdout.strip() or task.conversation_name


def _apple_script(search_text: str, expected_title: str, message: str, attachments: Sequence[str]) -> str:
    search_literal = json.dumps(search_text, ensure_ascii=False)
    expected_title_literal = json.dumps(expected_title, ensure_ascii=False)
    message_literal = json.dumps(message, ensure_ascii=False)
    attachment_lines = []
    for raw_path in attachments:
        path = str(Path(raw_path).expanduser().resolve())
        attachment_lines.append(
            f'''
    set the clipboard to (POSIX file {json.dumps(path, ensure_ascii=False)})
    keystroke "v" using {{command down}}
    delay 0.8
    key code 36
    delay 0.8
'''
        )
    attachment_script = "".join(attachment_lines)
    return f'''
on chatInnerSplit()
  tell application "System Events"
    tell process "企业微信"
      return splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    end tell
  end tell
end chatInnerSplit

on openVisibleConversation(expectedTitle)
  tell application "System Events"
    tell process "企业微信"
      try
        set convTable to table 1 of scroll area 1 of splitter group 1 of splitter group 1 of window 1
        repeat with conversationRow in rows of convTable
          try
            set rowTitle to value of static text 1 of UI element 1 of conversationRow
            if rowTitle is expectedTitle then
              select conversationRow
              delay 0.5
              return true
            end if
          end try
        end repeat
      end try
    end tell
  end tell
  return false
end openVisibleConversation

on focusComposer()
  tell application "System Events"
    tell process "企业微信"
      set composer to text area 1 of scroll area 2 of my chatInnerSplit()
      click composer
      delay 0.2
      set composerDraft to ""
      try
        set composerDraft to value of composer as text
      end try
      if composerDraft is not "" then
        error "当前会话输入框已有未发送内容，为避免误发已停止。" number 10004
      end if
      return true
    end tell
  end tell
end focusComposer

on sendComposerText(expectedText)
  tell application "System Events"
    tell process "企业微信"
      set composer to text area 1 of scroll area 2 of my chatInnerSplit()
      set value of composer to expectedText
      delay 0.2
      key code 36
      delay 0.8
      set remainingDraft to ""
      try
        set remainingDraft to value of composer as text
      end try
      if remainingDraft is expectedText then
        key code 36 using {{command down}}
        delay 0.8
      end if
      set remainingDraft to ""
      try
        set remainingDraft to value of composer as text
      end try
      if remainingDraft is expectedText then
        set value of composer to ""
        error "输入框内容未成功发送，已清空草稿并停止标记为已发送。" number 10005
      end if
      return true
    end tell
  end tell
end sendComposerText

on messageVisibleCount(expectedText)
  tell application "System Events"
    tell process "企业微信"
      set matchedCount to 0
      try
        set messageTable to table 1 of scroll area 1 of my chatInnerSplit()
        repeat with messageRow in rows of messageTable
          try
            set rowContents to entire contents of messageRow
            repeat with itemRef in rowContents
              try
                set itemValue to value of itemRef as text
                if itemValue is expectedText then set matchedCount to matchedCount + 1
              end try
            end repeat
          end try
        end repeat
      end try
      return matchedCount
    end tell
  end tell
end messageVisibleCount

tell application "企业微信" to activate
delay 0.8
tell application "System Events"
  tell process "企业微信"
    set expectedTitle to {expected_title_literal}
    set expectedMessage to {message_literal}
    set openedExactVisibleConversation to my openVisibleConversation(expectedTitle)
    set currentTitle to ""
    try
      set currentTitle to value of static text 1 of splitter group 1 of splitter group 1 of window 1
    end try
    if openedExactVisibleConversation is false or ((currentTitle as text) is not equal to (expectedTitle as text)) then
      keystroke "f" using {{command down}}
      delay 0.3
      set the clipboard to {search_literal}
      keystroke "v" using {{command down}}
      delay 0.8
      key code 36
      delay 0.8
    end if
    set currentTitle to ""
    try
      set currentTitle to value of static text 1 of splitter group 1 of splitter group 1 of window 1
    end try
    if (currentTitle as text) is not equal to (expectedTitle as text) then
      error "当前会话是“" & currentTitle & "”，不是目标会话“" & expectedTitle & "”，已停止发送。" number 10001
    end if
    set beforeMessageCount to my messageVisibleCount(expectedMessage)
    my focusComposer()
    my sendComposerText(expectedMessage)
    set afterMessageCount to my messageVisibleCount(expectedMessage)
    if afterMessageCount is less than or equal to beforeMessageCount then
      error "未在目标会话底部检测到新发送的消息气泡，已停止标记为已发送。" number 10003
    end if
{attachment_script}
  end tell
end tell
return currentTitle
'''
