"""
wxchatbot 发送模块 v3 - 搜索导航
流程：
  1. 截图OCR当前页面，看是否已在目标聊天 → 直接发送
  2. 不在 → Ctrl+F 搜索联系人 → 点击搜索结果 → 发送
坐标链：SetProcessDPIAware → 截图(物理像素) → OCR → ClientToScreen+截图坐标 → ljqCtrl.Click
"""
import ctypes
ctypes.windll.user32.SetProcessDPIAware()

import os, sys, time, traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
for _p in [
    str(PROJECT_DIR),
    str(PROJECT_DIR.parent.parent / 'memory'),
]:
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import win32gui, win32con, win32api, win32process
import ctypes as _ctypes
import ljqCtrl

# 延迟导入
_ocr_image = None
_pyperclip = None

def _get_ocr():
    global _ocr_image
    if _ocr_image is None:
        from ocr_utils import ocr_image
        _ocr_image = ocr_image
    return _ocr_image

def _get_clipboard():
    global _pyperclip
    if _pyperclip is None:
        import pyperclip
        _pyperclip = pyperclip
    return _pyperclip

def log_line(msg):
    from datetime import datetime
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


# ═══════════════════════════════════════
# 窗口管理
# ═══════════════════════════════════════

WINDOW_CLASSES = ['WeChatMainWndForPC', 'Qt51514QWindowIcon']

def find_wechat_window():
    """查找微信窗口，返回 hwnd 或 None"""
    def callback(hwnd, results):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if cls in WINDOW_CLASSES and '微信' in title:
            results.append((hwnd, title, cls))

    results = []
    win32gui.EnumWindows(callback, results)
    if results:
        hwnd, title, cls = results[0]
        log_line(f'📱 微信: "{title}" (class={cls})')
        return hwnd

    def callback2(hwnd, results2):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if '微信' in title:
            results2.append((hwnd, title))

    results2 = []
    win32gui.EnumWindows(callback2, results2)
    if results2:
        hwnd, title = results2[0]
        log_line(f'📱 微信(降级): "{title}"')
        return hwnd
    return None


def force_foreground(hwnd):
    """强制窗口到最前（无点击，避免干扰UI）"""
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        time.sleep(0.05)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.05)

        fg = win32gui.GetForegroundWindow()
        if fg != hwnd:
            tid_fg = win32process.GetWindowThreadProcessId(fg)[0]
            tid_wx = win32process.GetWindowThreadProcessId(hwnd)[0]
            if tid_fg != tid_wx:
                _ctypes.windll.user32.AttachThreadInput(tid_fg, tid_wx, True)
                win32gui.SetForegroundWindow(hwnd)
                _ctypes.windll.user32.AttachThreadInput(tid_fg, tid_wx, False)

        time.sleep(0.2)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    except Exception as e:
        log_line(f'⚠️ 置顶异常: {e}')


# ═══════════════════════════════════════
# 截图 + OCR + 坐标
# ═══════════════════════════════════════

def screenshot_ocr(hwnd):
    """截取微信窗口并OCR，返回 (img, details_list, rect)"""
    from PIL import ImageGrab
    rect = win32gui.GetWindowRect(hwnd)
    img = ImageGrab.grab(bbox=rect)
    result = _get_ocr()(img)
    # ocr_image 返回 dict{'text','lines','details':[{bbox,text,conf}]}
    details = result['details'] if isinstance(result, dict) else result
    return img, details, rect

def click_at(hwnd, ocr_x, ocr_y, button='left'):
    """OCR截图坐标 → 屏幕点击，支持 left/right"""
    import win32api
    rect = win32gui.GetWindowRect(hwnd)
    sx = int(rect[0] + ocr_x)
    sy = int(rect[1] + ocr_y)
    if button == 'right':
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    else:
        ljqCtrl.Click(sx, sy)

def find_text(details, target, exact=True, x_min=None, x_max=None, y_min=None):
    """在OCR结果中查找文本，返回 [{'text','cx','cy'}, ...]"""
    matches = []
    for d in details:
        t = d.get('text', '').strip()
        if not t:
            continue
        match = (t == target) if exact else (target in t)
        if not match:
            continue
        bbox = d['bbox']
        cx = (bbox[0][0] + bbox[2][0]) / 2
        cy = (bbox[0][1] + bbox[2][1]) / 2
        if x_min is not None and cx < x_min:
            continue
        if x_max is not None and cx > x_max:
            continue
        if y_min is not None and cy < y_min:
            continue
        matches.append({'text': t, 'cx': cx, 'cy': cy})
    return matches



# ═══════════════════════════════════════
# 发送流程
# ═══════════════════════════════════════

