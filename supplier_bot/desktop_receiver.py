from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from PIL import Image

from .selection import detect_selections, write_selection_json
from .desktop_sender import check_desktop_automation
from .images import phash, similar_hashes
from .inbox_events import InboxEvent, queue_inbox_event
from .workflow_state import STATUS_REPORT_SENT, STATUS_WAITING_IMAGES, load_daily_workflow


@dataclass
class DesktopCapture:
    supplier_id: str
    supplier_name: str
    image_paths: List[str] = field(default_factory=list)
    skipped_existing: int = 0
    pages_scanned: int = 0
    stop_reason: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class DesktopReceiveResult:
    checked: int = 0
    captured: int = 0
    queued_events: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    captures: List[DesktopCapture] = field(default_factory=list)
    selection_products: List[str] = field(default_factory=list)


@dataclass
class DesktopCaptureCheck:
    ok: bool
    detail: str


SCREEN_CAPTURE_HELP = (
    "macOS 未允许当前启动进程截屏。请打开 系统设置 -> 隐私与安全性 -> 屏幕与系统音频录制"
    "（旧版本叫“屏幕录制”），允许“终端”。授权后重新双击启动文件。"
)


def check_desktop_capture() -> DesktopCaptureCheck:
    target = Path(tempfile.gettempdir()) / f"wecom_capture_check_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    try:
        subprocess.run(["screencapture", "-x", str(target)], check=True, capture_output=True, text=True, timeout=10)
        with Image.open(target) as image:
            if image.width < 100 or image.height < 100:
                return DesktopCaptureCheck(False, f"截屏结果异常：{image.width}x{image.height}。{SCREEN_CAPTURE_HELP}")
        target.unlink(missing_ok=True)
        return DesktopCaptureCheck(True, "macOS 已允许屏幕录制，桌面收图可用")
    except subprocess.CalledProcessError as exc:
        raw = (exc.stderr or exc.stdout or str(exc)).strip()
        return DesktopCaptureCheck(False, f"桌面截屏未授权或失败：{raw}。{SCREEN_CAPTURE_HELP}")
    except Exception as exc:
        return DesktopCaptureCheck(False, f"桌面截屏不可用：{exc}。{SCREEN_CAPTURE_HELP}")


def open_screen_capture_settings() -> None:
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
        check=False,
    )


def capture_waiting_supplier_images(data_dir: Path, run_date: date) -> DesktopReceiveResult:
    workflow_path = data_dir / "tasks" / run_date.isoformat() / "daily_workflow.json"
    workflow = load_daily_workflow(workflow_path)
    result = DesktopReceiveResult()
    if not workflow:
        return result

    waiting = [supplier for supplier in workflow.suppliers if supplier.status == STATUS_WAITING_IMAGES]
    if not waiting:
        return result

    check = check_desktop_automation()
    if not check.ok:
        result.errors.append(check.detail)
        return result
    capture_check = check_desktop_capture()
    capture_allowed = capture_check.ok
    if not capture_allowed:
        result.errors.append(capture_check.detail)

    for supplier in waiting:
        result.checked += 1
        capture = capture_supplier_conversation_images(
            data_dir=data_dir,
            run_date=run_date,
            supplier_id=supplier.supplier_id,
            supplier_name=supplier.supplier_name,
            search_text=supplier.search_text or supplier.supplier_name,
        )
        result.captures.append(capture)
        if capture.errors:
            result.errors.extend(f"{supplier.supplier_id}: {error}" for error in capture.errors)
        if capture.image_paths:
            event_id = f"desktop-{run_date.isoformat()}-{supplier.supplier_id}-{datetime.now().strftime('%H%M%S')}"
            event_path = queue_inbox_event(
                data_dir,
                InboxEvent(
                    event_id=event_id,
                    supplier_id=supplier.supplier_id,
                    received_at=datetime.now().isoformat(timespec="seconds"),
                    image_paths=capture.image_paths,
                    source="desktop_capture",
                ),
            )
            result.captured += len(capture.image_paths)
            result.queued_events.append(str(event_path))
    return result


