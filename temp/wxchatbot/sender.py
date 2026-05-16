"""
wxchatbot 发送模块 - 微信消息发送
从wechat_reflect提取send_to核心逻辑
依赖: ljqCtrl (物理键鼠), pyperclip (剪贴板), win32gui/win32con
"""
import os, sys, time, traceback
from pathlib import Path

# 添加路径
PROJECT_DIR = Path(__file__).parent
for _p in [
    str(PROJECT_DIR),
    str(PROJECT_DIR.parent.parent / 'memory'),  # GenericAgent/memory/ -> ljqCtrl
]:
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import win32gui, win32con
import ljqCtrl


def log_line(msg):
    """简易日志"""
    from datetime import datetime
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


# 兼容新旧版微信的窗口类
WINDOW_CLASSES = ['WeChatMainWndForPC', 'Qt51514QWindowIcon']


def find_wechat_window():
    """
    查找微信窗口（兼容新旧版）
    返回: hwnd 或 None
    """
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
        log_line(f'  📱 找到微信: "{title}" (class={cls})')
        return hwnd

    # 降级：任何含"微信"的可见窗口
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
        log_line(f'  📱 找到微信(降级): "{title}"')
        return hwnd

    return None


def force_foreground(hwnd):
    """强制微信窗口到最前"""
    try:
        import win32process
        # 1. TOPMOST
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        time.sleep(0.05)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.05)
        # 2. AttachThreadInput + SetForegroundWindow
        fg = win32gui.GetForegroundWindow()
        if fg != hwnd:
            tid_fg = win32process.GetWindowThreadProcessId(fg)[0]
            tid_wx = win32process.GetWindowThreadProcessId(hwnd)[0]
            if tid_fg != tid_wx:
                win32process.AttachThreadInput(tid_fg, tid_wx, True)
            win32gui.SetForegroundWindow(hwnd)
            if tid_fg != tid_wx:
                win32process.AttachThreadInput(tid_fg, tid_wx, False)
        time.sleep(0.3)
        # 3. 取消TOPMOST
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    except Exception as e:
        log_line(f'  ⚠️ 置顶异常: {e}')


def click_input_box(hwnd):
    """点击微信输入框（客户区右下角）"""
    try:
        cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
        _, _, cw, ch = win32gui.GetClientRect(hwnd)
        # 输入框大约在客户区 90%宽, 95%高 位置
        input_lx = cx + cw * 0.90
        input_ly = cy + ch * 0.95
        # 逻辑坐标 → 物理坐标（ljqCtrl要求）
        px = input_lx / ljqCtrl.dpi_scale
        py = input_ly / ljqCtrl.dpi_scale
        ljqCtrl.Click(px, py)
        time.sleep(0.2)
    except Exception as e:
        log_line(f'  ⚠️ 点击输入框异常: {e}')


def send_message(msg, max_retries=3):
    """
    在当前微信窗口直接发送消息（不切换聊天）
    使用 ljqCtrl 物理坐标 + ClientToScreen 定位输入框
    
    Args:
        msg: 要发送的消息文本
        max_retries: 最大重试次数
    Returns:
        bool: 是否发送成功
    """
    import pyperclip
    
    hwnd = find_wechat_window()
    if not hwnd:
        log_line('  ❌ 未找到微信窗口，无法发送')
        return False
    
    for attempt in range(max_retries):
        try:
            # 1) 激活窗口 + 点击输入框
            force_foreground(hwnd)
            click_input_box(hwnd)
            
            # 2) 粘贴前再次确保最前
            force_foreground(hwnd)
            pyperclip.copy(msg)
            time.sleep(0.1)
            
            # 3) Ctrl+V 粘贴
            ljqCtrl.Press('ctrl+v', staytime=0)
            time.sleep(0.5)
            
            # 4) Enter 发送
            force_foreground(hwnd)
            ljqCtrl.Press('enter', staytime=0)
            time.sleep(0.8)
            
            log_line(f'  ✅ 已发送: {msg[:50]}')
            return True
            
        except Exception as e:
            log_line(f'  ⚠️ 发送异常(attempt {attempt+1}): {e}')
            if attempt < max_retries - 1:
                time.sleep(1)
    
    log_line(f'  ❌ 发送失败(已重试{max_retries}次)')
    return False


if __name__ == '__main__':
    print("sender.py - 微信消息发送模块")
    print("用法: 被 bot.py import 使用")
    hwnd = find_wechat_window()
    if hwnd:
        print(f"微信窗口: hwnd={hwnd}")
    else:
        print("未找到微信窗口")