def send_to(target_name, msg, max_retries=5, at_name=None):
    """
    OCR识别发送：截图→检测是否已在目标聊天→不在则列表找→点击→验证右侧有内容→发送
    - 标题栏OCR不可靠，验证改为：点击后右侧有文本即认为导航成功
    - 若已在目标聊天但点击了列表（toggle关闭），下轮自动恢复
    - 最多5次重试，记录失败原因
    at_name: 群聊时传入对方昵称，会先输入@触发微信匹配
    """
    hwnd = find_wechat_window()
    if not hwnd:
        log_line('❌ 未找到微信窗口')
        return False

    last_error = ""
    for attempt in range(max_retries):
        try:
            log_line(f'📤 send_to("{target_name}") attempt={attempt+1}/{max_retries}')

            force_foreground(hwnd)
            time.sleep(0.3)

            img, details, rect = screenshot_ocr(hwnd)
            w, h = img.size
            log_line(f'  📸 截图 {w}x{h}')

            # ── Step 1: 右侧标题栏/聊天区是否已有目标名（精确+模糊）──
            chat_hits = find_text(details, target_name, exact=True, x_min=w*0.3)
            if not chat_hits:
                chat_hits = find_text(details, target_name, exact=False, x_min=w*0.3)
            if chat_hits:
                log_line(f'  ✅ 已在目标聊天（匹配:"{chat_hits[0]["text"]}"），直接发送')
                _do_send(hwnd, msg, at_name=at_name)
                return True

            # ── Step 2: 左侧聊天列表找目标 ──
            list_hits = find_text(details, target_name, exact=True,
                                  x_max=w*0.3, y_min=120)
            if not list_hits:
                list_hits = find_text(details, target_name, exact=False,
                                      x_max=w*0.3, y_min=120)

            if not list_hits:
                log_line(f'  ⏭️ 未找到 "{target_name}"，重试OCR')
                last_error = f'OCR未识别到"{target_name}"'
                time.sleep(1)
                continue

            target = min(list_hits, key=lambda m: m['cy'])
            log_line(f'  📋 列表找到: "{target["text"]}" at ({target["cx"]:.0f},{target["cy"]:.0f})')

            # 点击导航
            click_at(hwnd, target['cx'], target['cy'])
            time.sleep(0.6)

            # ── 验证：右侧是否有聊天内容（>=3条文本=进入了聊天）──
            img2, details2, _ = screenshot_ocr(hwnd)
            right_texts = [d for d in details2 if d.get('text','').strip()
                          and (d['bbox'][0][0] + d['bbox'][2][0])/2 > img2.size[0]*0.3]
            if len(right_texts) >= 3:
                log_line(f'  ✅ 导航成功（右侧{len(right_texts)}条文本），发送消息')
                _do_send(hwnd, msg, at_name=at_name)
                return True
            else:
                log_line(f'  ⚠️ 验证失败（右侧仅{len(right_texts)}条文本），重试')
                last_error = f'点击后右侧无聊天内容(toggle?)'
                continue

        except Exception as e:
            log_line(f'⚠️ 异常(attempt {attempt+1}/{max_retries}): {e}')
            last_error = str(e)[:100]
            if attempt < max_retries - 1:
                time.sleep(1)

    log_line(f'❌ send_to 失败({max_retries}次): {last_error}')
    return False


def send_message(msg, max_retries=3):
    """在当前聊天窗口直接发送（不切换聊天）"""
    hwnd = find_wechat_window()
    if not hwnd:
        log_line('❌ 未找到微信窗口')
        return False

    for attempt in range(max_retries):
        try:
            force_foreground(hwnd)
            _do_send(hwnd, msg)
            return True
        except Exception as e:
            log_line(f'⚠️ 发送异常(attempt {attempt+1}): {e}')
            if attempt < max_retries - 1:
                time.sleep(1)
    return False


def _do_send(hwnd, msg, at_name=None):
    """粘贴+Enter发送。群聊时 at_name 非 None 则先输入 @昵称 触发微信匹配"""
    # 输入框位置：ClientToScreen估算
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    input_x = cx + cw * 0.5
    input_y = cy + ch * 0.92

    # 点击输入框
    force_foreground(hwnd)
    ljqCtrl.Click(input_x, input_y)
    time.sleep(0.3)

    if at_name:
        # ── 群聊 @ 模式：先输入 @昵称 触发微信匹配 ──
        clip = _get_clipboard()
        at_text = f"@{at_name}"
        clip.copy(at_text)
        time.sleep(0.1)
        force_foreground(hwnd)
        ljqCtrl.Press('ctrl+v', staytime=0)
        time.sleep(0.8)  # 等微信匹配候选
        # 按空格确认（如果微信弹出唯一候选则直接确认）
        force_foreground(hwnd)
        ljqCtrl.Press('space', staytime=0)
        time.sleep(0.3)
        # 再粘贴实际内容（不加@前缀，@已单独处理）
        clip.copy(msg)
        time.sleep(0.1)
        force_foreground(hwnd)
        ljqCtrl.Press('ctrl+v', staytime=0)
        time.sleep(0.5)
        log_line(f'  ✅ 已发送(@{at_name}): {msg[:50]}')
    else:
        # ── 私聊模式：直接粘贴 ──
        clip = _get_clipboard()
        clip.copy(msg)
        time.sleep(0.1)
        force_foreground(hwnd)
        ljqCtrl.Press('ctrl+v', staytime=0)
        time.sleep(0.5)
        log_line(f'  ✅ 已发送: {msg[:50]}')

    # Enter发送
    force_foreground(hwnd)
    time.sleep(0.1)
    ljqCtrl.Press('enter', staytime=0)
    time.sleep(0.3)


if __name__ == '__main__':
    print("sender.py v2 - OCR导航发送（无搜索）")
    print("=" * 50)
    hwnd = find_wechat_window()
    if not hwnd:
        print("❌ 未找到微信窗口")
        sys.exit(1)
    print(f"微信窗口: hwnd={hwnd}")

    test_msg = f"sender v2 test {time.strftime('%H:%M:%S')}"
    ok = send_to('文件传输助手', test_msg)
    print(f"\n{'✅' if ok else '❌'} 发送{'成功' if ok else '失败'}")
