#!/usr/bin/env python3
"""manage-assistant provisioner v5 (installed by hermes-app via the default agent).
每个动作打印 [[HAM:BEGIN]]{json}[[HAM:END]] 供 app 解析。
v4: 每个助手 = 独立 profile + 独立常驻网关(独立 api_server 端口, 复用同一 API key, 关消息平台)。
v5: 加"接管已有 bot"(discover/adopt)：纳入用户在 App 之外建的 profile 或独立实例
    (如带飞书的 bot2, HERMES_HOME=~/.hermes-bot2)。接管=给它开 api_server(复用 master key)
    并【保留】消息平台 token(与 create 相反)，再登记进注册表。接管记录 adopted=true：
    删除只从注册表注销(不删实例/不停网关)，禁止 stop(避免断飞书)。状态检查按各自 home。"""
import json, os, re, sys, signal, subprocess, time, urllib.request
from pathlib import Path

VERSION = 5
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

# ---- adopt(接管已有 bot) 辅助 ----
def read_pid_home(home):
    # 接管的独立实例 pid 文件在它自己的 HERMES_HOME 下，不在 profiles/<name>。
    try: raw = (Path(home) / "gateway.pid").read_text("utf-8").strip()
    except Exception: return 0
    if not raw: return 0
    try: return int(json.loads(raw)["pid"]) if raw.startswith("{") else int(raw)
    except Exception: return 0

def list_gateways():
    # 扫所有在跑的 hermes gateway 进程, 读它们的 HERMES_HOME(经 /proc)。
    out = []
    proc = Path("/proc")
    if not proc.exists(): return out
    for p in proc.iterdir():
        if not p.name.isdigit(): continue
        try: cmd = (p / "cmdline").read_bytes().replace(bytes([0]), b" ").decode("utf-8", "ignore")
        except Exception: continue
        if "gateway" not in cmd or "hermes" not in cmd: continue
        home = ""
        try:
            for e in (p / "environ").read_bytes().split(bytes([0])):
                if e.startswith(b"HERMES_HOME="):
                    home = e.split(b"=", 1)[1].decode("utf-8", "ignore").rstrip("/"); break
        except Exception: pass
        if home: out.append({"pid": int(p.name), "home": home})
    return out

def detect_api_port(home):
    # 该实例已配置的 api_server 端口：先 .env, 再 config.yaml(粗解析, 不引 yaml 依赖)。
    envf = Path(home) / ".env"
    if envf.exists():
        for line in envf.read_text("utf-8", "ignore").splitlines():
            stt = line.strip()
            if stt.startswith("API_SERVER_PORT="):
                v = stt.split("=", 1)[1].strip().strip('"').strip("'")
                if v.isdigit(): return int(v)
    cfg = Path(home) / "config.yaml"
    if cfg.exists():
        in_api = False
        for line in cfg.read_text("utf-8", "ignore").splitlines():
            stt = line.strip()
            indent = len(line) - len(line.lstrip())
            if stt.startswith("api_server:"): in_api = True; continue
            if in_api:
                if stt and indent == 0: in_api = False
                elif stt.startswith("port:"):
                    v = stt.split(":", 1)[1].strip().strip('"').strip("'")
                    if v.isdigit(): return int(v)
    return 0

def detect_platforms(home):
    # 仅用于展示:粗扫 .env/config.yaml 里出现过的消息平台前缀。
    found = set()
    for fn in (".env", "config.yaml"):
        f = Path(home) / fn
        if not f.exists(): continue
        up = f.read_text("utf-8", "ignore").upper()
        for pref in PLATFORM_PREFIXES:
            if pref in up: found.add(pref.rstrip("_"))
    return sorted(found)

def rewrite_env_adopt(home, port, key):
    # 接管:开 api_server, 复用 master key——但【保留】消息平台 token(飞书等)。只替换 API_SERVER_*。
    env = Path(home) / ".env"
    keep = []
    if env.exists():
        for line in env.read_text("utf-8", "ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"): keep.append(line); continue
            k = s.split("=", 1)[0].strip()
            if k.startswith("API_SERVER_"): continue
            keep.append(line)
    keep += ["API_SERVER_ENABLED=true", "API_SERVER_KEY=" + key,
        "API_SERVER_HOST=0.0.0.0", "API_SERVER_PORT=" + str(port)]
    env.write_text(chr(10).join(keep) + chr(10), encoding="utf-8")

def start_gateway_home(home):
    # 按指定 HERMES_HOME 拉起网关(接管的独立实例 home 不在 profiles/ 下)。
    LOGDIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    nm = Path(home).name
    log = open(LOGDIR / (nm + ".log"), "ab")
    subprocess.Popen(["hermes", "gateway", "run", "--replace"], env=env,
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, cwd=home)

# ---- actions ----
def act_status():
    emit({"ok": True, "installed": True, "version": VERSION, "count": len(reg_load()["assistants"])})

def act_list():
    reg = reg_load()
    for a in reg["assistants"]:
        home = a.get("home") or str(profile_dir(a["name"]))
        pid = read_pid_home(home)
        a["status"] = "running" if (pid and pid_alive(pid)) else "stopped"
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
    if rec.get("adopted") and rec.get("home"): start_gateway_home(rec["home"])
    else: start_gateway(name)
    ok = wait_health(int(rec.get("port") or 0), read_key(), 40)
    rec["status"] = "running" if ok else "starting"
    reg_save(reg)
    emit({"ok": True, "name": name, "port": rec.get("port"), "status": rec["status"]})

