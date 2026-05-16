# R05: GUI Automation OCR 工具调研

## 日期
2026-05-17

## 结论
当前 ocr_utils(RapidOCR) + gui_auto(cv2模板匹配) + ljqCtrl(键鼠) 方案已覆盖90%的GUI自动化场景，是Python生态中最轻量的成熟组合。发现的几个潜在改进方向：OmniParser(微软)、pyautogen+computer-use，但均需API调用，不符合本地轻量需求。**建议维持当前方案**。

## 当前工具栈
| 工具 | 版本 | 用途 |
|------|------|------|
| rapidocr-onnxruntime | 1.2.3 | OCR引擎，~1s/次，中英文+bbox |
| opencv-python | 4.13.0 | 模板匹配、图像处理 |
| pillow | 12.2.0 | 截图、图像预处理 |
| mss | 10.2.0 | 高性能截图 |
| pywin32 | 311 | 窗口枚举、WinAPI |
| PyGetWindow | 0.9 | 窗口管理 |

## 竞品对比（基于训练知识，截至2025年初）

### Tier 1: 框架级
| 工具 | 特点 | vs当前 |
|------|------|--------|
| **PyAutoGUI** | 全平台键鼠+截图+locateOnScreen | 过重，已禁用(pyautogui触发安全限制)，ljqCtrl更好 |
| **SikuliX** | 基于OpenCV的视觉自动化，Java | 需JVM，Python绑定过时，不如gui_auto轻量 |
| **TagUI** | RPA工具，支持视觉+OCR | Node.js生态，Python集成差 |

### Tier 2: AI-native（需API）
| 工具 | 特点 | vs当前 |
|------|------|--------|
| **OmniParser**(微软) | 将截图解析为结构化UI元素 | 需GPT-4V API，本地部署模型大，不适合轻量场景 |
| **Anthropic Computer Use** | Claude直接操控屏幕 | 需API，当前vision_sop已有类似能力 |
| **OS-Atlas** | 开源GUI grounding模型 | 需GPU推理，不如OCR+模板匹配快 |

### Tier 3: OCR引擎替代
| 工具 | 特点 | vs RapidOCR |
|------|------|-------------|
| **Tesseract** | 老牌OCR，pytesseract绑定 | 中文精度差，~3s/次，远不如rapidocr |
| **PaddleOCR** | 百度，中文精度高 | 依赖paddlepaddle(2GB+)，rapidocr是其ONNX轻量版 |
| **EasyOCR** | 多语言支持 | ~3s首次加载，精度不如rapidocr，依赖pytorch |

## 建议
1. **维持当前方案** — RapidOCR是最优本地OCR，cv2模板匹配足够快
2. **值得关注**: OmniParser v2如果开源本地模型成熟，可替代ocr+模板匹配两步为一步
3. **不需要变更**: 没有发现比当前方案更成熟的轻量替代
