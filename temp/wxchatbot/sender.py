"""
wxchatbot 发送模块 v2 - OCR导航（无搜索）
流程：
  1. 截图OCR当前页面，看是否已在目标聊天
  2. 已在 → 直接粘贴发送
  3. 不在 → 聊天列表找目标 → 点击导航 → 发送
  4. 列表也没有 → 返回失败
坐标链：SetProcessDPIAware → 截图(物理像素) → OCR → WindowRect左上角+截图坐标 → ljqCtrl.Click
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
    """强制窗口到最前"""
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

        time.sleep(0.3)
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


def _delete_one_read_chat(hwnd, details, img_w, target_name):
    """
    步骤3: 找一个已回聊天（无红点的），右键选「不显示」让它从列表隐藏
    让未回消息顶上来。返回是否成功隐藏一个
    
    注意：不能用"删除"，会清空聊天记录！必须用"不显示"
    """
    # 收集聊天列表中的所有文字条目
    chat_items = []
    skip_keywords = ['搜索', 'Q 搜索', 'Q搜索', '聊天', '通讯录', '发现', '我']
    
    for d in details:
        if not isinstance(d, dict):
            continue
        t = d.get('text', '').strip()
        if not t or t in skip_keywords:
            continue
        bbox = d['bbox']
        cx = (bbox[0][0] + bbox[2][0]) / 2
        cy = (bbox[0][1] + bbox[2][1]) / 2
        
        # 聊天列表区域：x < 30%, y > 120
        if cx >= img_w * 0.3 or cy < 120:
            continue
        # 排除目标名字
        if t == target_name or target_name in t:
            continue
        # 排除时间格式（如 "05/01", "04:20"）
        if any(c.isdigit() for c in t) and len(t) <= 6:
            continue
        # 排除消息预览（通常x偏右，文字较长且包含冒号等）
        # 聊天列表名字通常在 x < 15% 区域
        
        chat_items.append({'text': t, 'cx': cx, 'cy': cy})
    
    if not chat_items:
        log_line('  🗑️ 聊天列表无其他条目可删除')
        return False
    
    # 选y最大的（最底部的，最早出现的）
    # 但要避免连续删除同一个，所以选最底部
    target = max(chat_items, key=lambda m: m['cy'])
    log_line(f'  🗑️ 删除已回聊天: "{target["text"]}" at ({target["cx"]:.0f},{target["cy"]:.0f})')
    
    # 右键点击
    force_foreground(hwnd)
    click_at(hwnd, target['cx'], target['cy'], button='right')
    time.sleep(0.5)
    
    # 找右键菜单中的「不显示」选项（注意：不能用"删除"，会清空聊天记录！）
    img, menu_details, _ = screenshot_ocr(hwnd)
    w2 = img.size[0]
    
    # 找"不显示"文字（OCR可能识别为"不显示"或"不显 示"等）
    hide_hits = find_text(menu_details, '不显示', exact=False)
    if not hide_hits:
        log_line('  ⚠️ 右键菜单没找到"不显示"，按Esc关闭')
        ljqCtrl.Press('esc', staytime=0)
        return False
    
    # 选离点击位置最近的"不显示"
    hide_target = min(hide_hits, key=lambda m: abs(m['cy'] - target['cy']))
    log_line(f'  👁️ 点击"不显示" at ({hide_target["cx"]:.0f},{hide_target["cy"]:.0f})')
    
    force_foreground(hwnd)
    click_at(hwnd, hide_target['cx'], hide_target['cy'])
    time.sleep(0.3)
    
    # 「不显示」弹出二次确认弹窗，点击「我知道了」
    time.sleep(0.3)
    img3, confirm_details, _ = screenshot_ocr(hwnd)
    confirm_hits = find_text(confirm_details, '我知道了', exact=False)
    if confirm_hits:
        force_foreground(hwnd)
        click_at(hwnd, confirm_hits[0]['cx'], confirm_hits[0]['cy'])
        log_line(f'  ✅ 已隐藏 "{target["text"]}"（点击了"我知道了"）')
    else:
        log_line(f'  ⚠️ 未找到"我知道了"确认按钮，可能已隐藏 "{target["text"]}"')
    
    time.sleep(0.3)
    return True


# ═══════════════════════════════════════
# 发送流程
# ═══════════════════════════════════════

def send_to(target_name, msg, max_retries=2):
    """
    智能发送：先看当前页→再看聊天列表→发送
    不使用搜索功能。
    """
    hwnd = find_wechat_window()
    if not hwnd:
        log_line('❌ 未找到微信窗口')
        return False

    max_delete_rounds = 8  # 最多删8轮已回聊天
    for attempt in range(max_retries):
        try:
            log_line(f'📤 send_to("{target_name}") attempt={attempt+1}')

            for delete_round in range(max_delete_rounds + 1):
                # 激活窗口
                force_foreground(hwnd)
                time.sleep(0.3)

                # 截图OCR
                img, details, rect = screenshot_ocr(hwnd)
                w, h = img.size
                log_line(f'  📸 截图 {w}x{h} (round={delete_round})')

                # ── Step 1: 当前页面是否已在目标聊天 ──
                chat_hits = find_text(details, target_name, exact=True, x_min=w*0.3)

                if chat_hits:
                    log_line(f'  ✅ 当前页面已有 "{target_name}"，直接发送')
                    _do_send(hwnd, msg)
                    return True

                # ── Step 2: 聊天列表中找目标 ──
                list_hits = find_text(details, target_name, exact=True,
                                      x_max=w*0.3, y_min=120)

                if list_hits:
                    target = min(list_hits, key=lambda m: m['cy'])
                    log_line(f'  📋 聊天列表: "{target["text"]}" at ({target["cx"]:.0f},{target["cy"]:.0f})')

                    # 点击导航
                    force_foreground(hwnd)
                    click_at(hwnd, target['cx'], target['cy'])
                    time.sleep(0.5)

                    # 验证
                    img2, details2, _ = screenshot_ocr(hwnd)
                    verify = find_text(details2, target_name, exact=True, x_min=img2.size[0]*0.3)
                    if verify:
                        log_line(f'  ✅ 导航成功，发送消息')
                        _do_send(hwnd, msg)
                        return True
                    else:
                        log_line(f'  ⚠️ 导航后验证失败，重试')
                        break  # 跳出delete循环，进入下一轮attempt

                # ── Step 3: 右键隐藏已回聊天（无红点）──
                if delete_round >= max_delete_rounds:
                    log_line(f'  ❌ 已隐藏{max_delete_rounds}轮仍未找到 "{target_name}"')
                    break

                deleted = _delete_one_read_chat(hwnd, details, w, target_name)
                if not deleted:
                    log_line(f'  ❌ 没有可隐藏的已回聊天，找不到 "{target_name}"')
                    break  # 无法继续，跳出delete循环
                # 隐藏成功，继续循环让未回消息顶上来
                time.sleep(0.5)

        except Exception as e:
            log_line(f'⚠️ 异常(attempt {attempt+1}): {e}')
            traceback.print_exc()
            if attempt < max_retries - 1:
                time.sleep(1)

    log_line(f'❌ send_to 失败')
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


def _do_send(hwnd, msg):
    """粘贴+Enter发送"""
    # 输入框位置：ClientToScreen估算
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    input_x = cx + cw * 0.5
    input_y = cy + ch * 0.92

    # 点击输入框
    force_foreground(hwnd)
    ljqCtrl.Click(input_x, input_y)
    time.sleep(0.3)

    # 粘贴
    clip = _get_clipboard()
    clip.copy(msg)
    time.sleep(0.1)
    force_foreground(hwnd)
    ljqCtrl.Press('ctrl+v', staytime=0)
    time.sleep(0.5)

    # Enter发送
    force_foreground(hwnd)
    time.sleep(0.1)
    ljqCtrl.Press('enter', staytime=0)
    time.sleep(0.3)

    log_line(f'  ✅ 已发送: {msg[:50]}')


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
