"""
wxchatbot 主程序 - 文件交换方案
流程: poll DB → 黑白名单过滤 → 写pending.json → 等待reply.json → sender发送
"""
import os, sys, json, time, signal, logging, traceback
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from db import DBConfig, PollState, query_new_messages, build_uname2display
from sender import find_wechat_window, send_to

# ============ 配置加载 ============

def _find_wechat_decrypt_config():
    """自动查找wechat-decrypt/config.json"""
    # 搜索路径：先看同级的wechat_reflect_project/wechat-decrypt/
    candidates = [
        PROJECT_DIR.parent / "wechat_reflect_project" / "wechat-decrypt" / "config.json",
        PROJECT_DIR / "wechat-decrypt" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# 环境变量 → config.yaml 字段 的映射
_ENV_DB_MAP = {
    'WX_DB_DIR':             'db_dir',
    'WX_MY_WXID':            'my_wxid',
    'WX_MY_DISPLAY_NAME':    'my_display_name',
    'WX_MSG_DB_KEY':         'msg_db_key_hex',
    'WX_CONTACT_DB_KEY':     'contact_db_key_hex',
    'WX_MSG_DB_RELPATH':     'msg_db_relpath',
    'WX_CONTACT_DB_RELPATH': 'contact_db_relpath',
}
_ENV_BOT_MAP = {
    'WX_PERSONA':    'persona',
    'WX_MODE':       'mode',          # blacklist / whitelist
    'WX_BLACKLIST':  'blacklist',     # 逗号分隔
    'WX_WHITELIST':  'whitelist',     # 逗号分隔
}


def _apply_env_vars(cfg: dict) -> dict:
    """用环境变量覆盖配置（最高优先级）"""
    # DB段
    db_cfg = cfg.setdefault('db', {})
    for env_key, cfg_key in _ENV_DB_MAP.items():
        val = os.environ.get(env_key)
        if val:
            db_cfg[cfg_key] = val

    # Bot段（persona / mode / blacklist / whitelist）
    bot_cfg = cfg.setdefault('bot', {})
    for env_key, cfg_key in _ENV_BOT_MAP.items():
        val = os.environ.get(env_key)
        if val:
            # blacklist/whitelist: 逗号分隔 → list
            if cfg_key in ('blacklist', 'whitelist'):
                bot_cfg[cfg_key] = [x.strip() for x in val.split(',') if x.strip()]
            else:
                bot_cfg[cfg_key] = val

    return cfg


def load_config(config_path=None):
    """加载配置：.env > config.yaml > wechat-decrypt/config.json 兜底"""
    if config_path is None:
        config_path = PROJECT_DIR / "config.yaml"

    # 0) 加载 .env 文件（不覆盖已有的系统环境变量）
    from dotenv import load_dotenv
    dotenv_path = PROJECT_DIR / ".env"
    load_dotenv(dotenv_path, override=False)

    # 1) config.yaml（可能不存在）
    cfg = {}
    if config_path.exists():
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}

    # 2) .env / 环境变量覆盖（最高优先级）
    cfg = _apply_env_vars(cfg)

    # 3) wechat-decrypt/config.json 兜底（只补充仍缺失的DB字段）
    db_cfg = cfg.setdefault('db', {})
    needs_keys = not db_cfg.get('msg_db_key_hex') or not db_cfg.get('db_dir')

    if needs_keys:
        wd_config = _find_wechat_decrypt_config()
        if wd_config:
            with open(wd_config, 'r', encoding='utf-8') as f:
                wd = json.load(f)
            for key in ('db_dir', 'my_wxid', 'msg_db_key_hex', 'contact_db_key_hex',
                        'msg_db_relpath', 'contact_db_relpath'):
                if not db_cfg.get(key) and wd.get(key):
                    db_cfg[key] = wd[key]

    return cfg


# ============ 日志 ============

def setup_logger(log_level="INFO"):
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
    
    logger = logging.getLogger("wxchatbot")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # 文件handler
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(fh)
    
    # 控制台handler
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)
    
    return logger


# ============ 文件交换 ============

