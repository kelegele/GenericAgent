"""
wxchatbot DB模块 - 微信数据库解密+消息查询
从wechat_reflect_project提取核心逻辑，去除LLM/UI依赖
依赖: wechat-decrypt/monitor.py (decrypt_db_to_sqlite)
"""
import os, sys, sqlite3, json, re, threading, traceback, time
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
        self.my_display_name = config.get('my_display_name', '')
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


def classify_chat_table(cur, table_name, name_map, uname2display, my_wxid_base) -> dict:
    """
    按规则判定Msg表的聊天类型，返回:
    {
        'chat_type': 'group'|'private'|'self'|'unknown',
        'chat_name': 显示名（如"嚟广州一齐屙💩"或"飞栗"）,
        'chat_hash': 表名hash缩写,
        'peer_wxid': 对方wxid（私聊）或chatroom（群聊）,
        'sender_id_list': distinct real_sender_id列表,
    }
    """
    import re
    table_hash = table_name.replace('Msg_', '').lower()
    hash_short = table_hash[:4] + '...' + table_hash[-4:]
    
    try:
        # 1. 统计总行数和 distinct sender
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        total = cur.fetchone()[0]
        
        cur.execute(
            f"SELECT DISTINCT real_sender_id FROM {table_name} "
            f"WHERE real_sender_id != '' AND real_sender_id != 0"
        )
        sender_ids = [r[0] for r in cur.fetchall()]
        
        # 2. 采样检查 message_content 是否带 wxid_xxx: 前缀
        cur.execute(
            f"SELECT message_content FROM {table_name} "
            f"WHERE message_content IS NOT NULL AND message_content != '' "
            f"LIMIT 50"
        )
        has_wxid_prefix = False
        peer_wxid = None
        for (content,) in cur.fetchall():
            if not isinstance(content, str):
                continue
            # 群聊格式: wxid_xxx:\n内容  或  wxid_xxx:内容
            m = re.match(r'^(wxid_[a-zA-Z0-9_]+)\s*:\s*\n?(.+)', content, re.DOTALL)
            if m:
                has_wxid_prefix = True
                break
        
        # 3. 按规则判定
        is_self_wxid = lambda wxid: wxid and (wxid == my_wxid_base or wxid.startswith(my_wxid_base))
        
        if has_wxid_prefix:
            # 规则1: 群聊 — content带wxid_xxx前缀
            # 会话名: 尝试从群成员wxid找chatroom名
            # 先收集所有涉及的wxid（从content提取）
            cur.execute(
                f"SELECT message_content FROM {table_name} "
                f"WHERE message_content IS NOT NULL AND message_content != '' "
                f"LIMIT 200"
            )
            group_wxids = set()
            for (content,) in cur.fetchall():
                if isinstance(content, str):
                    for m in re.finditer(r'(wxid_[a-zA-Z0-9_]+)\s*:', content):
                        group_wxids.add(m.group(1))
            
            # 会话名: 从group_wxids中找非自己的wxid，映射uname2display
            # 群聊的会话名需要从contact.db的@chatroom获取，这里用hash_short兜底
            chat_name = hash_short
            peer_wxid = None
            
            # 策略1: 从sender_ids中找@chatroom名
            for sid in sender_ids:
                wxid = name_map.get(sid, '')
                if '@chatroom' in wxid:
                    display = uname2display.get(wxid, '')
                    if display:
                        chat_name = display
                        peer_wxid = wxid
                        break
            
            # 策略2: MD5匹配 — 遍历Name2Id中所有@chatroom条目，MD5(username)匹配table_hash
            if peer_wxid is None:
                import hashlib
                for rowid, username in name_map.items():
                    if '@chatroom' in str(username):
                        if hashlib.md5(username.encode()).hexdigest() == table_hash:
                            display = uname2display.get(username, '')
                            if display:
                                chat_name = display
                            peer_wxid = username
                            break
            
            return {
                'chat_type': 'group',
                'chat_name': chat_name,
                'chat_hash': hash_short,
                'peer_wxid': peer_wxid,
                'sender_id_list': sender_ids,
            }
        
        elif total >= 20 and len(sender_ids) == 1:
            # 规则3: 自聊
            return {
                'chat_type': 'self',
                'chat_name': hash_short,
                'chat_hash': hash_short,
                'peer_wxid': name_map.get(sender_ids[0], '') if sender_ids else '',
                'sender_id_list': sender_ids,
            }
        
        elif total >= 20 and len(sender_ids) == 2:
            # 规则2: 私聊
            peer_wxid = ''
            peer_display = ''
            for sid in sender_ids:
                wxid = name_map.get(sid, '')
                if wxid and not is_self_wxid(wxid):
                    peer_wxid = wxid
                    peer_display = uname2display.get(wxid, wxid)
                    break
            return {
                'chat_type': 'private',
                'chat_name': peer_display or hash_short,
                'chat_hash': hash_short,
                'peer_wxid': peer_wxid,
                'sender_id_list': sender_ids,
            }
        
        else:
            # 规则4: 未知（<20条）
            return {
                'chat_type': 'unknown',
                'chat_name': hash_short,
                'chat_hash': hash_short,
                'peer_wxid': '',
                'sender_id_list': sender_ids,
            }
    
    except Exception as e:
        return {
            'chat_type': 'unknown',
            'chat_name': hash_short,
            'chat_hash': hash_short,
            'peer_wxid': '',
            'sender_id_list': [],
        }


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
    my_display_name: str = '',
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
                    my_wxid, uname2display, target_hash, my_display_name
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
    my_wxid, uname2display, target_hash, my_display_name=''
) -> list:
    """实际查询逻辑（内部函数）"""
    conn = tmp = None
    result = []
    
    try:
        conn, tmp = decrypt_db_to_sqlite(db_path, key)
        cur = conn.cursor()
        
        main_tables = get_all_main_msg_tables(cur)
        
        # Name2Id映射: rowid → user_name(wxid/chatroom)
        name_map = {}
        try:
            cur.execute("SELECT rowid, * FROM Name2Id")
            for r in cur.fetchall():
                name_map[r[0]] = r[1]
        except:
            pass
        
        # my_wxid可能带后缀(如_9763)，但DB里存的是不带后缀的wxid，需要做前缀匹配
        my_wxid_base = my_wxid.rsplit('_', 1)[0] if '_' in my_wxid else my_wxid
        def is_self_wxid(wxid):
            if not wxid:
                return False
            return wxid == my_wxid or wxid == my_wxid_base
        
        for table_name in main_tables:
            table_hash = table_name.replace('Msg_', '').lower()
            
            if target_hash and table_hash != target_hash:
                continue
            
            # === 新规则：用 classify_chat_table 判定聊天类型 ===
            chat_info = classify_chat_table(cur, table_name, name_map, uname2display, my_wxid_base)
            chat_type = chat_info['chat_type']    # 'group'/'private'/'self'/'unknown'
            chat_display = chat_info['chat_name'] # 会话显示名
            chat_hash_short = chat_info['chat_hash']  # hash缩写
            is_group = (chat_type == 'group')
            
            # 自聊和未知类型：跳过，不处理
            if chat_type in ('self', 'unknown'):
                # 仍然更新last_seq，避免下次重复扫描
                chat_last_seq = last_seq_per_chat.get(table_hash, 0)
                try:
                    cur.execute(f"SELECT MAX(local_id) FROM {table_name}")
                    max_id = cur.fetchone()[0]
                    if max_id:
                        last_seq_per_chat[table_hash] = max(last_seq_per_chat.get(table_hash, 0), max_id)
                except:
                    pass
                continue
            
            # === 已回复检测：查最新一条消息判断是否已回复 ===
            chat_already_replied = False
            try:
                cur.execute(
                    f"SELECT real_sender_id, message_content FROM {table_name} "
                    f"ORDER BY local_id DESC LIMIT 1"
                )
                last_row = cur.fetchone()
                if last_row:
                    last_sender_id, last_content = last_row[0], last_row[1]
                    last_sender_wxid = name_map.get(last_sender_id, '')
                    
                    if is_group:
                        # 群聊：最新一条content不带wxid前缀 = 自己发的 = 已回复
                        if isinstance(last_content, str):
                            import re as _re2
                            has_wxid_pfx = _re2.match(r'^wxid_[a-zA-Z0-9_]+\s*:', last_content)
                            if not has_wxid_pfx and is_self_wxid(last_sender_wxid):
                                chat_already_replied = True
                    else:
                        # 私聊：最新一条的real_sender_id是自己 = 已回复
                        if is_self_wxid(last_sender_wxid):
                            chat_already_replied = True
            except:
                pass
            
            chat_last_seq = last_seq_per_chat.get(table_hash, 0)
            
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
                
                # === 解析发送者 ===
                sender_wxid = name_map.get(sender_id, str(sender_id))
                sender_display = uname2display.get(sender_wxid, uname2display.get(str(sender_id), f'id_{sender_id}'))
                is_self = is_self_wxid(sender_wxid)
                
                # === 清理消息体 ===
                body = content
                
                if is_group:
                    # 群聊：content格式为 "wxid_xxx:\n内容" 或 "wxid_xxx:内容"
                    import re as _re
                    m = _re.match(r'^(wxid_[a-zA-Z0-9_]+)\s*:\s*\n?(.*)', body, re.DOTALL)
                    if m:
                        group_sender_wxid = m.group(1)
                        body = m.group(2).strip()
                        # 用群消息中的wxid作为实际发送者（覆盖sender_id）
                        group_sender_display = uname2display.get(group_sender_wxid, group_sender_wxid)
                        sender_wxid = group_sender_wxid
                        sender_display = group_sender_display
                        is_self = is_self_wxid(sender_wxid)
                else:
                    # 私聊：去掉发送者名字前缀（如果有）
                    if sender_display:
                        for pfx in [sender_display + ':', sender_display + '：']:
                            if body.startswith(pfx):
                                body = body[len(pfx):].strip()
                                break
                
                # 检测@我 — 同时检查wxid和配置的显示名
                at_me = f'@{my_wxid}' in content or f'@{my_wxid}' in body
                if not at_me and my_display_name:
                    at_me = f'@{my_display_name}' in content or f'@{my_display_name}' in body
                
                if server_id:
                    seen_ids.add(server_id)
                
                ts = datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M:%S') if ctime else ''
                
                result.append({
                    'local_id': lid,
                    'sender': sender_display,
                    'sender_wxid': sender_wxid,
                    'sender_id': sender_id,
                    'content': body,
                    'is_at': at_me,
                    'chat_name': chat_display,
                    'chat_hash': table_hash,
                    'chat_type': chat_type,
                    'chat_hash_short': chat_hash_short,
                    'timestamp': ts,
                    'is_group': is_group,
                    'is_self': is_self,
                    'chat_already_replied': chat_already_replied,
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