def capture_selector_selections(data_dir: Path, run_date: date) -> DesktopReceiveResult:
    workflow_path = data_dir / "tasks" / run_date.isoformat() / "daily_workflow.json"
    workflow = load_daily_workflow(workflow_path)
    result = DesktopReceiveResult()
    if not workflow or not workflow.selectors:
        return result
    if not any(supplier.status == STATUS_REPORT_SENT for supplier in workflow.suppliers):
        return result

    report_dir = data_dir / "reports" / run_date.isoformat()
    manifest_path = report_dir / "manifest.json"
    if not manifest_path.exists():
        return result

    check = check_desktop_automation()
    if not check.ok:
        result.errors.append(check.detail)
        return result
    capture_check = check_desktop_capture()
    capture_allowed = capture_check.ok
    if not capture_allowed:
        result.errors.append(capture_check.detail)

    existing_selection_path = report_dir / "selection.json"
    existing = []
    existing_ids = set()
    if existing_selection_path.exists():
        existing = json.loads(existing_selection_path.read_text(encoding="utf-8"))
        existing_ids = {item.get("product_id") for item in existing}

    for selector in workflow.selectors:
        result.checked += 1
        if not capture_allowed:
            continue
        capture = capture_conversation_image_crops(
            data_dir=data_dir,
            run_date=run_date,
            owner_id=selector.contact_id,
            owner_name=selector.display_name,
            search_text=selector.search_text or selector.display_name,
            output_kind="selector_captures",
            scan_pages=12,
            stop_text=_selector_report_stop_text(),
        )
        result.captures.append(capture)
        if capture.errors:
            result.errors.extend(f"{selector.contact_id}: {error}" for error in capture.errors)
        if capture.image_paths and capture.stop_reason != "found_daily_ask_message":
            _forget_capture_paths(data_dir / "selector_captures" / run_date.isoformat() / selector.contact_id, capture.image_paths)
            result.errors.append(
                f"{selector.contact_id}: 未找到今天报表消息边界，已丢弃本次桌面选款截图，避免误用历史图片。"
            )
            continue
        for image_path in capture.image_paths:
            selections = detect_selections(manifest_path, Path(image_path), min_confidence=0.12)
            for selection in selections:
                if selection.product_id in existing_ids:
                    continue
                payload = selection.__dict__ | {
                    "source": "desktop_selector_capture",
                    "selector_id": selector.contact_id,
                    "selector_name": selector.display_name,
                    "screenshot_path": image_path,
                    "selected_at": datetime.now().isoformat(timespec="seconds"),
                    "capture_mode": "after_report_message_boundary",
                    "capture_pages_scanned": capture.pages_scanned,
                    "capture_stop_reason": capture.stop_reason,
                }
                existing.append(payload)
                existing_ids.add(selection.product_id)
                result.selection_products.append(selection.product_id)

    if result.selection_products:
        write_selection_json([_selection_from_dict(item) for item in existing], existing_selection_path)
        # Preserve the extra audit fields that Selection does not carry.
        existing_selection_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def capture_supplier_conversation_images(
    data_dir: Path,
    run_date: date,
    supplier_id: str,
    supplier_name: str,
    search_text: str,
) -> DesktopCapture:
    return capture_conversation_image_crops(
        data_dir=data_dir,
        run_date=run_date,
        owner_id=supplier_id,
        owner_name=supplier_name,
        search_text=search_text,
        output_kind="desktop_captures",
        stop_text=_daily_ask_stop_text(run_date),
    )


def capture_conversation_image_crops(
    data_dir: Path,
    run_date: date,
    owner_id: str,
    owner_name: str,
    search_text: str,
    output_kind: str,
    scan_pages: int = 50,
    stop_text: str = "",
) -> DesktopCapture:
    capture = DesktopCapture(supplier_id=owner_id, supplier_name=owner_name)
    try:
        _open_conversation(search_text)
        output_dir = data_dir / output_kind / run_date.isoformat() / owner_id
        output_dir.mkdir(parents=True, exist_ok=True)
        seen_path = output_dir / "seen.json"
        seen = _load_seen(seen_path)
        _capture_visible_images_bottom_up(
            output_dir=output_dir,
            owner_id=owner_id,
            scan_pages=scan_pages,
            seen=seen,
            capture=capture,
            stop_text=stop_text,
        )
        _write_seen(seen_path, seen)
    except Exception as exc:
        capture.errors.append(str(exc))
    return capture


