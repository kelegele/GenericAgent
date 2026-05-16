# R04: procmem_scanner 能力边界探测

## 日期
2026-05-17

## 结论
procmem_scanner 功能完全正常，基于yara引擎的进程内存扫描工具，性能优秀（全进程扫描~0.1s），适合用于进程内存特征搜索和CE式差集扫描。

## 能力卡片

| 维度 | 详情 |
|------|------|
| **核心功能** | 基于 yara 引擎扫描目标进程虚拟地址空间（0 ~ 0x7FFFFFFFFFFF） |
| **扫描模式** | `string`（纯文本）、`hex`（CE风格含`??`通配符）、`auto`（自动判断） |
| **llm_mode** | 输出JSON数组，每项含 address/offset/hex/ascii/match_pos，便于LLM分析上下文 |
| **依赖** | yara-python（已安装v4.5.4）、ctypes（WinAPI ReadProcessMemory+VirtualQueryEx） |
| **性能** | 自身进程全空间扫描：string模式 ~0.11s、hex通配符模式 ~0.10s |
| **权限** | 需 PROCESS_QUERY_INFORMATION + PROCESS_VM_READ（非强制管理员） |
| **CLI** | `python procmem_scanner.py <pid> <pattern> --mode auto --llm` |
| **Python API** | `scan_memory(pid, pattern, mode="auto", llm_mode=False) → list` |

## 实验验证

| 测试 | 模式 | Pattern | 结果 | 耗时 |
|------|------|---------|------|------|
| 1 | string | `procmem_scanner` | 7 hits | - |
| 2 | hex | `70 72 6f 63 6d 65 6d` | 7 hits | - |
| 3 | string | `python` | 27 hits | 0.11s |
| 4 | hex+wildcard | `70 72 ?? 63 6d 65 ??` | 6 hits | 0.10s |

## 适用场景
1. **CE式差集扫描**：定位微信等自绘UI中的动态内存字段（如当前会话标题）
2. **特征码定位**：搜索PE头（`4D 5A`）、魔数、特定指令序列
3. **调试辅助**：在目标进程中定位已知字符串/hex特征的内存地址
4. **LLM协作分析**：llm_mode=True 输出含上下文的JSON，便于AI分析内存布局

## 已执行操作（待审）
- `uv pip install yara-python`（安装到GA .venv，v4.5.4）
