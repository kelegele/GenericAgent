# GUI自动化能力点评估报告

## 评估目的
基于现有 ocr_utils/ui_detect/ljqCtrl，验证"窗口级截图→OCR→元素定位→点击确认"管道的可行性和精度，为未来微信/GUI自动化降低物理操作不确定性。

## 依赖状态

| 模块 | 状态 | 说明 |
|------|------|------|
| ocr_utils (rapidocr) | ✅ 可用 | ~4s/次窗口OCR，中英文准确率高，带bbox |
| win32gui/win32ui | ✅ 可用 | PrintWindow截图支持RDP断开后 |
| cv2 (OpenCV) | ✅ 可用 | 模板匹配~0.3s，精度1.0 |
| PIL | ✅ 可用 | 图像裁剪/处理 |
| ui_detect (YOLO) | ❌ 不可用 | ultralytics未装，模型权重缺失 |
| ljqCtrl | ✅ 可用 | Click/FindBlock/Press/GrabWindow |
| vision_api | ✅ 可用 | 最后手段，能OCR解决就不用 |

## 实验1: 窗口截图+OCR定位

**目标**: 验证 ocr_window(hwnd) 能否正确识别窗口文字并给出精确bbox

| 指标 | 结果 |
|------|------|
| 目标窗口 | 微信 (hwnd=919766, 932×721) |
| OCR耗时 | 4.18s |
| 识别行数 | 32行 / 242字符 |
| 示例定位 | "Q搜索" conf=0.897 center=(157,85) ✅ |
| 示例定位 | "大少奶奶" conf=1.000 center=(652,140) ✅ |
| 示例定位 | "肚子痛" conf=0.999 center=(667,186) ✅ |

**结论**: OCR精度优秀，中文识别conf普遍>0.86，bbox坐标精确。4.18s略慢但可接受。

## 实验2: 模板匹配精度

**目标**: 验证 cv2.matchTemplate 能否从截图中精确匹配裁剪的UI元素

| 指标 | 结果 |
|------|------|
| 模板 | "Q搜索"区域 (110×30px) |
| 匹配耗时 | 0.276s |
| 最大置信度 | **1.0000** (完美匹配) |
| 匹配位置 | (100, 70) → center=(155, 85) |
| 原始位置 | center=(155, 85) |
| 误差 | **0像素** |

**结论**: 模板匹配在截图内精度完美，速度快(0.3s)。适合固定UI元素的可靠定位。

## 实验3: OCR bbox → 物理坐标映射

**目标**: 验证OCR输出的bbox坐标能否正确映射到屏幕物理坐标

| 文字 | OCR bbox center(截图内) | +窗口偏移→逻辑坐标 | ×dpi_scale→物理坐标 |
|------|------------------------|-------------------|-------------------|
| Q搜索 | (157, 85) | (330, 343) | (495, 515) |
| 大少奶奶 | (652, 140) | (825, 398) | (1238, 597) |
| 肚子痛 | (667, 186) | (840, 444) | (1260, 666) |

**关键发现**: PrintWindow截图返回的是**逻辑像素**(已除dpi_scale)，不是物理像素。

## 实验4: DPI缩放验证（⚠️ 最关键发现）

| 指标 | 值 |
|------|-----|
| 物理分辨率 | 3840×2160 |
| 逻辑分辨率 | 2560×1440 |
| dpi_scale | **0.6667** (=2560/3840) |

### 坐标映射链（核心公式）

```
截图内坐标(逻辑像素)
    → + 窗口左上角(GetWindowRect返回逻辑坐标)
    = 屏幕逻辑坐标
    → ÷ dpi_scale (= ×1.5)
    = 物理坐标 → 用于 ljqCtrl.Click()
```

**简化**: `Click_x = (ocr_cx + win_left) / dpi_scale`, `Click_y = (ocr_cy + win_top) / dpi_scale`

但注意 ljqCtrl 源码中 `Click(x, y)` 内部已做 `SetCursorPos((int(x*dpi_scale), int(y*dpi_scale)))`——
即 **Click 期望输入物理坐标但内部会乘 dpi_scale**！这与直觉矛盾。

实际验证需确认：ljqCtrl.Click 接收的到底是物理还是逻辑坐标。从源码看：
- `dpi_scale = cwidth / swidth` = 0.6667
- `SetCursorPos((int(x*dpi_scale), int(y*dpi_scale)))` 
- SetCursorPos 期望物理坐标
- 所以 Click(x, y) 中的 x,y 应该是 **逻辑坐标 × (1/dpi_scale)** = 物理坐标/实际物理值

**结论**: 坐标映射是这个管道最大的坑。建议封装一个 `click_text(hwnd, target_text)` 函数屏蔽所有坐标转换。

## 可行性评估

### ✅ 现有能力足以完成的任务

1. **点击指定文字**: OCR找文字→计算bbox中心→映射到物理坐标→Click
2. **模板匹配点击**: 裁剪模板→FindBlock定位→Click
3. **读取窗口文字**: ocr_window→获取全部文本
4. **等待元素出现**: 循环截图→OCR→检查目标文字conf

### ❌ 当前限制

1. **UI元素检测(YOLO)不可用**: 无法检测按钮/输入框/图标等非文字元素
2. **OCR速度**: 4s/次，不适合高频轮询（可用区域OCR优化）
3. **DPI坑**: 坐标映射复杂，容易出错
4. **RDP断开**: ImageGrab全黑，必须用PrintWindow(ocr_window已处理)

## 推荐的最小验证函数

```python
def click_text_in_window(hwnd, target_text, confidence=0.8):
    """在指定窗口中找到目标文字并点击"""
    from ocr_utils import ocr_window
    from ljqCtrl import Click, dpi_scale
    import win32gui
    
    result = ocr_window(hwnd)
    for d in result['details']:
        if target_text in d['text'] and float(d['conf']) >= confidence:
            bbox = d['bbox']
            # bbox是截图内逻辑像素
            cx = sum(p[0] for p in bbox) / 4
            cy = sum(p[1] for p in bbox) / 4
            # +窗口偏移 = 屏幕逻辑坐标
            rect = win32gui.GetWindowRect(hwnd)
            screen_x = cx + rect[0]
            screen_y = cy + rect[1]
            # Click内部会×dpi_scale转为物理坐标给SetCursorPos
            # 但SetCursorPos要物理坐标，所以这里应传逻辑坐标
            Click(screen_x, screen_y)
            return True
    return False
```

## 建议下一步

1. **封装 gui_auto.py 工具**: 把截图+OCR+坐标映射+点击封装为高层API
2. **安装ultralytics+模型**: 解锁YOLO UI元素检测能力(按钮/图标)
3. **区域OCR优化**: 对已知区域做小范围OCR，速度可提升10x
4. **实际验证**: 在微信窗口执行一次安全的"读取群名→点击"闭环

## 实验产物
- `gui_auto_exp/wechat_capture.png` - 微信截图样本
- `gui_auto_exp/template_qsearch.png` - 裁剪的模板