#!/usr/bin/env python3
"""hermes-app-gateway skill —— 在 Hermes 主机上部署/管理"统一网关"。

明文 skill：本目录(scripts/)下的 gateway.py / provision.py 都是可逐行审计的明文源码，
install 时把它们复制到运行位置并起常驻进程。运行时的登录/用户/助手/会话全部走网关
HTTP，不再经过 LLM。

每个动作打印 [[HAM:BEGIN]]{json}[[HAM:END]]，供 app / 你解析。

动作：
  status   探测网关是否安装 / 在跑 / 版本 / 端口
  detect   侦测网络环境（本地IP / 公网IP / NAT / hermes 是否在跑）
  install  复制网关+provisioner、装依赖、注入 master key、生成 JWT 密钥与
           owner 一次性认领令牌、起常驻进程、/health 自检；返回连接信息
  restart  重启网关进程
  stop     停止网关进程
  info     返回连接信息（地址/端口/版本/是否已认领 owner）
"""
import json, os, signal, socket, subprocess, sys, time
import urllib.request, urllib.error
from pathlib import Path

# 确保 stdout 为 UTF-8：HAM 结果可能含中文，避免在非 UTF-8 终端崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_VERSION = 1
GATEWAY_VERSION = "2.0.0"

# 本 skill 自带的明文源码（与本文件同目录），install 时复制到运行位置
SCRIPT_DIR = Path(__file__).resolve().parent
GATEWAY_SRC = SCRIPT_DIR / "gateway.py"
PROVISION_SRC = SCRIPT_DIR / "provision.py"

GW_DIR = Path.home() / ".hermes-gateway"
GW_SCRIPT = GW_DIR / "hermes_gateway.py"
CONFIG_PATH = GW_DIR / "config.json"
DB_PATH = GW_DIR / "gateway.db"
PID_PATH = GW_DIR / "gateway.pid"
LOG_PATH = GW_DIR / "gateway.log"
PROVISION_PATH = Path.home() / ".hermes" / "skills" / "manage-assistant" / "provision.py"
DEFAULT_PORT = 8443
HERMES_PORT = 8642


def emit(o):
    print("[[HAM:BEGIN]]" + json.dumps(o, ensure_ascii=False) + "[[HAM:END]]")
    sys.exit(0)


def fail(m):
    emit({"ok": False, "error": str(m)})


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def gateway_port():
    return int(load_config().get("gateway_port", DEFAULT_PORT))