def _capture_visible_images_bottom_up(
    output_dir: Path,
    owner_id: str,
    scan_pages: int,
    seen: List[dict],
    capture: DesktopCapture,
    stop_text: str = "",
) -> None:
    _scroll_conversation_to_bottom()
    previous_fingerprint = ""
    for page_index in range(max(1, scan_pages)):
        screenshot = _screenshot_wecom_window()
        audit_path = output_dir / f"window_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{page_index}.png"
        Path(screenshot).replace(audit_path)
        capture.pages_scanned += 1
        stop_relation = _stop_text_relation(stop_text) if stop_text else {"relation": "missing", "local_bottom": None}
        if stop_relation["relation"] == "below":
            capture.stop_reason = "passed_daily_ask_message"
            break
        min_top = stop_relation["local_bottom"] if stop_relation["relation"] == "visible" else None
        _save_new_crops(
            output_dir,
            owner_id,
            page_index,
            extract_incoming_image_crops(audit_path, min_top=min_top),
            seen,
            capture,
        )
        if stop_relation["relation"] == "visible":
            capture.stop_reason = "found_daily_ask_message"
            break
        if _chat_scroll_value() <= 0.001:
            capture.stop_reason = "top_reached"
            break
        fingerprint = _screen_fingerprint(audit_path)
        if fingerprint and fingerprint == previous_fingerprint:
            capture.stop_reason = "screen_unchanged"
            break
        previous_fingerprint = fingerprint
        _scroll_conversation_page_up()
    if not capture.stop_reason:
        capture.stop_reason = "max_pages_reached"


def _capture_images_after_stop_text(
    output_dir: Path,
    owner_id: str,
    scan_pages: int,
    stop_text: str,
    seen: List[dict],
    capture: DesktopCapture,
) -> None:
    _scroll_conversation_to_top()
    previous_fingerprint = ""
    found_boundary = False
    for page_index in range(max(1, scan_pages)):
        screenshot = _screenshot_wecom_window()
        audit_path = output_dir / f"window_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{page_index}.png"
        Path(screenshot).replace(audit_path)
        capture.pages_scanned += 1

        stop_bottom = _visible_stop_text_bottom(stop_text)
        if found_boundary or stop_bottom is not None:
            min_top = stop_bottom if stop_bottom is not None and not found_boundary else None
            crops = extract_incoming_image_crops(audit_path, min_top=min_top)
            _save_new_crops(output_dir, owner_id, page_index, crops, seen, capture)
            found_boundary = True

        if found_boundary and _chat_scroll_value() >= 0.999:
            capture.stop_reason = "bottom_reached_after_daily_ask"
            break

        fingerprint = _screen_fingerprint(audit_path)
        if fingerprint and fingerprint == previous_fingerprint:
            capture.stop_reason = "screen_unchanged_after_daily_ask" if found_boundary else "screen_unchanged_before_daily_ask"
            break
        previous_fingerprint = fingerprint
        _scroll_conversation_page_down()

    if not capture.stop_reason:
        capture.stop_reason = "max_pages_reached_after_daily_ask" if found_boundary else "daily_ask_message_not_found"