def act_stop(name):
    reg = reg_load()
    rec = next((a for a in reg["assistants"] if a["name"] == name), None)
    if not rec: fail("未找到助手 " + name)
    # 接管的已有 bot:停它的网关会断开飞书等消息平台, 禁止。移出请用 delete(仅注销)。
    if rec.get("adopted"): fail("该助手是接管的已有 bot, 停止其网关会断开飞书等消息平台, 已禁止")
    stop_gateway(name)
    time.sleep(1)
    rec["status"] = "stopped"; reg_save(reg)
    emit({"ok": True, "name": name, "status": "stopped"})

def act_delete(name):
    reg = reg_load()
    rec = next((a for a in reg["assistants"] if a["name"] == name), None)
    # 接管的已有 bot:只从 App 注册表注销, 绝不停网关/删实例(那会断飞书、毁用户的 bot)。
    if rec and rec.get("adopted"):
        reg["assistants"] = [a for a in reg["assistants"] if a["name"] != name]
        reg_save(reg)
        emit({"ok": True, "name": name, "unregistered": True})
    stop_gateway(name)
    time.sleep(1)
    run(["hermes", "profile", "delete", name, "-y"])
    reg["assistants"] = [a for a in reg["assistants"] if a["name"] != name]
    reg_save(reg)
    emit({"ok": True, "name": name})

def act_discover():
    # 列出 App 还没纳管的 bot:① 默认 ~/.hermes/profiles 下未登记的 profile;
    # ② 在跑的独立实例(各自 HERMES_HOME, 经 /proc 找)。供"接管"一键纳入。
    reg = reg_load()
    known = set(a["name"] for a in reg["assistants"])
    seen_homes = set([str(HOME).rstrip("/")])
    for a in reg["assistants"]:
        seen_homes.add((a.get("home") or str(profile_dir(a["name"]))).rstrip("/"))
    cands = []
    if PROFILES.exists():
        for d in sorted(PROFILES.iterdir()):
            if not d.is_dir() or d.name in known: continue
            seen_homes.add(str(d).rstrip("/"))
            pid = read_pid_home(str(d))
            cands.append({"name": d.name, "home": str(d), "kind": "profile",
                "api_port": detect_api_port(str(d)), "platforms": detect_platforms(str(d)),
                "gateway_running": bool(pid and pid_alive(pid))})
    for g in list_gateways():
        home = g["home"].rstrip("/")
        if not home or home in seen_homes: continue
        if any(c["home"].rstrip("/") == home for c in cands): continue
        base = Path(home).name
        nm = base[len(".hermes-"):] if base.startswith(".hermes-") else base.lstrip(".")
        if not NAME_RE.match(nm): nm = re.sub("[^a-z0-9_-]", "-", nm.lower()) or "bot"
        seen_homes.add(home)
        cands.append({"name": nm, "home": home, "kind": "standalone", "pid": g["pid"],
            "api_port": detect_api_port(home), "platforms": detect_platforms(home),
            "gateway_running": True})
    emit({"ok": True, "candidates": cands})

def act_adopt(p):
    # 接管一个已有 bot:开 api_server(复用 master key), 【保留】其消息平台 token, 重启网关, 登记。
    home = (p.get("home") or "").strip().rstrip("/")
    name = (p.get("name") or "").strip()
    if not NAME_RE.match(name): fail("名字不合法(小写字母/数字/连字符, 字母或数字开头)")
    if not home or not Path(home).exists(): fail("实例目录不存在: " + str(home))
    reg = reg_load()
    if any(a["name"] == name for a in reg["assistants"]): fail("已登记同名助手 " + name)
    if any((a.get("home") or "").rstrip("/") == home for a in reg["assistants"]): fail("该实例已被接管")
    key = read_key()
    if not key: fail("默认网关未配置 API_SERVER_KEY, 无法复用鉴权")
    port = detect_api_port(home) or next_port(reg)
    rewrite_env_adopt(home, port, key)
    start_gateway_home(home)
    ok = wait_health(port, key, 40)
    soul = ""
    sp = Path(home) / "SOUL.md"
    if sp.exists():
        try: soul = sp.read_text("utf-8", "ignore").strip()[:40]
        except Exception: pass
    rec = {"name": name, "model": "", "port": port, "host": "0.0.0.0",
        "status": "running" if ok else "starting", "soul_preview": soul,
        "home": home, "adopted": True, "platforms": detect_platforms(home)}
    reg["assistants"].append(rec); reg_save(reg)
    emit({"ok": True, "name": name, "port": port, "status": rec["status"], "adopted": True})

def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if a == "status": act_status()
        elif a == "list": act_list()
        elif a == "create": act_create(json.loads(Path(sys.argv[2]).read_text("utf-8")))
        elif a == "start": act_start(sys.argv[2])
        elif a == "stop": act_stop(sys.argv[2])
        elif a == "delete": act_delete(sys.argv[2])
        elif a == "discover": act_discover()
        elif a == "adopt": act_adopt(json.loads(Path(sys.argv[2]).read_text("utf-8")))
        else: fail("未知动作 " + a)
    except Exception as e: fail(e)

if __name__ == "__main__": main()
