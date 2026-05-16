import sys, os, json, importlib

script_dir = r"F:\dev\GenericAgent"
proj = os.path.join(script_dir, 'temp', 'wechat_reflect_project')
os.environ.setdefault('WECHAT_REFLECT_API_KEY', 'ga_bridge')
for p in [proj, os.path.join(proj, 'wechat-decrypt'), os.path.join(script_dir, 'memory')]:
    if p not in sys.path:
        sys.path.insert(0, p)

wr = importlib.import_module('wechat_reflect')
cfg_path = os.pa
...[Truncated]...
)
finally:
    os.chdir(old_cwd)
