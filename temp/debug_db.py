
import sys, os, shutil, re
sys.path.insert(0, r'F:\dev\GenericAgent\temp\wechat_reflect_project')

# 手动读.env
env = {}
with open(r'F:\dev\GenericAgent\temp\wechat_reflect_project\.env', 'r') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
for k, v in env.items():
    os.environ[k] = v

MSG_DB_KEY_HEX = os.environ.get('WECHAT_REFLECT_DB_KEY', '')
print(f"KEY存在: {bool(MSG_DB_KEY_HEX)}, len={len(MSG_DB_KEY_HEX)}")

from monitor import decrypt_db_to_sqlite

# 找DB路径
db_path = None
appdata = os.path.expandvars(r'%APPDATA%\Tencent\WeChat')
for root, dirs, files in os.walk(appdata):
    for f in files:
        if f == 'message_0.db':
            db_path = os.path.join(root, f)
            break
    if db_path:
        break

print(f"DB: {db_path}")
if not db_path or not MSG_DB_KEY_HEX:
    sys.exit(1)

key = bytes.fromhex(MSG_DB_KEY_HEX)
db_copy = db_path + '.debug_tmp'
shutil.copy2(db_path, db_copy)
conn, tmp = decrypt_db_to_sqlite(db_copy, key)
cur = conn.cursor()

# 列出所有Msg_表
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
tables = [r[0] for r in cur.fetchall()]
print(f"Msg_表数量: {len(tables)}")

# 找Kelegele群 - 查每个表最新消息看有没有目标内容
target_hash = os.environ.get('WECHAT_REFLECT_TARGET_HASH', '')
print(f"target_hash: {target_hash[:20] if target_hash else '未设置'}")

# 查所有表找包含"好的，听好了"的消息
for t in tables:
    try:
        cur.execute(f"SELECT local_id, local_type, message_content FROM {t} ORDER BY local_id DESC LIMIT 5")
        for lid, lt, content in cur.fetchall():
            if isinstance(content, bytes):
                txt = content.decode('utf-8', errors='replace')[:200]
            else:
                txt = str(content)[:200] if content else ''
            if '好的' in txt or '听好了' in txt or '很久很久以前' in txt:
                print(f"\n*** 找到! table={t} id={lid} type={lt}")
                print(f"    content: {repr(txt[:150])}")
    except:
        pass

# 同时看 target_hash 表
if target_hash:
    full_table = f'Msg_{target_hash}'
    print(f"\n=== 目标表 {full_table} 最新15条 ===")
    cur.execute(f"SELECT local_id, local_type, message_content FROM {full_table} ORDER BY local_id DESC LIMIT 15")
    for lid, lt, content in cur.fetchall():
        if isinstance(content, bytes):
            txt = content.decode('utf-8', errors='replace')[:100]
        else:
            txt = str(content)[:100] if content else '(null)'
        print(f"  id={lid} type={lt}: {repr(txt)}")

conn.close()
if tmp and os.path.exists(tmp): os.unlink(tmp)
if os.path.exists(db_copy): os.unlink(db_copy)