def _save_new_crops(
    output_dir: Path,
    owner_id: str,
    page_index: int,
    crops: Sequence[Image.Image],
    seen: List[dict],
    capture: DesktopCapture,
) -> None:
    for idx, crop in enumerate(crops, 1):
        temp_path = output_dir / f"candidate_{datetime.now().strftime('%H%M%S')}_{page_index}_{idx}.jpg"
        crop.save(temp_path, quality=94)
        image_hash = phash(str(temp_path))
        if _already_seen(image_hash, seen):
            temp_path.unlink(missing_ok=True)
            capture.skipped_existing += 1
            continue
        final_path = output_dir / f"{owner_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{page_index}_{idx}.jpg"
        temp_path.rename(final_path)
        seen.append(
            {
                "hash": image_hash,
                "path": str(final_path),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        capture.image_paths.append(str(final_path))


def extract_incoming_image_crops(window_screenshot: Path | Image.Image, min_top: int | None = None) -> List[Image.Image]:
    with Image.open(window_screenshot) if isinstance(window_screenshot, Path) else _image_context(window_screenshot) as raw:
        image = raw.convert("RGB")
        width, height = image.size
        chat_left = int(width * 0.33)
        chat_right = min(width - 18, chat_left + max(220, int(width * 0.42)))
        chat_top = int(height * 0.09)
        chat_bottom = int(height * 0.83)
        region = image.crop((chat_left, chat_top, chat_right, chat_bottom))
        boxes = _detect_photo_boxes(region)
        crops = []
        for box in boxes:
            left, top, right, bottom = box
            edge_margin = 8
            if top <= edge_margin or bottom >= region.height - edge_margin:
                continue
            if min_top is not None and chat_top + bottom <= min_top:
                continue
            pad = 4
            crop_box = (
                max(0, left - pad),
                max(0, top - pad),
                min(region.width, right + pad),
                min(region.height, bottom + pad),
            )
            crop = region.crop(crop_box)
            if crop.width >= 35 and crop.height >= 35:
                crops.append(crop)
        return crops


class _image_context:
    def __init__(self, image: Image.Image) -> None:
        self.image = image

    def __enter__(self) -> Image.Image:
        return self.image

    def __exit__(self, *_args) -> None:
        return None


def _open_conversation(search_text: str) -> None:
    search_literal = json.dumps(search_text, ensure_ascii=False)
    script = f'''
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

tell application "企业微信" to activate
delay 0.5
tell application "System Events"
  tell process "企业微信"
    set expectedTitle to {search_literal}
    set openedExactVisibleConversation to my openVisibleConversation(expectedTitle)
    set currentTitle to ""
    try
      set currentTitle to value of static text 1 of splitter group 1 of splitter group 1 of window 1
    end try
    if openedExactVisibleConversation is false or ((currentTitle as text) is not equal to (expectedTitle as text)) then
      keystroke "f" using {{command down}}
      delay 0.2
      set the clipboard to {search_literal}
      keystroke "v" using {{command down}}
      delay 0.5
      key code 36
      delay 0.8
    end if
    set currentTitle to ""
    try
      set currentTitle to value of static text 1 of splitter group 1 of splitter group 1 of window 1
    end try
    if currentTitle is not "" and (currentTitle as text) is not equal to (expectedTitle as text) then
      error "当前会话是“" & currentTitle & "”，不是目标会话“" & expectedTitle & "”，已停止收图。" number 10002
    end if
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=15)


def _scroll_conversation_page_up() -> None:
    script = '''
tell application "企业微信" to activate
delay 0.1
tell application "System Events"
  tell process "企业微信"
    set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    set currentValue to value of scroll bar 1 of chatScroll
    set nextValue to currentValue - 0.12
    if nextValue < 0 then set nextValue to 0
    set value of scroll bar 1 of chatScroll to nextValue
    delay 0.35
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def _scroll_conversation_page_down() -> None:
    script = '''
tell application "企业微信" to activate
delay 0.1
tell application "System Events"
  tell process "企业微信"
    set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    set currentValue to value of scroll bar 1 of chatScroll
    set nextValue to currentValue + 0.04
    if nextValue > 1 then set nextValue to 1
    set value of scroll bar 1 of chatScroll to nextValue
    delay 0.35
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def _scroll_conversation_to_top() -> None:
    script = '''
tell application "企业微信" to activate
delay 0.1
tell application "System Events"
  tell process "企业微信"
    set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    set value of scroll bar 1 of chatScroll to 0
    delay 0.35
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def _scroll_conversation_to_bottom() -> None:
    script = '''
tell application "企业微信" to activate
delay 0.1
tell application "System Events"
  tell process "企业微信"
    set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    set value of scroll bar 1 of chatScroll to 1
    delay 0.35
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def _chat_scroll_value() -> float:
    script = '''
tell application "企业微信" to activate
delay 0.1
tell application "System Events"
  tell process "企业微信"
    set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
    return value of scroll bar 1 of chatScroll
  end tell
end tell
'''
    completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return 0.0


def _visible_conversation_text() -> str:
    script = '''
tell application "System Events"
  tell process "企业微信"
    set outText to ""
    try
      set chatTable to table 1 of scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
      set uiItems to entire contents of chatTable
      repeat with itemRef in uiItems
        try
          set itemClass to class of itemRef as text
          if itemClass is "static text" or itemClass is "text area" or itemClass is "text field" then
            try
              set outText to outText & (value of itemRef as text) & linefeed
            on error
              try
                set outText to outText & (name of itemRef as text) & linefeed
              end try
            end try
          end if
        end try
      end repeat
    end try
    return outText
  end tell
end tell
'''
    completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    return completed.stdout


def _visible_stop_text_bottom(stop_text: str) -> int | None:
    if not stop_text:
        return None
    stop_literal = json.dumps(stop_text, ensure_ascii=False)
    script = f'''
tell application "System Events"
  tell process "企业微信"
    set stopText to {stop_literal}
    try
      set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
      set scrollPos to position of chatScroll
      set scrollSize to size of chatScroll
      set scrollTop to item 2 of scrollPos
      set scrollBottom to scrollTop + (item 2 of scrollSize)
      set chatTable to table 1 of scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
      repeat with itemRef in entire contents of chatTable
        try
          set itemClass to class of itemRef as text
          if itemClass is "static text" or itemClass is "text area" or itemClass is "text field" then
            set itemText to ""
            try
              set itemText to value of itemRef as text
            on error
              try
                set itemText to name of itemRef as text
              end try
            end try
            if itemText contains stopText then
              set itemPos to position of itemRef
              set itemSize to size of itemRef
              set itemTop to item 2 of itemPos
              set itemBottom to itemTop + (item 2 of itemSize)
              if itemBottom >= scrollTop and itemTop <= scrollBottom then
                return itemBottom
              end if
            end if
          end if
        end try
      end repeat
    end try
    return -1
  end tell
end tell
'''
    completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    try:
        global_bottom = int(float(completed.stdout.strip()))
    except ValueError:
        return None
    if global_bottom < 0:
        return None
    _x, window_y, _width, _height = _wecom_window_bounds()
    return max(0, global_bottom - window_y)


def _stop_text_relation(stop_text: str) -> dict:
    if not stop_text:
        return {"relation": "missing", "local_bottom": None}
    stop_literal = json.dumps(stop_text, ensure_ascii=False)
    script = f'''
tell application "System Events"
  tell process "企业微信"
    set stopText to {stop_literal}
    try
      set chatScroll to scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
      set scrollPos to position of chatScroll
      set scrollSize to size of chatScroll
      set scrollTop to item 2 of scrollPos
      set scrollBottom to scrollTop + (item 2 of scrollSize)
      set chatTable to table 1 of scroll area 1 of splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of window 1
      repeat with itemRef in entire contents of chatTable
        try
          set itemClass to class of itemRef as text
          if itemClass is "static text" or itemClass is "text area" or itemClass is "text field" then
            set itemText to ""
            try
              set itemText to value of itemRef as text
            on error
              try
                set itemText to name of itemRef as text
              end try
            end try
            if itemText contains stopText then
              try
                set itemPos to position of itemRef
                set itemSize to size of itemRef
                set itemTop to item 2 of itemPos
                set itemBottom to itemTop + (item 2 of itemSize)
                if itemBottom < scrollTop then
                  return "above|" & itemBottom
                else if itemTop > scrollBottom then
                  return "below|" & itemBottom
                else
                  return "visible|" & itemBottom
                end if
              on error
                return "visible|0"
              end try
            end if
          end if
        end try
      end repeat
    end try
    return "missing|-1"
  end tell
end tell
'''
    completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    raw = completed.stdout.strip()
    relation, _, value = raw.partition("|")
    try:
        global_bottom = int(float(value))
    except ValueError:
        global_bottom = -1
    local_bottom = None
    if global_bottom >= 0:
        _x, window_y, _width, _height = _wecom_window_bounds()
        local_bottom = max(0, global_bottom - window_y)
    if relation not in {"above", "visible", "below", "missing"}:
        relation = "missing"
    return {"relation": relation, "local_bottom": local_bottom}


def _screen_fingerprint(path: Path) -> str:
    try:
        return phash(str(path))
    except Exception:
        return ""


def _daily_ask_stop_text(run_date: date) -> str:
    return "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。"


def _selector_report_stop_text() -> str:
    return "这是今天的选款报表"


def _forget_capture_paths(output_dir: Path, image_paths: Sequence[str]) -> None:
    path_set = {str(Path(path)) for path in image_paths}
    seen_path = output_dir / "seen.json"
    seen = _load_seen(seen_path)
    kept = [item for item in seen if str(Path(item.get("path", ""))) not in path_set]
    _write_seen(seen_path, kept)
    for image_path in path_set:
        Path(image_path).unlink(missing_ok=True)


def _wecom_window_bounds() -> Tuple[int, int, int, int]:
    script = 'tell application "System Events" to tell process "企业微信" to get {position, size} of window 1'
    completed = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    numbers = [int(part.strip()) for part in completed.stdout.replace("\n", ",").split(",") if part.strip()]
    if len(numbers) != 4:
        raise RuntimeError(f"无法读取企业微信窗口位置: {completed.stdout!r}")
    return numbers[0], numbers[1], numbers[2], numbers[3]


def _screenshot_wecom_window() -> Path:
    target = Path(tempfile.gettempdir()) / f"wecom_capture_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    window_id = _wecom_window_id()
    if window_id:
        subprocess.run(["screencapture", "-x", "-o", f"-l{window_id}", str(target)], check=True, timeout=10)
        return target

    x, y, width, height = _wecom_window_bounds()
    subprocess.run(["screencapture", "-x", str(target)], check=True, timeout=10)
    with Image.open(target) as screen:
        max_x = min(screen.width, x + width)
        max_y = min(screen.height, y + height)
        if x < 0 or y < 0 or x >= screen.width or y >= screen.height or max_x <= x or max_y <= y:
            raise RuntimeError(
                f"企业微信窗口坐标超出截屏范围 window=({x},{y},{width},{height}) screen={screen.width}x{screen.height}"
            )
        crop = screen.crop((x, y, max_x, max_y))
        crop.save(target)
    return target


def _wecom_window_id() -> str:
    swift = r'''
import CoreGraphics

let options = CGWindowListOption(arrayLiteral: .optionOnScreenOnly)
guard let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
    exit(1)
}
for window in windows {
    let owner = window[kCGWindowOwnerName as String] as? String ?? ""
    let title = window[kCGWindowName as String] as? String ?? ""
    if owner.contains("企业微信") || owner.contains("WeWork") || title.contains("企业微信") {
        if let number = window[kCGWindowNumber as String] {
            print(number)
            exit(0)
        }
    }
}
exit(1)
'''
    try:
        completed = subprocess.run(["swift", "-e", swift], check=True, capture_output=True, text=True, timeout=8)
    except Exception:
        return ""
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""


def _detect_photo_boxes(region: Image.Image) -> List[Tuple[int, int, int, int]]:
    width, height = region.size
    pixels = region.load()
    mask = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            avg = (r + g + b) // 3
            spread = max(r, g, b) - min(r, g, b)
            if avg < 220 and (spread > 18 or avg < 150):
                mask[y * width + x] = 1

    visited = bytearray(width * height)
    boxes: List[Tuple[int, int, int, int, int]] = []
    for start in range(width * height):
        if not mask[start] or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        count = 0
        while stack:
            idx = stack.pop()
            count += 1
            x = idx % width
            y = idx // width
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    nidx = ny * width + nx
                    if mask[nidx] and not visited[nidx]:
                        visited[nidx] = 1
                        stack.append(nidx)
        box_w = max_x - min_x + 1
        box_h = max_y - min_y + 1
        if _looks_like_photo_box(box_w, box_h, count):
            boxes.append((min_x, min_y, max_x + 1, max_y + 1, count))
    merged = _merge_boxes([(a, b, c, d) for a, b, c, d, _count in boxes])
    return sorted(merged, key=lambda box: (box[1], box[0]))


def _looks_like_photo_box(width: int, height: int, area: int) -> bool:
    if width < 35 or height < 35:
        return False
    if area < 900:
        return False
    ratio = width / max(height, 1)
    if ratio < 0.20 or ratio > 3.8:
        return False
    fill = area / max(width * height, 1)
    return fill > 0.18


def _merge_boxes(boxes: Sequence[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
    merged: List[Tuple[int, int, int, int]] = []
    for box in boxes:
        current = box
        changed = True
        while changed:
            changed = False
            rest = []
            for other in merged:
                if _boxes_close(current, other):
                    current = (
                        min(current[0], other[0]),
                        min(current[1], other[1]),
                        max(current[2], other[2]),
                        max(current[3], other[3]),
                    )
                    changed = True
                else:
                    rest.append(other)
            merged = rest
        merged.append(current)
    return merged


def _boxes_close(left: Tuple[int, int, int, int], right: Tuple[int, int, int, int]) -> bool:
    gap_x = max(right[0] - left[2], left[0] - right[2], 0)
    gap_y = max(right[1] - left[3], left[1] - right[3], 0)
    overlap_x = min(left[2], right[2]) - max(left[0], right[0])
    overlap_y = min(left[3], right[3]) - max(left[1], right[1])
    return (gap_x <= 8 and overlap_y > 0) or (gap_y <= 8 and overlap_x > 0)


def _load_seen(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_seen(path: Path, seen: Sequence[dict]) -> None:
    path.write_text(json.dumps(list(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def _already_seen(image_hash: str, seen: Iterable[dict]) -> bool:
    return any(similar_hashes(image_hash, item.get("hash", ""), threshold=4) for item in seen)


def _selection_from_dict(item: dict):
    from .models import Selection

    return Selection(item["product_id"], float(item.get("confidence", 0)), str(item.get("reason", "")))
