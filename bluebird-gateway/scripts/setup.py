#!/usr/bin/env python3
"""bluebird-gateway skill —— 在 Hermes 主机上部署/管理"统一网关"。

明文 skill：本目录(scripts/)下的 gateway.py / provision.py 都是可逐行审计的明文源码，
install 时把它们复制到运行位置并起常驻进程。运行时的登录/用户/助手/会话全部走网关
HTTP，不再经过 LLM。

每个动作打印 [[HAM:BEGIN]]{json}[[HAM:END]]，供 app / 你解析。

动作：
  status     探测网关是否安装 / 在跑 / 版本 / 端口
  detect     侦测网络环境（本地IP / 公网IP / NAT / hermes 是否在跑）
  install    复制网关+provisioner、装依赖、注入 master key、生成 JWT 密钥与
             owner 一次性认领令牌、起常驻进程、/health 自检；返回连接信息
  restart    重启网关进程
  stop       停止网关进程
  info       返回连接信息（地址/端口/版本/是否已认领 owner）
  tailscale  （可选）用 `tailscale serve` 把本机网关挂上 tailnet（带自动 HTTPS），
             并打印 App 该填的 MagicDNS 地址 / 裸 IP 地址。需先装好 Tailscale。
             用法：setup.py tailscale [authkey]
  tls        配置网关 TLS（建筑块，决策由 agent 按 SKILL.md 编排）：
               tls use-cert <证书PEM> <私钥PEM>  挂上已签发的证书 → 网关直接 HTTPS
               tls off                            关闭 TLS → 回到 HTTP
               tls status                         查看当前 http/https / 证书指纹
             证书一般由 agent 用 acme.sh 经 HTTP-01 / DNS-01 签发后传入。
"""
import json, os, shutil, signal, socket, subprocess, sys, time
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
CERT_DIR = GW_DIR / "certs"
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
    """返回网关 /health 的 JSON（成功）或 None。
    网关可能是纯 HTTP，也可能自带 TLS 跑 HTTPS——两种都试（HTTPS 自检不校验证书，
    因为连的是 127.0.0.1 而证书多半签给域名）。"""
    import ssl
    for url, kw in (
        (f"http://127.0.0.1:{port}/health", {}),
        (f"https://127.0.0.1:{port}/health", {"context": ssl._create_unverified_context()}),
    ):
        try:
            with urllib.request.urlopen(url, timeout=timeout, **kw) as r:
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


PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _can_import(mod):
    """用将来跑网关的同一解释器、以子进程方式确认模块可导入（最可靠）。"""
    return subprocess.run([sys.executable, "-c", "import " + mod],
                          capture_output=True).returncode == 0


def _pip_install(pkgs, mirror=None):
    cmd = [sys.executable, "-m", "pip", "install", "--user", *pkgs]
    if mirror:
        cmd += ["-i", mirror]
    return subprocess.run(cmd, capture_output=True, text=True)


def _ensure_deps():
    """装必需依赖；默认源失败自动换国内镜像；最终仍缺则用明确错误中止（不再静默）。bcrypt 可选。"""
    required = {"flask": "flask", "flask_cors": "flask-cors", "jwt": "pyjwt", "requests": "requests"}
    missing = [pkg for mod, pkg in required.items() if not _can_import(mod)]
    if missing:
        _pip_install(missing)
        if any(not _can_import(mod) for mod in required):  # 默认源没装全 → 换国内镜像重试
            _pip_install(missing, PYPI_MIRROR)
    still = [pkg for mod, pkg in required.items() if not _can_import(mod)]
    if still:
        fail("依赖安装失败，缺少 " + ", ".join(still)
             + "。请手动安装后重试： python3 -m pip install --user -i "
             + PYPI_MIRROR + " " + " ".join(still))
    # bcrypt 可选（C 扩展，装不上会自动回退到标准库 pbkdf2）
    if not _can_import("bcrypt"):
        if _pip_install(["bcrypt"]).returncode != 0:
            _pip_install(["bcrypt"], PYPI_MIRROR)


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


def _have(cmd):
    return bool(shutil.which(cmd))


def _port_free(port):
    """本机该端口当前是否空闲（HTTP-01 临时绑 80 时要用）。仅本机判断，不代表公网可达。"""
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.close()
        return True
    except Exception:
        return False


def act_detect():
    lip = local_ip()
    pip = public_ip()
    has_nat = True
    if lip and pip:
        has_nat = not pip.startswith(lip.split(".")[0])
    # 供 agent 决策 TLS 路径：有哪些签发/隧道工具、80/443 能否本机绑（HTTP-01 用）。
    tools = {t: _have(t) for t in ("openssl", "acme.sh", "certbot", "caddy", "tailscale")}
    emit({"ok": True, "local_ip": lip, "public_ip": pip, "has_nat": has_nat,
          "hermes_running": port_listening(HERMES_PORT),
          "gateway_running": bool(health(gateway_port())),
          "gateway_port": gateway_port(),
          "tools": tools,
          "port80_free": _port_free(80), "port443_free": _port_free(443)})


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


# ---- Tailscale（可选传输方案）----
# 网关本身传输无关：它只在 127.0.0.1 监听，由 `tailscale serve` 把它挂上 tailnet。
# 这个动作只做 Tailscale 侧编排 + 打印 App 该填的地址，不碰核心 install。

def _ts_cli():
    return shutil.which("tailscale") or shutil.which("tailscale.exe")


