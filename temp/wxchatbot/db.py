"""
wxchatbot DB模块 - 微信数据库解密+消息查询
从wechat_reflect_project提取核心逻辑，去除LLM/UI依赖
依赖: wechat-decrypt/monitor.py (decrypt_db_to_sqlite)
"""
import os, sys, sqlite3, json, threading, traceback, time
from pathlib import Path
from datetime import datetime

# 添加wechat-decrypt到路径
PROJECT_DIR = Path(__file__).parent
WECHAT_DECRYPT_DIR = PROJECT_DIR / "wechat-decrypt"
if WECHAT_DECRYPT_DIR.exists():
    sys.path.insert(0, str(WECHAT_DECRYPT_DIR))
else:
    # fallback: 尝试从wechat_reflect_project引用
    _fallback = PROJECT_DIR.parent / "wechat_reflect_project" / "wechat-decrypt"
    if _fallback.exists():
        sys.path.insert(0, str(_fallback))

from monitor import decrypt_db_to_sqlite


# ============ 配置 ============

class DBConfig:
    """数据库配置，从config.yaml或环境变量加载"""
    def __init__(self, config: dict):
        self.msg_db_relpath = config.get('msg_db_relpath', 'db_storage/message/message_0.db')
        self.msg_db_key_hex = config.get('msg_db_key_hex')
        self.contact_db_relpath = config.get('contact_db_relpath', 'db_storage/contact/contact.db')
        self.contact_db_key_hex = config.get('contact_db_key_hex')
        self.my_wxid = config.get('my_wxid')
        self.db_dir = config.get('db_dir')  # 微信数据根目录
        
        # 超时/重试
        self.decrypt_timeout = config.get('decrypt_timeout', 15)
        self.decrypt_max_retries = config.get('decrypt_max_retries', 3)
    
    @property
    def msg_db_path(self):
        if self.db_dir:
            return os.path.join(self.db_dir, self.msg_db_relpath)
        return None
    
    @property
    def contact_db_path(self):
        if self.db_dir:
            return os.path.join(self.db_dir, self.contact_db_relpath)
        return None
    
    @property
    def msg_key_bytes(self):
        if self.msg_db_key_hex:
            return bytes.fromhex(self.msg_db_key_hex)
        return None
    
    @property
    def contact_key_bytes(self):
        if self.contact_db_key_hex:
            return bytes.fromhex(self.contact_db_key_hex)
        return None


# ============ 联系人映射 ============

def build_uname2display(contact_db_path: str, contact_key: bytes) -> dict:
    """
    从contact.db解密并构建 username → display_name 映射
    display_name优先级: remark > nick_name > username
    """
    if not os.path.exists(contact_db_path):
        return {}
    
    conn = tmp = None
    try:
        conn, tmp = decrypt_db_to_sqlite(contact_db_path, contact_key)
        cur = conn.cursor()
        uname2display = {}
        try:
            rows = cur.execute("SELECT username, nick_name, remark FROM contact").fetchall()
            for r in rows:
                username, nick, remark = r[0], r[1], r[2]
                uname2display[username] = remark if remark else (nick if nick else username)
        except Exception as e:
            print(f"[WARN] 读contact表失败: {e}")
        return uname2display
    except Exception as e:
        print(f"[WARN] 解密contact.db失败: {e}")
        return {}
    finally:
        if conn:
            try: conn.close()
            except: pass
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass


def build_hash2display(conn) -> dict:
    """
    从消息DB的Name2Id表构建 msg_table_hash → display_name 映射
    """
    hash2display = {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT rowid, * FROM Name2Id")
        for r in cur.fetchall():
            rowid, name = r[0], r[1]
            hash2display[str(rowid).lower()] = name
    except Exception as e:
        pass  # Name2Id可能不存在于某些DB版本
    return hash2display


# ============ 消息查询 ============

def get_all_main_msg_tables(cur) -> list:
    """获取所有主消息表（Msg_开头）"""
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
        tables = [r[0] for r in cur.fetchall()]
        # 过滤掉子表（如Msg_0_backup）
        return [t for t in tables if not any(x in t.lower() for x in ['backup', 'temp', 'bak'])]
    except Exception as e:
        print(f"[WARN] 获取消息表失败: {e}")
        return []


def msg_table_to_display(table_name: str, hash2display: dict = None) -> str:
    """将Msg表名转换为可读的聊天名称"""
    table_hash = table_name.replace('Msg_', '').lower()
    if hash2display:
        return hash2display.get(table_hash, table_hash)
    return table_hash


def query_new_messages(
    db_path: str, 
    key: bytes, 
    last_seq_per_chat: dict,
    seen_ids: set,
    my_wxid: str,
    uname2display: dict,
    timeout: int = 15,
    max_retries: int = 3,
    target_hash: str = None,
) -> list:
    """
    解密消息DB，查询所有新消息
    返回: [{
        'local_id': int,
        'sender': str,        # 发送者显示名
        'sender_id': str,     # 发送者wxid
        'content': str,       # 消息内容
        'is_at': bool,        # 是否@了我
        'chat_name': str,     # 聊天名称（群名/好友名）
        'chat_hash': str,     # 聊天表hash
        'timestamp': str,     # 时间
        'is_group': bool,     # 是否群聊
    }, ...]
    """
    for attempt in range(max_retries):
        result_holder = {'result': None}
        
        def _worker():
            try:
                result_holder['result'] = _do_query(
                    db_path, key, last_seq_per_chat, seen_ids, 
                    my_wxid, uname2display, target_hash
                )
            except Exception as e:
                print(f'[ERROR] query worker: {e}')
                traceback.print_exc()
        
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout)
        
        if t.is_alive():
            print(f'[WARN] 解密超时(尝试{attempt+1}/{max_retries})')
            continue
        
        return result_holder['result'] or []
    
    return []