def read_pid():
    try:
        return int(PID_PATH.read_text("utf-8").strip())
    except Exception:
        return 0


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def health(port, timeout=2):
    """返回网关 /health 的 JSON（成功）或 None。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode())
    except Exception:
        pass
    return None


def port_listening(port, timeout=2):
    """端口是否有 HTTP 服务在听（4xx/5xx 也算在听）。"""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def public_ip():
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=4).read().decode().strip()
    except Exception:
        return None


def read_master_key():
    """从 hermes 的 .env 读 master key（API_SERVER_KEY）。master key 不出服务器。"""
    k = os.environ.get("API_SERVER_KEY", "").strip()
    if k:
        return k
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            if line.strip().startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _ensure_deps():
    """缺啥装啥（装到当前解释器，确保与跑网关的 python 一致）；bcrypt 可选。"""
    need = {"flask": "flask", "flask_cors": "flask-cors", "jwt": "pyjwt", "requests": "requests"}
    missing = []
    for mod, pkg in need.items():
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", *missing],
                       capture_output=True, text=True)
    try:
        __import__("bcrypt")
    except Exception:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "bcrypt"],
                       capture_output=True, text=True)


def _stop_gateway():
    pid = read_pid()
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
        except Exception:
            pass


def _start_gateway(port):
    GW_DIR.mkdir(parents=True, exist_ok=True)
    _stop_gateway()
    log = open(LOG_PATH, "ab")
    # 用 sys.executable 跑网关，保证依赖环境与 _ensure_deps 装的一致；脱离会话常驻。
    p = subprocess.Popen([sys.executable, str(GW_SCRIPT)], env=dict(os.environ),
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(GW_DIR))
    PID_PATH.write_text(str(p.pid), encoding="utf-8")


def _wait_health(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = health(port)
        if h:
            return h
        time.sleep(1)
    return None


def _ensure_owner_token(port):
    """生成/复用 owner 一次性认领令牌，直接写网关 settings 表。已认领则返回 None。"""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='owner_claimed'").fetchone()
            if row and row[0] == '1':
                return None
            row = conn.execute("SELECT value FROM settings WHERE key='owner_claim_token'").fetchone()
            if row and row[0]:
                return row[0]  # 复用未使用的令牌（install 幂等）
            import secrets
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('owner_claim_token', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token,))
            conn.commit()
            return token
        finally:
            conn.close()
    except Exception:
        return None


# ---- actions ----

def act_status():
    port = gateway_port()
    h = health(port)
    installed = GW_SCRIPT.exists()
    running = bool(h) or pid_alive(read_pid())
    emit({"ok": True, "installed": installed, "running": running,
          "skill_version": SKILL_VERSION,
          "version": (h or {}).get("version") if h else (GATEWAY_VERSION if installed else None),
          "port": port,
          "owner_claimed": (h or {}).get("owner_claimed"),
          "hermes_connected": (h or {}).get("hermes_connected")})


def act_detect():
    lip = local_ip()
    pip = public_ip()
    has_nat = True
    if lip and pip:
        has_nat = not pip.startswith(lip.split(".")[0])
    emit({"ok": True, "local_ip": lip, "public_ip": pip, "has_nat": has_nat,
          "hermes_running": port_listening(HERMES_PORT),
          "gateway_running": bool(health(gateway_port())),
          "gateway_port": gateway_port()})


def act_install():
    GW_DIR.mkdir(parents=True, exist_ok=True)
    # 1) 复制网关 + provisioner（从本 skill scripts/ 下的明文源）
    if not GATEWAY_SRC.exists() or not PROVISION_SRC.exists():
        fail("skill 不完整：scripts/ 下缺少 gateway.py 或 provision.py")
    GW_SCRIPT.write_text(GATEWAY_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    PROVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROVISION_PATH.write_text(PROVISION_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        os.chmod(GW_SCRIPT, 0o755)
        os.chmod(PROVISION_PATH, 0o755)
    except Exception:
        pass

    # 2) master key（不出服务器）
    key = read_master_key()
    if not key:
        fail("未找到 hermes 的 API_SERVER_KEY（~/.hermes/.env），无法配置网关代理")

    # 3) 读/建配置：端口、hermes 地址、JWT 密钥（已有则保留）
    cfg = load_config()
    cfg.setdefault("gateway_port", DEFAULT_PORT)
    cfg.setdefault("hermes_url", f"http://127.0.0.1:{HERMES_PORT}")
    cfg["hermes_api_key"] = key
    if not cfg.get("jwt_secret"):
        import secrets
        cfg["jwt_secret"] = secrets.token_hex(32)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    port = int(cfg["gateway_port"])

    # 4) 装依赖 + 起常驻进程 + 自检
    _ensure_deps()
    _start_gateway(port)
    h = _wait_health(port, 30)
    if not h:
        fail("网关已启动但 /health 未在 30s 内通过，请查看 " + str(LOG_PATH))

    # 5) owner 一次性认领令牌（仅在尚未认领时发放）
    claim_token = None if h.get("owner_claimed") else _ensure_owner_token(port)

    out = {"ok": True, "installed": True, "running": True,
           "skill_version": SKILL_VERSION, "version": h.get("version"),
           "port": port, "local_ip": local_ip(),
           "owner_claimed": h.get("owner_claimed", False)}
    if claim_token:
        out["owner_claim_token"] = claim_token
    emit(out)


def act_restart():
    port = gateway_port()
    if not GW_SCRIPT.exists():
        fail("网关未安装，请先运行 install")
    _start_gateway(port)
    h = _wait_health(port, 30)
    if not h:
        fail("网关重启后 /health 未在 30s 内通过，请查看 " + str(LOG_PATH))
    emit({"ok": True, "running": True, "port": port, "version": h.get("version")})


def act_stop():
    _stop_gateway()
    try:
        PID_PATH.unlink()
    except Exception:
        pass
    emit({"ok": True, "running": False})


def act_info():
    port = gateway_port()
    h = health(port) or {}
    emit({"ok": True, "installed": GW_SCRIPT.exists(), "running": bool(h),
          "skill_version": SKILL_VERSION, "version": h.get("version"),
          "port": port, "local_ip": local_ip(),
          "owner_claimed": h.get("owner_claimed"),
          "open_registration": h.get("open_registration"),
          "hermes_connected": h.get("hermes_connected")})


def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if a == "status":
            act_status()
        elif a == "detect":
            act_detect()
        elif a == "install":
            act_install()
        elif a == "restart":
            act_restart()
        elif a == "stop":
            act_stop()
        elif a == "info":
            act_info()
        else:
            fail("未知动作 " + a)
    except SystemExit:
        raise
    except Exception as e:
        fail(e)


if __name__ == "__main__":
    main()
