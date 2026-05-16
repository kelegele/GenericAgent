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
from sender import find_wechat_window, send_message

# ============ 配置加载 ============

def load_config(config_path=None):
    """加载config.yaml"""
    if config_path is None:
        config_path = PROJECT_DIR / "config.yaml"
    
    import yaml
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


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
        # 原子写入：先写临时文件再rename
        tmp = self.pending_path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.rename(self.pending_path)
    
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
    
    # 按会话分组
    from collections import defaultdict
    by_chat = defaultdict(list)
    for msg in raw_messages:
        by_chat[msg[4]].append(msg)  # msg[4] = chat_display
    
    for chat_display, msgs in by_chat.items():
        chat_msgs = []
        for m in msgs:
            local_id, sender_name, content, is_at, chat_disp = m
            chat_msgs.append({
                "sender": sender_name,
                "content": content,
                "is_at": is_at
            })
        
        results.append({
            "chat": chat_display,
            "messages": chat_msgs,
            "needs_reply": True
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
        
        # 统计
        self.stats = {
            "polls": 0,
            "messages_found": 0,
            "replies_sent": 0,
            "start_time": None
        }
    
    def _poll_once(self):
        """单次轮询：查新消息 → 过滤 → 写pending"""
        self.stats["polls"] += 1
        
        try:
            raw_msgs = query_new_messages(self.db_config, self.poll_state)
        except Exception as e:
            self.logger.error(f"查询消息失败: {e}")
            return
        
        if not raw_msgs:
            return
        
        self.logger.info(f"📬 发现 {len(raw_msgs)} 条新消息")
        
        # 黑白名单过滤
        filtered = []
        for msg in raw_msgs:
            local_id, sender_name, content, is_at, chat_display = msg
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
            msg_count = len(chat_info["messages"])
            self.logger.info(f"  📨 {chat_name}: {msg_count}条待回复")
            for m in chat_info["messages"]:
                sender = m['sender']
                content_preview = m['content'][:50] + ('...' if len(m['content']) > 50 else '')
                at_mark = ' @️⃣' if m['is_at'] else ''
                self.logger.info(f"    [{sender}]{at_mark}: {content_preview}")
        
        # 写pending.json
        self.exchange.write_pending(ga_data)
        self.logger.info("  ✅ pending.json 已写入，等待GA处理")
    
    def _check_reply(self):
        """检查reply.json，有回复则发送"""
        reply_data = self.exchange.read_reply()
        if not reply_data:
            return
        
        replies = reply_data if isinstance(reply_data, list) else [reply_data]
        
        for reply in replies:
            chat = reply.get('chat', '')
            content = reply.get('content', '')
            
            if not chat or not content:
                self.logger.warning(f"  ⚠️ 回复格式不完整: {reply}")
                continue
            
            self.logger.info(f"  📤 发送回复到 [{chat}]: {content[:50]}...")
            
            success = send_message(chat, content)
            if success:
                self.stats["replies_sent"] += 1
                self.logger.info(f"  ✅ 发送成功")
            else:
                self.logger.error(f"  ❌ 发送失败")
    
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
        
        # 主循环
        while self.running:
            try:
                self._poll_once()
                self._check_reply()
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