class FileExchange:
    """管理 pending.json 和 reply.json 的文件交换"""
    
    def __init__(self, exchange_dir):
        self.exchange_dir = Path(exchange_dir)
        self.exchange_dir.mkdir(parents=True, exist_ok=True)
        self.pending_path = self.exchange_dir / "pending.json"
        self.reply_path = self.exchange_dir / "reply.json"
    
    def write_pending(self, messages):
        """写待回复消息列表，供GA读取"""
        data = {
            "timestamp": datetime.now().isoformat(),
            "messages": messages
        }
        # 原子写入：先写临时文件再replace（Windows上rename不能覆盖已存在文件）
        tmp = self.pending_path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(self.pending_path))
    
    def read_reply(self):
        """读取GA写回的回复，返回后删除文件"""
        if not self.reply_path.exists():
            return None
        try:
            with open(self.reply_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 读取后删除（消费模式）
            self.reply_path.unlink()
            return data
        except (json.JSONDecodeError, Exception) as e:
            return None
    
    def has_pending(self):
        """检查是否有未处理的pending（GA还没读走）"""
        return self.pending_path.exists()


# ============ 黑白名单过滤 ============

def should_reply(chat_display, config, my_wxid=None):
    """根据黑白名单模式判断是否需要回复该会话"""
    mode = config.get('mode', 'blacklist')
    name = chat_display.strip() if chat_display else ''
    
    if mode == 'blacklist':
        blacklist = [b.strip() for b in config.get('blacklist', [])]
        return name not in blacklist
    elif mode == 'whitelist':
        whitelist = [w.strip() for w in config.get('whitelist', [])]
        return name in whitelist
    else:
        return True


# ============ 消息格式化 ============

def format_messages_for_ga(raw_messages, config):
    """将原始消息格式化为GA可理解的结构"""
    persona = config.get('persona', '你是一个友好的聊天助手')
    results = []
    
    # 按会话分组（chat_name作为key，区分私聊和群聊）
    from collections import defaultdict
    by_chat = defaultdict(list)
    for msg in raw_messages:
        # is_self消息只用于去重，不发给GA
        if msg.get('is_self'):
            continue
        by_chat[msg['chat_name']].append(msg)
    
    for chat_display, msgs in by_chat.items():
        # 取第一条消息的is_group和chat信息
        first = msgs[0]
        is_group = first.get('is_group', False)
        chat_already_replied = first.get('chat_already_replied', False)
        
        # === 回复判定 ===
        needs_reply = True
        
        # 已回复检测：如果最后一条消息已回复，跳过
        if chat_already_replied:
            needs_reply = False
        elif is_group:
            # 群聊未回复：检查最近消息是否@自己
            has_at_me = any(m.get('is_at', False) for m in msgs)
            if not has_at_me:
                needs_reply = False
        
        chat_msgs = []
        for m in msgs:
            chat_msgs.append({
                "sender": m['sender'],
                "sender_wxid": m.get('sender_wxid', ''),
                "content": m['content'],
                "is_at": m.get('is_at', False)
            })
        
        results.append({
            "chat": chat_display,
            "is_group": is_group,
            "chat_type": first.get('chat_type', 'unknown'),
            "chat_hash": first.get('chat_hash', ''),
            "chat_already_replied": chat_already_replied,
            "messages": chat_msgs,
            "needs_reply": needs_reply
        })
    
    return {
        "persona": persona,
        "chats": results
    }


# ============ 主循环 ============

class WxChatBot:
    def __init__(self, config_path=None):
        self.config = load_config(config_path)
        self.logger = setup_logger(self.config.get('log_level', 'INFO'))
        self.running = False
        
        # 初始化DB
        db_cfg = self.config.get('db', {})
        self.db_config = DBConfig(db_cfg)
        
        # 初始化PollState
        state_dir = PROJECT_DIR / "state"
        state_dir.mkdir(exist_ok=True)
        self.poll_state = PollState(state_dir / "poll_state.json")
        
        # 初始化文件交换
        exchange_dir = PROJECT_DIR / self.config.get('exchange_dir', 'exchange')
        self.exchange = FileExchange(exchange_dir)
        
        self.poll_interval = self.config.get('poll_interval', 5)
        
        # 联系人映射缓存（启动时构建一次）
        self.uname2display = {}
        self._refresh_uname2display()
        
        # 统计
        self.stats = {
            "polls": 0,
            "messages_found": 0,
            "replies_sent": 0,
            "start_time": None
        }
    
    def _refresh_uname2display(self):
        """从contact.db构建联系人映射"""
        try:
            self.uname2display = build_uname2display(
                self.db_config.contact_db_path,
                self.db_config.contact_key_bytes
            )
            self.logger.info(f"联系人映射已加载: {len(self.uname2display)} 条")
        except Exception as e:
            self.logger.warning(f"加载联系人映射失败: {e}")
            self.uname2display = {}
    
    def _poll_once(self):
        """单次轮询：查新消息 → 过滤 → 写pending"""
        self.stats["polls"] += 1
        
        try:
            raw_msgs = query_new_messages(
                self.db_config.msg_db_path,
                self.db_config.msg_key_bytes,
                self.poll_state.last_seq_per_chat,
                self.poll_state.seen_server_ids,
                self.db_config.my_wxid,
                self.uname2display,
                timeout=self.config.get('db', {}).get('decrypt_timeout', 15),
                max_retries=self.config.get('db', {}).get('decrypt_max_retries', 3),
                my_display_name=self.db_config.my_display_name,
            )
        except Exception as e:
            self.logger.error(f"查询消息失败: {e}")
            return
        
        if not raw_msgs:
            return
        
        # 按会话分组
        from collections import defaultdict
        by_chat = defaultdict(list)
        for msg in raw_msgs:
            by_chat[msg['chat_name']].append(msg)
        
        # 启动时展示每个会话最近20条消息
        self.logger.info(f"{'='*60}")
        self.logger.info(f"📋 会话概览 ({len(by_chat)} 个会话，共 {len(raw_msgs)} 条消息)")
        for chat_name, msgs in sorted(by_chat.items(), key=lambda x: -len(x[1])):
            sample_msg = msgs[-1] if msgs else {}
            is_group = sample_msg.get('is_group', False)
            type_label = "群聊" if is_group else "私聊"
            hash_short = chat_name[:4] + ".." + chat_name[-4:] if len(chat_name) > 12 else chat_name
            chat_display = sample_msg.get('chat_display', hash_short)
            last_msg = msgs[-1] if msgs else {}
            last_is_self = last_msg.get('is_self', False)
            status = "✅已回复" if last_is_self else "📨待回复"
            
            self.logger.info(f"─── [{type_label}][{chat_display}][{hash_short}] {len(msgs)}条 {status} ───")
            recent = msgs[-20:] if len(msgs) > 20 else msgs
            for m in recent:
                sender = m.get('sender', '?')
                sender_wxid = m.get('sender_wxid', '')
                content = (m.get('content', '') or '')[:50]
                is_self = m.get('is_self', False)
                at_mark = ' @️⃣' if m.get('is_at') else ''
                wxid_tag = f"({sender_wxid})" if sender_wxid else ""
                self_marker = "👈" if is_self else ""
                self.logger.info(f"  {sender}{wxid_tag}{self_marker}{at_mark}: {content}")
        self.logger.info(f"{'='*60}")
        
        # Bug2修复：判定已回复——只看最后一条消息是否是自己发的
        chats_already_replied = set()
        for chat_name, msgs in by_chat.items():
            last_msg = msgs[-1] if msgs else None
            if last_msg and last_msg.get('is_self'):
                chats_already_replied.add(chat_name)
                is_group = last_msg.get('is_group', False)
                type_label = "群聊" if is_group else "私聊"
                chat_display = last_msg.get('chat_display', chat_name[:4]+".."+chat_name[-4:])
                self.logger.info(f"  ⏭️ [{type_label}][{chat_display}] 最后一条是自己发的，已回复")
        
        # 黑白名单过滤 + 去掉is_self消息 + 去掉已回复会话
        filtered = []
        for msg in raw_msgs:
            if msg.get('is_self'):
                continue  # 自己的消息不加入待回复
            chat_display = msg['chat_name']
            if chat_display in chats_already_replied:
                continue  # 已回复过的会话跳过
            if should_reply(chat_display, self.config):
                filtered.append(msg)
            else:
                self.logger.debug(f"  跳过(黑名单): {chat_display}")
        
        if not filtered:
            self.logger.info("  过滤后无待回复消息")
            return
        
        self.stats["messages_found"] += len(filtered)
        
        # 格式化
        ga_data = format_messages_for_ga(filtered, self.config)
        
        # 按会话列出
        for chat_info in ga_data["chats"]:
            chat_name = chat_info["chat"]
            chat_type = chat_info.get("chat_type", "未知")
            chat_hash = chat_info.get("chat_hash", "")
            is_group = chat_info.get("is_group", False)
            needs_reply = chat_info.get("needs_reply", True)
            chat_already_replied = chat_info.get("chat_already_replied", False)
            msg_count = len(chat_info["messages"])
            
            type_label = {"group": "群聊", "private": "私聊", "self": "自聊", "unknown": "未知"}.get(chat_type, "未知")
            hash_short = chat_hash[:4] + ".." + chat_hash[-4:] if len(chat_hash) > 12 else chat_hash
            
            if chat_already_replied:
                status = "✅已回复"
            elif not needs_reply:
                status = "⏭️无@跳过"
            else:
                status = f"📨{msg_count}条待回复"
            
            self.logger.info(f"  [{type_label}][{chat_name}][{hash_short}] {status}")
            for m in chat_info["messages"]:
                sender = m['sender']
                sender_wxid = m.get('sender_wxid', '')
                content_preview = m['content'][:50] + ('...' if len(m['content']) > 50 else '')
                at_mark = ' @️⃣' if m.get('is_at') else ''
                wxid_tag = f"({sender_wxid})" if sender_wxid else ""
                self.logger.info(f"    💬 {sender}{wxid_tag}{at_mark}: {content_preview}")
        
        # 写pending.json（只写needs_reply=True的会话）
        reply_chats = [c for c in ga_data["chats"] if c.get("needs_reply", True)]
        if reply_chats:
            self.exchange.write_pending({"chats": reply_chats, "persona": ga_data.get("persona", "")})
            self.logger.info(f"  ✅ pending.json 已写入 {len(reply_chats)} 个待回复会话，等待GA处理")
        else:
            self.logger.info("  ⏭️ 所有会话已回复/无需回复，不写pending")
        
        # 持久化cursor，防止重启后重复查询
        self.poll_state.save()
        self.logger.debug("  💾 poll_state 已保存")
    
    def _check_reply(self):
        """检查reply.json，有回复则发送"""
        reply_data = self.exchange.read_reply()
        if not reply_data:
            return
        
        replies = reply_data if isinstance(reply_data, list) else [reply_data]
        
        for reply in replies:
            chat = reply.get('chat', '')
            content = reply.get('content', '')
            skip = reply.get('skip', False)
            
            if not chat:
                self.logger.warning(f"  ⚠️ 回复缺少chat字段: {reply}")
                continue
            
            if skip or not content:
                self.logger.info(f"  ⏭️ 跳过回复 [{chat}]: skip={skip}, content={'有' if content else '空'}")
                continue
            
            # Bug1修复：群聊时通过 at_name 触发微信 @ 提醒
            is_group = reply.get('is_group', False)
            sender = reply.get('sender', '')
            at_name = sender if (is_group and sender) else None
            
            chat_hash = reply.get('chat_hash', '')
            hash_suffix = f" [{chat_hash[:4]}..{chat_hash[-4:]}]" if len(chat_hash) > 12 else ''
            self.logger.info(f"  📤 准备发送回复到 [{'群' if is_group else '私'}聊:{chat}{hash_suffix}]{' @'+sender if at_name else ''}: {content[:50]}...")
            
            success = send_to(chat, content, at_name=at_name)
            if success:
                self.stats["replies_sent"] += 1
                self.logger.info(f"  ✅ 已发送回复到 [{chat}]")
            else:
                self.logger.warning(f"  ⏭️ 未发送 [{chat}]: OCR未在聊天列表中找到该联系人，跳过")
    
    def run(self):
        """主循环"""
        self.running = True
        self.stats["start_time"] = datetime.now().isoformat()
        
        self.logger.info("=" * 50)
        self.logger.info("🤖 wxchatbot 启动")
        self.logger.info(f"  模式: {self.config.get('mode', 'blacklist')}")
        self.logger.info(f"  轮询间隔: {self.poll_interval}s")
        self.logger.info(f"  DB: {self.db_config.db_dir}")
        self.logger.info(f"  exchange: {self.exchange.exchange_dir}")
        
        # 检查微信窗口
        hwnd = find_wechat_window()
        if hwnd:
            self.logger.info(f"  微信窗口: hwnd={hwnd}")
        else:
            self.logger.warning("  ⚠️ 未找到微信窗口，发送功能不可用")
        
        self.logger.info("=" * 50)
        
        # 注册信号处理
        def _signal_handler(sig, frame):
            self.logger.info("收到停止信号，退出...")
            self.running = False
        
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        
        self.logger.info("🔄 开始轮询主循环...")
        self.logger.info(f"  poll_interval={self.poll_interval}s")
        self.logger.info(f"  DB路径: {self.db_config.msg_db_path}")
        
        # 主循环
        while self.running:
            try:
                self.logger.info(f"📡 第{self.stats['polls']+1}次轮询开始...")
                self._poll_once()
                self._check_reply()
                self.stats["polls"] += 1
                self.logger.info(f"📡 第{self.stats['polls']}次轮询完成，sleep {self.poll_interval}s")
            except KeyboardInterrupt:
                self.running = False
                break
            except Exception as e:
                self.logger.error(f"主循环异常: {e}\n{traceback.format_exc()}")
            
            # sleep，每秒检查running状态
            for _ in range(self.poll_interval):
                if not self.running:
                    break
                time.sleep(1)
        
        # 退出
        elapsed = datetime.now() - datetime.fromisoformat(self.stats["start_time"]) if self.stats["start_time"] else 0
        self.logger.info(f"🛑 wxchatbot 已停止")
        self.logger.info(f"  运行时长: {elapsed}")
        self.logger.info(f"  轮询次数: {self.stats['polls']}")
        self.logger.info(f"  发现消息: {self.stats['messages_found']}")
        self.logger.info(f"  发送回复: {self.stats['replies_sent']}")


# ============ 入口 ============

if __name__ == '__main__':
    bot = WxChatBot()
    bot.run()
