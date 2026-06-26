#!/usr/bin/env python3
"""manage-assistant provisioner v4 (installed by hermes-app via the default agent).
每个动作打印 [[HAM:BEGIN]]{json}[[HAM:END]] 供 app 解析。
v4: 每个助手 = 独立 profile + 独立常驻网关(独立 api_server 端口, 复用同一 API key, 关消息平台)。
    修: gateway.pid 是 JSON 记录(非纯整数)；保留默认网关真实端口；加 stop 动作。"""
import json, os, re, sys, signal, subprocess, time, urllib.request
from pathlib import Path

VERSION = 4
HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
PROFILES = HOME / "profiles"
REGISTRY = HOME / "app-assistants.json"
LOGDIR = HOME / "app-gateways"
PORT_BASE = 8650
RESERVED_PORTS = {8642, 9119}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# 克隆 profile 会继承默认的消息平台 token；起网关前按前缀剥掉，否则会重复上线同一 bot。
PLATFORM_PREFIXES = ("TELEGRAM_", "DISCORD_", "SLACK_", "FEISHU_", "WECOM_", "WEIXIN_",
    "WHATSAPP_", "MATRIX_", "SIGNAL_", "MATTERMOST_", "HASS_", "TWILIO_", "DINGTALK_",
    "BLUEBUBBLES_", "MSGRAPH_", "WEBHOOK_", "LINE_", "QQ_", "SMS_", "EMAIL_", "YUANBAO_")

def emit(o):
    print("[[HAM:BEGIN]]" + json.dumps(o, ensure_ascii=False) + "[[HAM:END]]")
    sys.exit(0)
def fail(m): emit({"ok": False, "error": str(m)})
def reg_load():
    try: return json.loads(REGISTRY.read_text("utf-8"))
    except Exception: return {"version": VERSION, "assistants": []}
def reg_save(r):
    r["version"] = VERSION
    REGISTRY.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
def run(c): return subprocess.run(c, capture_output=True, text=True)
def profile_dir(name): return PROFILES / name
def pid_path(name): return profile_dir(name) / "gateway.pid"  # 0.17: {HERMES_HOME}/gateway.pid
def read_pid(name):
    # 0.17 把 gateway.pid 写成 JSON 记录 {"pid":N,...}(非纯整数)；兼容两种格式。
    try: raw = pid_path(name).read_text("utf-8").strip()
    except Exception: return 0
    if not raw: return 0
    try: return int(json.loads(raw)["pid"]) if raw.startswith("{") else int(raw)
    except Exception: return 0

def read_key():
    # provision.py 跑在默认网关的 agent 进程内 → 其 env 就有 API_SERVER_KEY；兜底读默认 .env。
    k = os.environ.get("API_SERVER_KEY", "").strip()
    if k: return k
    env = HOME / ".env"
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            if line.strip().startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

def default_port():
    # 默认网关的真实 api_server 端口(非标准部署可能不是 8642)，也要避开。
    p = os.environ.get("API_SERVER_PORT", "").strip()
    if p.isdigit(): return int(p)
    env = HOME / ".env"
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            if line.strip().startswith("API_SERVER_PORT="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v.isdigit(): return int(v)
    return 8642

def next_port(reg):
    used = set(RESERVED_PORTS)
    used.add(default_port())
    for a in reg["assistants"]:
        if a.get("port"): used.add(int(a["port"]))
    p = PORT_BASE
    while p in used: p += 1
    return p

def h_create(name):
    r = run(["hermes", "profile", "create", name, "--clone"])  # 克隆默认 config/.env/SOUL/skills
    if r.returncode: raise RuntimeError("profile create 失败: " + (r.stderr or r.stdout)[:300])

def write_soul(name, soul):
    (profile_dir(name) / "SOUL.md").write_text(soul or "", encoding="utf-8")

def rewrite_env(name, port, key):
    env = profile_dir(name) / ".env"
    keep = []
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                keep.append(line); continue
            k = s.split("=", 1)[0].strip()
            if k.startswith(PLATFORM_PREFIXES) or k.startswith("API_SERVER_"):
                continue
            keep.append(line)
    keep += ["API_SERVER_ENABLED=true", "API_SERVER_KEY=" + key,
        "API_SERVER_HOST=0.0.0.0", "API_SERVER_PORT=" + str(port)]
    env.write_text(chr(10).join(keep) + chr(10), encoding="utf-8")

def start_gateway(name):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_dir(name))  # 锚定该 profile，避开 -p 的 env 继承歧义
    log = open(LOGDIR / (name + ".log"), "ab")
    subprocess.Popen(["hermes", "gateway", "run", "--replace"], env=env,
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, cwd=str(HOME))  # 脱离 agent 会话常驻

