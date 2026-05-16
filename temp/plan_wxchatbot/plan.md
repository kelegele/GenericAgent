<!-- EXECUTION PROTOCOL (每轮必读，这是你的执行指南)
1. file_read(plan.md)，找到第一个 [ ] 项
2. 该步标注了SOP → file_read 该SOP的🔑速查段
3. 执行该步骤 + Mini验证产出
4. file_patch 标记 [ ] → [✓]+简要结果，然后回到步骤1继续下一个[ ]
5. 所有步骤（包括验证步骤）标记完成后 → 终止检查：file_read(plan.md)确认0个[ ]残留
⚠ 禁止凭记忆执行 | 禁止跳过验证步骤 | 禁止未经终止检查就结束 | 禁止停下来输出纯文字汇报
💡 搬砖活（读大量代码/文件/网页/重复操作）优先委托subagent，保持主agent上下文干净
-->
# 微信聊天机器人 Harness

需求：GA化身微信聊天机器人，接管指定微信的一切消息（群聊+私聊），支持黑名单/白名单模式切换
约束：不局限旧wechat_reflect项目，从零搭建轻量harness；复用wechat-decrypt解密能力

## 探索发现
- ga.py已有WeChat Bridge（547-674行）：_ensure_wechat, do_wechat_poll, do_wechat_send
- 当前poll只支持单群target_hash过滤，不支持全会话
- wechat_reflect.py 2471行，包含：decrypt_db_to_sqlite, get_all_main_msg_tables, build_chat_display_map, send_to等核心函数
- 核心瓶颈：①当前只poll一个群 ②无持续轮询机制(heartbeat) ③send_to用物理键鼠模拟，需先激活目标窗口
- config.json已有完整配置：db_dir, my_wxid, msg_db_key_hex, contact_db_key_hex
- 微信4.x窗口类：Qt51514QWindowIcon，title="微信"

## 架构设计

```
wxchatbot/                    ← 独立项目目录
├── bot.py                    ← 主循环：poll → filter → generate → send
├── config.yaml               ← 黑白名单+模式+人设配置
├── db.py                     ← DB解密+消息查询（复用wechat-decrypt）
├── sender.py                 ← 发送消息（物理键鼠模拟）
└── requirements.txt
```

核心流程：
1. db.py: 解密message_0.db → 查所有Msg_表（不限制单群） → 返回新消息列表
2. bot.py主循环: poll新消息 → config.yaml黑白名单过滤 → GA生成回复 → sender发送
3. 持续运行：主循环 sleep 间隔轮询，独立进程运行
4. GA集成：作为独立脚本启动，GA通过stdout/in或信号与之交互

## 执行计划

1. [✓] 创建 wxchatbot/ 项目目录结构 + git init
   SOP: (无)
   结果：wxchatbot/ + exchange/ 目录已创建，统一由根目录git跟踪

2. [✓] db.py: 消息查询模块
   SOP: (无)
   依赖：1
   结果：db.py已创建，import验证通过。包含DBConfig/PollState/query_new_messages/build_uname2display

3. [✓] config.yaml: 黑白名单+模式配置
   SOP: (无)
   依赖：1
   结果：config.yaml已创建，含mode/blacklist/whitelist/poll_interval/persona/db/exchange_dir配置

4. [✓] sender.py: 消息发送模块
   SOP: ljqCtrl_sop (物理键鼠)
   依赖：1
   结果：sender.py已创建，import验证通过(微信hwnd=133940)。包含find_wechat_window/force_foreground/send_message

5. [✓] bot.py: 主循环 + 文件交换通知GA
   SOP: (无)
   依赖：2,3,4
   结果：bot.py已创建，验证通过。WxChatBot主循环+FileExchange原子读写+黑白名单过滤逻辑全部正确

6. [✓] 测试：手动运行bot.py验证完整流程
   SOP: (无)
   依赖：5
   结果：集成测试通过。config加载/DBConfig初始化/PollState/黑白名单过滤/FileExchange原子读写/消息格式化全部正确

---

## 验证检查点
7. [✓] **[VERIFY] 启动独立验证subagent**
     SOP: plan_sop.md
     结果：全部验证通过。config加载✅/DBConfig初始化✅/PollState✅/黑白名单过滤✅/FileExchange原子读写✅/消息格式化✅/sender import+微信窗口(hwnd=133940)✅