def _ts_status(ts):
    try:
        r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def act_tailscale():
    ts = _ts_cli()
    if not ts:
        fail("未找到 tailscale 命令。请先安装 Tailscale（https://tailscale.com/download）"
             "并确保它在 PATH 中，再重试。")
    port = gateway_port()
    authkey = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TS_AUTHKEY", "").strip()

    # 1) 确保已登录 tailnet
    st = _ts_status(ts)
    if (st or {}).get("BackendState") != "Running":
        if authkey:
            up = subprocess.run([ts, "up", "--authkey", authkey],
                                capture_output=True, text=True, timeout=90)
            if up.returncode != 0:
                fail("tailscale up 失败：" + (up.stderr or up.stdout or "")[:300])
            st = _ts_status(ts)
        else:
            fail("Tailscale 未登录。请先 `tailscale up`（浏览器登录），或带 auth key 重试："
                 "setup.py tailscale <authkey>")

    # 2) tailscale serve 暴露本机网关（带自动 HTTPS）。
    #    serve 的 flag 因 Tailscale 版本而异；失败时给出手动命令而不是硬中止。
    serve_cmd = [ts, "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"]
    served = subprocess.run(serve_cmd, capture_output=True, text=True, timeout=30)
    serve_ok = served.returncode == 0

    # 3) 取 MagicDNS 名 + tailnet IPv4
    self_node = (st or {}).get("Self", {}) if st else {}
    dns_name = (self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    ip4 = next((x for x in ips if ":" not in x), None)

    out = {
        "ok": True,
        "tailscale_logged_in": True,
        "serve_configured": serve_ok,
        "magicdns": dns_name or None,
        # App「网关地址」该填的值：优先 MagicDNS(https)，否则裸 IP(http，隧道已加密)
        "app_url_magicdns": (f"https://{dns_name}" if (serve_ok and dns_name) else None),
        "app_url_ip": (f"http://{ip4}:{port}" if ip4 else None),
        "gateway_port": port,
        "note": ("MagicDNS(serve) 方案：把网关 config.json 的 bind_host 设为 127.0.0.1 后重启；"
                 "裸 IP 方案：bind_host 保持 0.0.0.0。"),
    }
    if not serve_ok:
        out["serve_hint"] = ("tailscale serve 未成功（可能 flag 因版本不同）。可手动执行："
                             + " ".join(serve_cmd) + "  错误："
                             + (served.stderr or served.stdout or "")[:200])
    emit(out)


# ---- TLS（建筑块；"问用户/发掘/降级"的决策树在 SKILL.md，由 agent 编排）----

def _cfg_set_tls(certfile, keyfile):
    """写 config.json 的 ssl_certfile/ssl_keyfile（None=关闭），其余字段保留。"""
    cfg = load_config()
    cfg["ssl_certfile"] = certfile
    cfg["ssl_keyfile"] = keyfile
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _cert_fingerprint(certfile):
    """证书 SHA256 指纹（供 App 固定/用户核对）；拿不到返回 None。"""
    if not certfile:
        return None
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256",
                            "-in", str(certfile)], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and "=" in r.stdout:
            return r.stdout.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def _restart_verify(port):
    """重启网关并等 /health（http/https 都试）。返回 health dict 或 None。"""
    _start_gateway(port)
    return _wait_health(port, 30)


def act_tls():
    sub = sys.argv[2] if len(sys.argv) > 2 else "status"
    port = gateway_port()
    cfg = load_config()

    if sub == "status":
        on = bool(cfg.get("ssl_certfile") and cfg.get("ssl_keyfile"))
        emit({"ok": True, "tls": "https" if on else "http", "port": port,
              "ssl_certfile": cfg.get("ssl_certfile"), "ssl_keyfile": cfg.get("ssl_keyfile"),
              "fingerprint": _cert_fingerprint(cfg.get("ssl_certfile")) if on else None})

    if sub == "off":
        _cfg_set_tls(None, None)
        if GW_SCRIPT.exists() and not _restart_verify(port):
            fail("关闭 TLS 后 /health 未通过，见 " + str(LOG_PATH))
        emit({"ok": True, "tls": "http", "port": port, "local_ip": local_ip()})

    if sub == "use-cert":
        certfile = sys.argv[3] if len(sys.argv) > 3 else ""
        keyfile = sys.argv[4] if len(sys.argv) > 4 else ""
        if not certfile or not keyfile:
            fail("用法：setup.py tls use-cert <证书PEM> <私钥PEM>")
        cp = Path(certfile).expanduser()
        kp = Path(keyfile).expanduser()
        if not cp.exists() or not kp.exists():
            fail("证书或私钥文件不存在：" + str(cp) + " | " + str(kp))
        try:  # 网关以本用户身份跑，证书必须对它可读
            cp.read_bytes()
            kp.read_bytes()
        except Exception as e:
            fail("证书/私钥不可读（注意运行网关的用户权限）：" + str(e))
        _cfg_set_tls(str(cp), str(kp))
        h = _restart_verify(port)
        if not h:
            fail("启用 HTTPS 后 /health 未通过（证书与私钥不匹配？文件不可读？），见 " + str(LOG_PATH))
        emit({"ok": True, "tls": "https", "port": port, "local_ip": local_ip(),
              "fingerprint": _cert_fingerprint(str(cp)),
              "note": ("已启用 HTTPS；App 网关地址用 https://<你的域名>:%d。"
                       "证书续期后需重跑 setup.py restart（acme.sh 可用 --reloadcmd 自动触发）。") % port})

    fail("未知 tls 子动作：" + sub + "（可用：use-cert / off / status）")


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
        elif a == "tailscale":
            act_tailscale()
        elif a == "tls":
            act_tls()
        else:
            fail("未知动作 " + a)
    except SystemExit:
        raise
    except Exception as e:
        fail(e)


if __name__ == "__main__":
    main()