def _do_query(
    db_path, key, last_seq_per_chat, seen_ids, 
    my_wxid, uname2display, target_hash
) -> list:
    """实际查询逻辑（内部函数）"""
    conn = tmp = None
    result = []
    
    try:
        conn, tmp = decrypt_db_to_sqlite(db_path, key)
        cur = conn.cursor()
        
        main_tables = get_all_main_msg_tables(cur)
        hash2display = build_hash2display(conn)
        
        # 获取Name2Id行映射
        name_map = {}
        try:
            cur.execute("SELECT rowid, * FROM Name2Id")
            for r in cur.fetchall():
                name_map[r[0]] = r[1]
        except:
            pass
        
        for table_name in main_tables:
            table_hash = table_name.replace('Msg_', '').lower()
            
            if target_hash and table_hash != target_hash:
                continue
            
            chat_last_seq = last_seq_per_chat.get(table_hash, 0)
            chat_display = hash2display.get(table_hash, msg_table_to_display(table_name))
            is_group = '@chatroom' in chat_display or '@chatroom' in table_hash
            
            try:
                cur.execute(
                    f"SELECT local_id, server_id, local_type, real_sender_id, create_time, message_content "
                    f"FROM {table_name} WHERE local_id > ? ORDER BY local_id",
                    (chat_last_seq,)
                )
                rows = cur.fetchall()
            except Exception as e:
                continue
            
            if not rows:
                continue
            
            for (lid, server_id, ltype, sender_id, ctime, content) in rows:
                last_seq_per_chat[table_hash] = max(last_seq_per_chat.get(table_hash, 0), lid)
                
                # 只处理文本消息(local_type=1)
                if ltype != 1:
                    continue
                
                # 跳过已处理的
                if server_id and server_id in seen_ids:
                    continue
                
                # 解码消息内容
                if isinstance(content, bytes):
                    non_text = sum(1 for b in content if b < 0x20 and b not in (0x0a, 0x0d, 0x09))
                    if len(content) > 0 and non_text / len(content) > 0.3:
                        continue
                    content = content.decode('utf-8', errors='replace')
                if not content:
                    continue
                
                # 解析发送者
                sender_display = uname2display.get(sender_id, name_map.get(sender_id, f'id_{sender_id}'))
                is_self = (sender_id == my_wxid)
                
                # 清理消息体
                body = content
                # 去掉发送者名字前缀
                if sender_display:
                    for pfx in [sender_display + ':', sender_display + '：']:
                        if body.startswith(pfx):
                            body = body[len(pfx):].strip()
                            break
                # 处理wxid前缀格式
                if ':' in body[:50] and '\n' in body[:80]:
                    parts = body.split('\n', 1)
                    pre = parts[0].rstrip(':')
                    if pre.startswith('wxid_') and len(parts) > 1:
                        body = parts[1].strip()
                
                # 检测@我
                at_me = f'@{my_wxid}' in body
                if not at_me:
                    for name in [sender_display]:
                        if name and f'@{name}' in body:
                            at_me = True
                            break
                
                # 跳过自己的消息
                if is_self:
                    if server_id:
                        seen_ids.add(server_id)
                    continue
                
                if server_id:
                    seen_ids.add(server_id)
                
                ts = datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M:%S') if ctime else ''
                
                result.append({
                    'local_id': lid,
                    'sender': sender_display,
                    'sender_id': sender_id,
                    'content': body,
                    'is_at': at_me,
                    'chat_name': chat_display,
                    'chat_hash': table_hash,
                    'timestamp': ts,
                    'is_group': is_group,
                })
        
        conn.close()
        conn = None
    except Exception as e:
        print(f'[ERROR] _do_query: {e}')
        traceback.print_exc()
    finally:
        if conn:
            try: conn.close()
            except: pass
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass
    
    result.sort(key=lambda x: x['local_id'])
    return result


# ============ Poll状态管理 ============

class PollState:
    """管理轮询状态（持久化到JSON文件）"""
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.last_seq_per_chat = {}
        self.seen_server_ids = set()
        self.load()
    
    def load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.last_seq_per_chat = data.get('last_seq_per_chat', {})
                self.seen_server_ids = set(data.get('seen_server_ids', []))
            except:
                pass
    
    def save(self):
        data = {
            'last_seq_per_chat': self.last_seq_per_chat,
            'seen_server_ids': list(self.seen_server_ids)[-5000:],
        }
        os.makedirs(os.path.dirname(self.state_file) or '.', exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def update(self, last_seq_per_chat: dict, seen_server_ids: set):
        self.last_seq_per_chat = last_seq_per_chat
        self.seen_server_ids = seen_server_ids
        self.save()


# ============ 简易测试 ============

if __name__ == '__main__':
    print("db.py - 微信数据库查询模块")
    print("用法: 被 bot.py import 使用，不单独运行")
    print(f"wechat-decrypt路径: {WECHAT_DECRYPT_DIR}")
    print(f"路径存在: {WECHAT_DECRYPT_DIR.exists()}")