def wait_health(port, key, timeout=25):
    if not port: return False
    req = urllib.request.Request("http://127.0.0.1:" + str(port) + "/health",
        headers={"Authorization": "Bearer " + key})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200: return True
        except Exception: pass
        time.sleep(1)
    return False

def pid_alive(pid):
    try: os.kill(pid, 0); return True
    except Exception: return False

def gateway_running(name):
    pid = read_pid(name)
    return pid_alive(pid) if pid else False

def stop_gateway(name):
    pid = read_pid(name)
    if pid:
        try: os.kill(pid, signal.SIGTERM)
        except Exception: pass

# ---- actions ----
def act_status():
    emit({"ok": True, "installed": True, "version": VERSION, "count": len(reg_load()["assistants"])})

def act_list():
    reg = reg_load()
    for a in reg["assistants"]:
        a["status"] = "running" if gateway_running(a["name"]) else "stopped"
    emit({"ok": True, "assistants": reg["assistants"]})

def act_create(p):
    name = (p.get("name") or "").strip()
    if not NAME_RE.match(name): fail("名字不合法")
    reg = reg_load()
    if any(a["name"] == name for a in reg["assistants"]) or profile_dir(name).exists():
        fail("名字已存在")
    key = read_key()
    if not key: fail("默认网关未配置 API_SERVER_KEY，无法为助手网关复用鉴权")
    port = next_port(reg)
    h_create(name)
    rewrite_env(name, port, key)
    write_soul(name, p.get("soul", ""))
    start_gateway(name)
    # health 没在 40s 内过不代表失败(NAS 上克隆+起 api_server 可能更慢)：进程已起，
    # 标 starting、不删 profile，让 app 提示"还在启动"，避免误删慢启动的有效助手。
    ok = wait_health(port, key, 40)
    rec = {"name": name, "model": p.get("model") or "", "port": port, "host": "0.0.0.0",
        "status": "running" if ok else "starting", "soul_preview": (p.get("soul", "") or "")[:40]}
    reg["assistants"].append(rec); reg_save(reg)
    emit({"ok": True, "name": name, "port": port, "status": rec["status"]})

def act_start(name):
    reg = reg_load()
    rec = next((a for a in reg["assistants"] if a["name"] == name), None)
    if not rec: fail("未找到助手 " + name)
    start_gateway(name)
    ok = wait_health(int(rec.get("port") or 0), read_key(), 40)
    rec["status"] = "running" if ok else "starting"
    reg_save(reg)
    emit({"ok": True, "name": name, "port": rec.get("port"), "status": rec["status"]})

def act_stop(name):
    reg = reg_load()
    rec = next((a for a in reg["assistants"] if a["name"] == name), None)
    if not rec: fail("未找到助手 " + name)
    stop_gateway(name)
    time.sleep(1)
    rec["status"] = "stopped"; reg_save(reg)
    emit({"ok": True, "name": name, "status": "stopped"})

def act_delete(name):
    stop_gateway(name)
    time.sleep(1)
    run(["hermes", "profile", "delete", name, "-y"])
    reg = reg_load()
    reg["assistants"] = [a for a in reg["assistants"] if a["name"] != name]
    reg_save(reg)
    emit({"ok": True, "name": name})

def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if a == "status": act_status()
        elif a == "list": act_list()
        elif a == "create": act_create(json.loads(Path(sys.argv[2]).read_text("utf-8")))
        elif a == "start": act_start(sys.argv[2])
        elif a == "stop": act_stop(sys.argv[2])
        elif a == "delete": act_delete(sys.argv[2])
        else: fail("未知动作 " + a)
    except Exception as e: fail(e)

if __name__ == "__main__": main()
