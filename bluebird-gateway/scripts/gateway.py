#!/usr/bin/env python3
"""
Hermes Gateway - 统一网关服务
集成：JWT 认证 + 账号系统 + 会话管理 + 安全代理

功能：
  1. 账号管理 - 注册/登录，JWT Token 签发
  2. 会话隔离 - 每个用户只看到自己的会话
  3. 安全代理 - master key 不出服务器，客户端使用 JWT
  4. 跨设备同步 - 多设备共享账号数据

运行方式：
  python3 hermes_gateway.py

部署为系统服务：
  sudo systemctl enable hermes-gateway
  sudo systemctl start hermes-gateway

作者: Hermes App Team
许可: MIT License
"""

import os
import json
import sqlite3
import secrets
import hashlib
import jwt
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import hmac

# bcrypt 可选：优先用 bcrypt 做加盐慢哈希；环境未安装时回退到标准库 pbkdf2_sha256（同样加盐慢哈希）
try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

# pbkdf2 回退算法的迭代次数
PBKDF2_ITERATIONS = 200_000

# 网关版本（单一事实源：app/status 经 /health 拿到的就是它；升级检测靠它比对）
# 规则：凡改动 gateway.py 行为/结构就 +1；setup.py 不再重复硬编码，改为解析本常量。
GATEWAY_VERSION = "2.4.2"

# ============ 配置 ============

class Config:
    """网关配置"""
    # 数据目录
    DATA_DIR = Path.home() / ".hermes-gateway"
    DB_PATH = DATA_DIR / "gateway.db"
    CONFIG_PATH = DATA_DIR / "config.json"

    # 默认配置
    GATEWAY_PORT = 8443
    # 监听地址。默认 0.0.0.0（直连/裸IP/端口映射场景，含 Tailscale 裸 IP `http://100.x:port`）。
    # 当网关躲在 Caddy 反代或 `tailscale serve` 之后时，设为 127.0.0.1，使其只在本机可达、
    # 由前置层负责对外暴露与 TLS。
    BIND_HOST = "0.0.0.0"
    HERMES_URL = "http://127.0.0.1:8642"  # Hermes 本地地址
    HERMES_API_KEY = None  # 从环境变量或配置文件读取
    JWT_SECRET = None  # 首次运行时生成
    JWT_EXPIRE_DAYS = 30
    # TLS（可选）：填了 cert+key 路径且文件存在，则网关直接服务 HTTPS（自包含，无需前置反代）；
    # 留空则纯 HTTP（由前面的 Caddy/nginx/tailscale serve 负责 TLS）。
    SSL_CERTFILE = None
    SSL_KEYFILE = None
    # 多用户按会话隔离（Phase A，默认开启；可在 config.json 置 false 灰度回退）：
    #   enforce_session_ownership: 单 master key 之上把每个会话/运行绑定到发起用户；之后任何
    #     携带他人会话/运行标识的读写一律 403；GET /api/sessions 列表与 search 仅回本人拥有的。
    #     未登记标识（cron/telegram 等系统线程）放行，但不进入任何人的列表。
    #   api_allow_prefixes: 非空时 /api/* 仅放行这些路径前缀（白名单），其余 403。
    ENFORCE_SESSION_OWNERSHIP = True
    API_ALLOW_PREFIXES = []

    @classmethod
    def load(cls):
        """加载配置"""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 数据目录含密码哈希(gateway.db)与密钥(config.json)，收紧为仅 owner 可访问
        try:
            os.chmod(cls.DATA_DIR, 0o700)
        except Exception:
            pass

        # 加载或生成配置文件
        if cls.CONFIG_PATH.exists():
            with open(cls.CONFIG_PATH, 'r') as f:
                config = json.load(f)
                cls.GATEWAY_PORT = config.get('gateway_port', cls.GATEWAY_PORT)
                cls.BIND_HOST = config.get('bind_host', cls.BIND_HOST)
                cls.HERMES_URL = config.get('hermes_url', cls.HERMES_URL)
                cls.HERMES_API_KEY = config.get('hermes_api_key')
                cls.JWT_SECRET = config.get('jwt_secret')
                cls.SSL_CERTFILE = config.get('ssl_certfile') or None
                cls.SSL_KEYFILE = config.get('ssl_keyfile') or None
                # 缺省时取类默认（True）→ 新库/未显式配置即安全默认开启；显式 false 才关。
                cls.ENFORCE_SESSION_OWNERSHIP = bool(config.get('enforce_session_ownership', cls.ENFORCE_SESSION_OWNERSHIP))
                cls.API_ALLOW_PREFIXES = config.get('api_allow_prefixes') or []

        # 如果没有配置，尝试从环境变量读取
        if not cls.HERMES_API_KEY:
            cls.HERMES_API_KEY = os.environ.get('HERMES_API_KEY')
            if not cls.HERMES_API_KEY:
                # 尝试从 Hermes 的 .env 读取
                hermes_env = Path.home() / ".hermes" / ".env"
                if hermes_env.exists():
                    for line in hermes_env.read_text().splitlines():
                        if line.strip().startswith("API_SERVER_KEY="):
                            cls.HERMES_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break

        # 生成 JWT 密钥
        if not cls.JWT_SECRET:
            cls.JWT_SECRET = secrets.token_hex(32)

        # 保存配置
        cls.save()

    @classmethod
    def save(cls):
        """保存配置"""
        config = {
            'gateway_port': cls.GATEWAY_PORT,
            'bind_host': cls.BIND_HOST,
            'hermes_url': cls.HERMES_URL,
            'hermes_api_key': cls.HERMES_API_KEY,
            'jwt_secret': cls.JWT_SECRET,
            'ssl_certfile': cls.SSL_CERTFILE,
            'ssl_keyfile': cls.SSL_KEYFILE,
            'enforce_session_ownership': cls.ENFORCE_SESSION_OWNERSHIP,
            'api_allow_prefixes': cls.API_ALLOW_PREFIXES,
        }
        with open(cls.CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        os.chmod(cls.CONFIG_PATH, 0o600)  # 仅所有者可读写

# ============ 数据库 ============

# 数据库结构版本：每次给「已存在的表」加列/改结构时 +1，并在 MIGRATIONS 登记对应迁移。
# 约定：init_db 的 CREATE TABLE 永远反映「最新结构」；迁移只负责把历史库逐级升到最新。
# 因此全新库（CREATE TABLE 已是最新）跑迁移应是幂等 no-op，迁移函数务必自带存在性检查。
SCHEMA_VERSION = 3

def _column_exists(conn, table, column):
    """表里是否已有该列（供迁移做幂等判断）。"""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())

def _schema_version(conn):
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0

def _set_schema_version(conn, v):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(v),))

# 迁移登记表：{目标版本: 接收 conn 的函数}。函数必须幂等（自带列/表存在性检查），
# 这样在全新库(CREATE TABLE 已是最新)上重复执行也安全。
def _mig_add_token_version(conn):
    """v2：users 加 token_version，用于令牌吊销（改密/登出/删号后旧 JWT 失效）。"""
    if not _column_exists(conn, "users", "token_version"):
        conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")

def _mig_add_ownership_tables(conn):
    """v3：会话/运行归属表（Phase A 按用户隔离）。均为 CREATE IF NOT EXISTS → 幂等。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS session_owners (
        session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        profile TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS run_owners (
        run_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_owners_user ON session_owners(user_id)")

MIGRATIONS = {2: _mig_add_token_version, 3: _mig_add_ownership_tables}

def run_migrations(conn):
    """把数据库结构从当前版本逐级升到 SCHEMA_VERSION；全新库与历史库都安全。"""
    cur = _schema_version(conn)
    for v in sorted(MIGRATIONS):
        if v > cur:
            MIGRATIONS[v](conn)
            _set_schema_version(conn, v)
            cur = v
    # 无迁移可跑（全新库或已最新）时盖章到基线版本，便于后续判断/排查
    if cur < SCHEMA_VERSION:
        _set_schema_version(conn, SCHEMA_VERSION)

def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(Config.DB_PATH)

    # users 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'user',              -- 'admin' | 'user'
            created_by INTEGER,                    -- 创建者 ID
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            token_version INTEGER NOT NULL DEFAULT 0,   -- 令牌吊销基线：自增即令旧 JWT 失效
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    # sessions 表（会话元数据）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            hermes_session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat',
            profile TEXT DEFAULT '',
            preview TEXT,
            source TEXT DEFAULT 'app',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pinned BOOLEAN DEFAULT 0,
            archived BOOLEAN DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            token_in INTEGER DEFAULT 0,
            token_out INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # devices 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            device_name TEXT,
            device_type TEXT,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, device_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # settings 表（网关级配置：owner 认领令牌、开放注册开关等）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # 会话/运行归属（Phase A 按用户隔离）：谁发起(run/responses)谁认领，之后跨用户读写 403，
    # GET /api/sessions 列表与 search 仅回本人拥有的。原 sessions 表已不再由网关自管
    # （改为透传 Hermes），保留不用。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_owners (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            profile TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_owners (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 创建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_owners_user ON session_owners(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id)")

    # 应用结构迁移（settings 表此时已建好）；全新库为幂等 no-op，历史库逐级升级
    run_migrations(conn)

    conn.commit()
    conn.close()

    # gateway.db 含密码哈希与 owner_claim_token：收紧为仅 owner 可读写（与 config.json 0600 对齐）
    try:
        os.chmod(Config.DB_PATH, 0o600)
    except Exception:
        pass

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key: str, default=None):
    """读网关级配置（settings 表）"""
    try:
        conn = get_db()
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default

def set_setting(key: str, value: str):
    """写网关级配置（settings 表）"""
    conn = get_db()
    conn.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value))
    )
    conn.commit()
    conn.close()

# ============ 工具函数 ============

def hash_password(password: str) -> str:
    """对密码做加盐慢哈希。

    bcrypt 可用时返回 bcrypt 哈希（以 $2 开头）；
    否则用标准库 pbkdf2_sha256，格式为 pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>。
    """
    if _HAS_BCRYPT:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与存储的哈希是否匹配，兼容 bcrypt / pbkdf2 / 旧版裸 SHA256。"""
    if not stored:
        return False
    # bcrypt
    if stored.startswith('$2'):
        if not _HAS_BCRYPT:
            return False
        try:
            return bcrypt.checkpw(password.encode(), stored.encode())
        except Exception:
            return False
    # pbkdf2_sha256
    if stored.startswith('pbkdf2_sha256$'):
        try:
            _, iters, salt, hexhash = stored.split('$')
            dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iters))
            return hmac.compare_digest(dk.hex(), hexhash)
        except Exception:
            return False
    # 旧版裸 SHA256（无盐）—— 仅为兼容历史数据，登录成功后会自动升级
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(legacy, stored)

def _get_token_version(user_id: int) -> int:
    """读取用户当前 token_version（吊销基线）；读不到按 0。"""
    try:
        conn = get_db()
        row = conn.execute('SELECT token_version FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        return int(row['token_version']) if row and row['token_version'] is not None else 0
    except Exception:
        return 0

def generate_token(user_id: int) -> dict:
    """生成 JWT Token（带 token_version，用于吊销校验）"""
    now = datetime.utcnow()
    tv = _get_token_version(user_id)
    access_payload = {
        'user_id': user_id,
        'type': 'access',
        'tv': tv,
        'exp': now + timedelta(days=Config.JWT_EXPIRE_DAYS)
    }
    refresh_payload = {
        'user_id': user_id,
        'type': 'refresh',
        'tv': tv,
        'exp': now + timedelta(days=Config.JWT_EXPIRE_DAYS * 2)
    }

    return {
        'access_token': jwt.encode(access_payload, Config.JWT_SECRET, algorithm='HS256'),
        'refresh_token': jwt.encode(refresh_payload, Config.JWT_SECRET, algorithm='HS256'),
        'expires_at': int((now + timedelta(days=Config.JWT_EXPIRE_DAYS)).timestamp())
    }

def _decode_token(token: str, expected_type: str = None):
    """解码并验签 JWT，返回 payload(dict) 或 None。expected_type 非空时校验 type。"""
    try:
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=['HS256'])
        if expected_type is not None and payload.get('type') != expected_type:
            return None
        return payload
    except Exception:
        return None

def verify_token(token: str, expected_type: str = None):
    """验证 JWT 并返回 user_id（不含 token_version 吊销校验；那由调用方查库比对）。

    默认 expected_type=None 保持旧行为（不校验 type），兼容现有调用方与已签发令牌。
    """
    payload = _decode_token(token, expected_type)
    return payload.get('user_id') if payload else None

def get_current_user():
    """从请求中获取当前用户（含 token_version 吊销校验）"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None

    token = auth_header[7:]
    payload = _decode_token(token)
    if not payload:
        return None
    user_id = payload.get('user_id')
    if not user_id:
        return None

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if not user:
        return None

    # token_version 吊销校验：令牌签发时的 tv 必须等于用户当前 tv（改密/登出后旧令牌立即失效）
    cur_tv = user['token_version'] if 'token_version' in user.keys() and user['token_version'] is not None else 0
    if int(payload.get('tv', 0)) != int(cur_tv):
        return None

    return dict(user)

def require_auth(f):
    """装饰器：要求认证"""
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': '未授权，请先登录'}), 401
        request.current_user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def require_admin(f):
    """装饰器：要求管理员权限"""
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': '未授权，请先登录'}), 401
        if user['role'] != 'admin':
            return jsonify({'error': '需要管理员权限'}), 403
        request.current_user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# ============ Flask App ============

app = Flask(__name__)
CORS(app)

# ============ 账号管理 API ============

@app.route('/api/auth/register', methods=['POST'])
def register():
    """注册新用户"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    display_name = data.get('display_name', username)
    email = data.get('email', '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    if len(password) < 6:
        return jsonify({'error': '密码长度至少 6 位'}), 400

    claim_token = (data.get('claim_token') or '').strip()
    conn = get_db()
    try:
        role = 'user'
        created_by = None
        is_owner_claim = False

        stored_token = get_setting('owner_claim_token', '')
        owner_claimed = get_setting('owner_claimed', '0') == '1'

        if claim_token:
            # 带 owner 一次性认领令牌：注册即成为管理员（令牌用后作废）
            if not stored_token or owner_claimed or not hmac.compare_digest(claim_token, stored_token):
                return jsonify({'error': '管理员认领令牌无效或已被使用'}), 403
            role = 'admin'
            is_owner_claim = True
        else:
            auth_user = get_current_user()
            if auth_user and auth_user['role'] == 'admin':
                # 管理员通过接口创建用户
                created_by = auth_user['id']
            elif get_setting('open_registration', '0') == '1':
                # 开放注册开启时，允许自助注册为普通用户
                role = 'user'
            else:
                return jsonify({'error': '注册未开放，请联系管理员创建账号'}), 403

        cursor = conn.execute(
            'INSERT INTO users (username, password_hash, display_name, email, role, created_by) VALUES (?, ?, ?, ?, ?, ?)',
            (username, hash_password(password), display_name, email, role, created_by)
        )
        conn.commit()
        user_id = cursor.lastrowid

        # owner 认领成功：作废令牌并关闭开放注册（此后仅管理员可建号）
        if is_owner_claim:
            set_setting('owner_claimed', '1')
            set_setting('owner_claim_token', '')
            set_setting('open_registration', '0')

        tokens = generate_token(user_id)

        return jsonify({
            'user': {
                'id': user_id,
                'username': username,
                'display_name': display_name,
                'email': email,
                'role': role
            },
            **tokens
        })
    except sqlite3.IntegrityError:
        return jsonify({'error': '用户名已存在'}), 400
    finally:
        conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username = ?',
        (username,)
    ).fetchone()
    conn.close()

    if not user or not verify_password(password, user['password_hash']):
        return jsonify({'error': '用户名或密码错误'}), 401

    # 旧版 SHA256 哈希在登录成功后透明升级为加盐慢哈希
    if not user['password_hash'].startswith(('$2', 'pbkdf2_sha256$')):
        try:
            up = get_db()
            up.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                       (hash_password(password), user['id']))
            up.commit()
            up.close()
        except Exception:
            pass

    tokens = generate_token(user['id'])

    return jsonify({
        'user': {
            'id': user['id'],
            'username': user['username'],
            'display_name': user['display_name'],
            'email': user['email'],
            'role': user['role'],
        },
        **tokens
    })

@app.route('/api/auth/refresh', methods=['POST'])
def refresh():
    """刷新 Token（仅接受 refresh 类型令牌；校验用户仍存在且 token_version 匹配）"""
    data = request.json
    refresh_token = data.get('refresh_token', '')

    payload = _decode_token(refresh_token, expected_type='refresh')
    if not payload:
        return jsonify({'error': '无效的刷新令牌'}), 401
    user_id = payload.get('user_id')

    conn = get_db()
    user = conn.execute('SELECT id, token_version FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    cur_tv = user['token_version'] if user and user['token_version'] is not None else 0
    if not user or int(payload.get('tv', 0)) != int(cur_tv):
        return jsonify({'error': '无效的刷新令牌'}), 401

    tokens = generate_token(user_id)
    return jsonify(tokens)

@app.route('/api/auth/logout', methods=['POST'])
@require_auth
def logout():
    """登出所有设备：自增 token_version，使该用户已签发的所有 JWT 立即失效。"""
    user_id = request.current_user['id']
    conn = get_db()
    conn.execute('UPDATE users SET token_version = token_version + 1 WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '已登出所有设备，请重新登录'})

@app.route('/api/auth/me', methods=['GET'])
@require_auth
def get_me():
    """获取当前用户信息"""
    user = request.current_user
    return jsonify({
        'id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'],
        'email': user['email'],
        'role': user['role'],
        'created_at': user['created_at']
    })

# ============ 管理员专用 API ============

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    """管理员：列出所有用户"""
    conn = get_db()
    users = conn.execute('''
        SELECT u.id, u.username, u.display_name, u.email, u.role, u.created_at, u.last_login,
               (SELECT COUNT(*) FROM session_owners WHERE user_id = u.id) as session_count
        FROM users u
        ORDER BY u.role DESC, u.created_at ASC
    ''').fetchall()
    conn.close()

    return jsonify({
        'users': [dict(u) for u in users],
        'total': len(users)
    })

@app.route('/api/admin/users', methods=['POST'])
@require_admin
def admin_create_user():
    """管理员：创建用户"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    display_name = data.get('display_name', username)
    email = data.get('email', '').strip()
    role = data.get('role', 'user')

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    if len(password) < 6:
        return jsonify({'error': '密码长度至少 6 位'}), 400

    if role not in ['admin', 'user']:
        return jsonify({'error': '无效的角色'}), 400

    conn = get_db()
    try:
        cursor = conn.execute(
            '''INSERT INTO users (username, password_hash, display_name, email, role, created_by)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (username, hash_password(password), display_name, email, role, request.current_user['id'])
        )
        conn.commit()
        user_id = cursor.lastrowid

        return jsonify({
            'user': {
                'id': user_id,
                'username': username,
                'display_name': display_name,
                'email': email,
                'role': role
            },
            'message': f'用户 {username} 创建成功'
        })
    except sqlite3.IntegrityError:
        return jsonify({'error': '用户名已存在'}), 400
    finally:
        conn.close()

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    """管理员：删除用户"""
    # 不能删除自己
    if user_id == request.current_user['id']:
        return jsonify({'error': '不能删除自己'}), 400

    conn = get_db()

    # 检查目标用户
    user = conn.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404

    # 检查是否是最后一个管理员
    if user['role'] == 'admin':
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if admin_count <= 1:
            conn.close()
            return jsonify({'error': '不能删除最后一个管理员'}), 400

    # 删除用户（级联删除会话和设备）
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'用户 {user["username"]} 已删除'
    })

@app.route('/api/admin/users/<int:user_id>/role', methods=['PATCH'])
@require_admin
def admin_change_user_role(user_id):
    """管理员：修改用户角色"""
    data = request.json
    new_role = data.get('role')

    if new_role not in ['admin', 'user']:
        return jsonify({'error': '无效的角色'}), 400

    # 不能修改自己的角色
    if user_id == request.current_user['id']:
        return jsonify({'error': '不能修改自己的角色'}), 400

    conn = get_db()

    # 检查目标用户
    user = conn.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404

    # 如果要降级管理员，检查是否是最后一个
    if user['role'] == 'admin' and new_role == 'user':
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if admin_count <= 1:
            conn.close()
            return jsonify({'error': '不能降级最后一个管理员'}), 400

    conn.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'用户 {user["username"]} 的角色已更新为 {new_role}'
    })

@app.route('/api/admin/users/<int:user_id>/password', methods=['PATCH'])
@require_admin
def admin_reset_password(user_id):
    """管理员：重置用户密码"""
    data = request.json
    new_password = data.get('password', '')

    if len(new_password) < 6:
        return jsonify({'error': '密码长度至少 6 位'}), 400

    conn = get_db()
    user = conn.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404

    conn.execute('UPDATE users SET password_hash = ?, token_version = token_version + 1 WHERE id = ?',
                 (hash_password(new_password), user_id))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'用户 {user["username"]} 的密码已重置，其已登录设备需重新登录'
    })

# ============ 会话管理：改为透传 Hermes ============
# 旧版网关自管一张 sessions 元数据表（list/create/update/delete）。app 新模型里会话真身在
# Hermes :8642 的 /api/sessions/*（列表/消息/搜索/改名/归档/删除），且 app 从不写网关这张表。
# 故移除这些自管路由：请求落到下方 proxy_hermes_api，由它透传 Hermes 并按用户做归属过滤/守卫。

# ============ Hermes 代理 ============

# hop-by-hop 头不能转发（RFC 7230 §6.1）：原样透传上游头会让 Content-Length /
# Transfer-Encoding / Content-Encoding 与我们重新分块的流式 body 冲突，导致客户端解析错乱。
_HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
    'content-length', 'content-encoding',
}

def _proxy_headers(headers):
    return [(k, v) for k, v in headers.items() if k.lower() not in _HOP_BY_HOP]

# ============ 会话/运行归属（Phase A：单 master key 之上按用户隔离）============
# Hermes 每实例只有一份全局会话库，且网关注入同一把 master key → Hermes 无法区分用户。
# 故在网关层维护 session/run -> user：谁发起(POST /v1/runs、/v1/responses)谁认领；之后任何
# 携带他人会话/运行标识的读写一律 403；GET /api/sessions 列表与 search 仅回本人拥有的。
# 归属只由"发起动作"建立，读操作只校验不认领；未登记标识(cron/telegram 等系统线程)放行，
# 但不进入任何人的列表。总开关：Config.ENFORCE_SESSION_OWNERSHIP。

def _owner_of_session(session_id):
    """会话属主 user_id；未登记返回 None。"""
    if not session_id:
        return None
    conn = get_db()
    try:
        row = conn.execute('SELECT user_id FROM session_owners WHERE session_id = ?',
                           (session_id,)).fetchone()
        return row['user_id'] if row else None
    finally:
        conn.close()

def _claim_session(session_id, user_id, profile=''):
    """首次触达认领（已属他人则不覆盖，INSERT OR IGNORE 保证原子）。"""
    if not session_id:
        return
    conn = get_db()
    try:
        conn.execute(
            'INSERT OR IGNORE INTO session_owners (session_id, user_id, profile) VALUES (?, ?, ?)',
            (session_id, user_id, profile or ''))
        conn.commit()
    finally:
        conn.close()

def _owner_of_run(run_id):
    """运行属主 user_id；未登记返回 None。"""
    if not run_id:
        return None
    conn = get_db()
    try:
        row = conn.execute('SELECT user_id FROM run_owners WHERE run_id = ?',
                           (run_id,)).fetchone()
        return row['user_id'] if row else None
    finally:
        conn.close()

def _claim_run(run_id, user_id):
    if not run_id:
        return
    conn = get_db()
    try:
        conn.execute('INSERT OR IGNORE INTO run_owners (run_id, user_id) VALUES (?, ?)',
                     (run_id, user_id))
        conn.commit()
    finally:
        conn.close()

def _owned_session_ids(user_id):
    """当前用户拥有的全部会话 id 集合（供列表/搜索过滤）。"""
    conn = get_db()
    try:
        return {r['session_id'] for r in conn.execute(
            'SELECT session_id FROM session_owners WHERE user_id = ?', (user_id,)).fetchall()}
    finally:
        conn.close()

def _body_session_id():
    """从 body/header 尽力取会话标识（runs:session_id；responses:conversation）。"""
    try:
        b = request.get_json(silent=True)
        if isinstance(b, dict):
            for k in ('session_id', 'hermes_session_id', 'conversation', 'session', 'sessionId'):
                v = b.get(k)
                if isinstance(v, str) and v:
                    return v
    except Exception:
        pass
    v = request.headers.get('X-Hermes-Session-Id')
    return v or None

def _item_session_ids(item):
    """一条会话/搜索结果里可能承载会话 id 的字段（列表多用 id，搜索命中多用 session_id）。"""
    if not isinstance(item, dict):
        return []
    return [item[k] for k in ('id', 'session_id', 'sessionId')
            if isinstance(item.get(k), str) and item.get(k)]

def _filter_owned_list(payload, owned):
    """把 Hermes 的会话/搜索结果过滤为仅本人拥有的项。
    fail-closed：识别不出承载会话的结构时回空列表，绝不原样透传（否则等于泄露全部）。
    键名不再写死为 sessions|data|results|hits——自动定位「值是数组、元素含 id 类字段」的键，
    兼容 Hermes 各版本形状（例如实测顶层会话数组键名与预期不符曾导致过滤被整包跳过）。"""
    def keep(item):
        return any(sid in owned for sid in _item_session_ids(item))
    if isinstance(payload, list):
        return [x for x in payload if keep(x)]
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, list) and (not v or (isinstance(v[0], dict)
                    and any(kk in v[0] for kk in ('id', 'session_id', 'sessionId')))):
                out = dict(payload)
                out[k] = [x for x in v if keep(x)]
                return out
    # 结构不认识：安全起见回空，绝不把未过滤的整包透传出去
    return []

# ============ 按助手名路由到其独立网关端口（/p/<name>/...）============
# 每个具名助手（含"接管"纳入的已有 bot，如带飞书的 bot2）跑在自己的 api_server 端口上，
# 端口登记在 ~/.hermes/app-assistants.json（{"assistants":[{"name":..,"port":..}, ..]}）。
# app 用 /p/<name>/v1/... 与 /p/<name>/api/... 访问该助手；网关据注册表把 name 映射到端口，
# 转发到 127.0.0.1:<port> 并注入 master key。缺这条路由时具名助手的聊天/会话/搜索都会 404
# （只有默认助手走裸 /v1、/api 正常）。
ASSISTANTS_REGISTRY = Path.home() / ".hermes" / "app-assistants.json"

def resolve_assistant_port(name):
    """助手名 -> 其独立网关端口（来自 app-assistants.json）。未知则 None。"""
    try:
        reg = json.loads(ASSISTANTS_REGISTRY.read_text("utf-8"))
        for a in reg.get("assistants", []):
            if a.get("name") == name and a.get("port"):
                return int(a["port"])
    except Exception:
        pass
    return None

def _proxy_to_port(port, sub_path):
    """把当前请求转发到 127.0.0.1:<port>/<sub_path>（注入 master key，流式返回）。
    与 proxy_hermes 一致地用 _proxy_headers 剥掉 hop-by-hop 头：否则上游的
    Content-Length/Transfer-Encoding/Content-Encoding 会和我们重新分块的流式 body 冲突，
    SSE（/v1/runs/<id>/events）会解析错乱。"""
    url = f"http://127.0.0.1:{port}/{sub_path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    headers = {
        'Authorization': f'Bearer {Config.HERMES_API_KEY}',
        'Content-Type': 'application/json',
    }
    try:
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, stream=True)
        else:
            resp = requests.request(
                method=request.method,
                url=url,
                headers=headers,
                json=request.json if request.is_json else None,
                stream=True,
            )
        return Response(
            resp.iter_content(chunk_size=1024),
            status=resp.status_code,
            headers=_proxy_headers(resp.headers),
        )
    except Exception as e:
        return jsonify({'error': f'代理请求失败: {str(e)}'}), 500

@app.route('/p/<name>/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@require_auth
def proxy_assistant_v1(name, path):
    """具名助手的 /v1/*（runs/responses/models 等）路由到它的独立端口。"""
    port = resolve_assistant_port(name)
    if not port:
        return jsonify({'error': f'未知助手或未登记端口: {name}'}), 404
    return _proxy_to_port(port, f"v1/{path}")

@app.route('/p/<name>/api/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@require_auth
def proxy_assistant_api(name, path):
    """具名助手的 /api/*（会话列表/消息/搜索等）路由到它的独立端口。"""
    port = resolve_assistant_port(name)
    if not port:
        return jsonify({'error': f'未知助手或未登记端口: {name}'}), 404
    return _proxy_to_port(port, f"api/{path}")

@app.route('/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@require_auth
def proxy_hermes(path):
    """代理 Hermes /v1/*（默认助手）。按用户做会话/运行归属校验与认领。"""
    uid = request.current_user['id']
    seg = path.split('/')
    capture_runs = False
    if Config.ENFORCE_SESSION_OWNERSHIP:
        # /v1/runs/<run_id>/...（events/stop/approval/get）：校验运行归属
        if seg[0] == 'runs' and len(seg) >= 2 and seg[1]:
            owner = _owner_of_run(seg[1])
            if owner is not None and owner != uid:
                return jsonify({'error': '无权访问该运行'}), 403
        # 创建运行/响应：按 body 里的会话标识先校验、再认领
        if request.method == 'POST' and path in ('runs', 'responses'):
            sid = _body_session_id()
            if sid:
                owner = _owner_of_session(sid)
                if owner is not None and owner != uid:
                    return jsonify({'error': '无权访问该会话'}), 403
                _claim_session(sid, uid)
            capture_runs = (path == 'runs')

    url = f"{Config.HERMES_URL}/v1/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    headers = {
        'Authorization': f'Bearer {Config.HERMES_API_KEY}',
        'Content-Type': 'application/json',
    }

    try:
        if capture_runs:
            # POST /v1/runs 的响应是小 JSON（run_id/session_id）：缓冲登记归属，再原样返回。
            resp = requests.request(
                method=request.method, url=url, headers=headers,
                json=request.json if request.is_json else None)
            try:
                j = resp.json()
                if isinstance(j, dict):
                    _claim_run(j.get('run_id') or j.get('id'), uid)
                    srv_sid = j.get('session_id') or j.get('sessionId')
                    if srv_sid:
                        _claim_session(srv_sid, uid)
            except Exception:
                pass
            return Response(resp.content, status=resp.status_code,
                            headers=_proxy_headers(resp.headers))
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, stream=True)
        else:
            resp = requests.request(
                method=request.method, url=url, headers=headers,
                json=request.json if request.is_json else None, stream=True)
        # 返回响应（保持流式传输）
        return Response(
            resp.iter_content(chunk_size=1024),
            status=resp.status_code,
            headers=_proxy_headers(resp.headers)
        )
    except Exception as e:
        return jsonify({'error': f'代理请求失败: {str(e)}'}), 500

@app.route('/api/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@require_auth
def proxy_hermes_api(path):
    """代理 Hermes /api/*（含会话列表/消息/搜索）。按用户做归属校验与列表过滤。"""
    uid = request.current_user['id']
    # 网关自己的账号/管理接口不代理（sessions 现改为透传 Hermes，故不再整体拦截）
    if path.startswith('auth/') or path.startswith('admin/'):
        return jsonify({'error': '无效的路径'}), 404

    # 助手/画像管理相关路径需要管理员
    if path.startswith('assistants') or path.startswith('profiles'):
        if request.current_user['role'] != 'admin':
            return jsonify({'error': '只有管理员可以管理助手'}), 403

    seg = path.split('/')
    is_sessions = seg[0] == 'sessions'
    list_or_search = is_sessions and path in ('sessions', 'sessions/search')
    # 单会话读写：/api/sessions/<id> 或 /api/sessions/<id>/messages（排除 search/prune）
    single_id = seg[1] if (is_sessions and len(seg) >= 2 and seg[1] not in ('', 'search', 'prune')) else None

    if Config.ENFORCE_SESSION_OWNERSHIP:
        # prune 会全局删旧会话（跨用户）→ 仅管理员
        if is_sessions and len(seg) >= 2 and seg[1] == 'prune' and request.current_user['role'] != 'admin':
            return jsonify({'error': '仅管理员可批量清理会话'}), 403
        if single_id is not None:
            owner = _owner_of_session(single_id)
            # fail-closed：无主会话（认领缺失或对不上 Hermes 真实 id）默认只有管理员可读写，
            # 普通用户一律拒绝；有主则必须是本人。杜绝"认领没对上→无主→放行"造成的越权。
            if owner is None:
                if request.current_user['role'] != 'admin':
                    return jsonify({'error': '无权访问该会话'}), 403
            elif owner != uid:
                return jsonify({'error': '无权访问该会话'}), 403

    # opt-in 白名单（默认空=不启用）：仅放行配置的前缀，其余拒绝
    if Config.API_ALLOW_PREFIXES and not any(path.startswith(p) for p in Config.API_ALLOW_PREFIXES):
        return jsonify({'error': '该接口未在白名单内'}), 403

    url = f"{Config.HERMES_URL}/api/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    headers = {
        'Authorization': f'Bearer {Config.HERMES_API_KEY}',
        'Content-Type': 'application/json',
    }

    # 会话列表/搜索：缓冲响应并过滤为本人拥有的会话（防止共享默认助手泄露他人列表）
    if Config.ENFORCE_SESSION_OWNERSHIP and request.method == 'GET' and list_or_search:
        try:
            resp = requests.request(method='GET', url=url, headers=headers)
        except Exception as e:
            return jsonify({'error': f'代理请求失败: {str(e)}'}), 500
        try:
            payload = _filter_owned_list(resp.json(), _owned_session_ids(uid))
            return jsonify(payload), resp.status_code
        except Exception:
            # 非 JSON / 形状意外：为不泄露，回空列表而非原样透传
            return jsonify([]), resp.status_code

    try:
        resp = requests.request(
            method=request.method, url=url, headers=headers,
            json=request.json if (request.method != 'GET' and request.is_json) else None,
            stream=True
        )
        return Response(
            resp.iter_content(chunk_size=1024),
            status=resp.status_code,
            headers=_proxy_headers(resp.headers)
        )
    except Exception as e:
        return jsonify({'error': f'代理请求失败: {str(e)}'}), 500

# ============ 助手管理 API（仅管理员，委托给 provisioner skill）============
# 助手的真正执行器是 install 时铺好的 provision.py（与 app 经 agent 调用的同一份）。
# 网关在这里通过 subprocess 调它并解析其 [[HAM:BEGIN]]{json}[[HAM:END]] 输出，
# 保证"网关 HTTP 调用"与"app→agent 调用"指向同一套实现，不再各搞一套。

PROVISION_PATH = Path.home() / ".hermes" / "skills" / "manage-assistant" / "provision.py"

def _parse_ham(text: str):
    b, e = "[[HAM:BEGIN]]", "[[HAM:END]]"
    i = text.rfind(b)
    if i < 0:
        return None
    j = text.find(e, i + len(b))
    if j < 0:
        return None
    try:
        return json.loads(text[i + len(b):j].strip())
    except Exception:
        return None

def _run_provision(args, timeout=90):
    """调用 provisioner，返回 (解析后的 dict, 错误字符串)。"""
    import subprocess
    if not PROVISION_PATH.exists():
        return None, '助手管理未安装，请重新运行 skill 的 install'
    try:
        r = subprocess.run(['python3', str(PROVISION_PATH)] + args,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, '助手脚本执行超时'
    except Exception as ex:
        return None, str(ex)
    j = _parse_ham(r.stdout or '')
    if j is None:
        return None, (r.stderr or r.stdout or '无法解析助手脚本输出')[:300]
    return j, None

@app.route('/api/admin/assistants', methods=['GET'])
@require_admin
def admin_list_assistants():
    """管理员：列出所有助手"""
    j, err = _run_provision(['list'], timeout=30)
    if err:
        return jsonify({'error': err}), 500
    return jsonify(j)

@app.route('/api/admin/assistants/init', methods=['POST'])
@require_admin
def admin_init_assistants():
    """助手管理已由 skill install 铺好；此端点仅做就绪自检，兼容旧版 app。"""
    if PROVISION_PATH.exists():
        j, err = _run_provision(['status'], timeout=20)
        if not err:
            return jsonify({'ok': True, 'installed': True, **(j or {})})
    return jsonify({'ok': False, 'error': '助手管理未安装，请重新运行 skill 的 install'}), 400

@app.route('/api/admin/assistants', methods=['POST'])
@require_admin
def admin_create_assistant():
    """管理员：创建新助手"""
    import tempfile
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '助手名称不能为空'}), 400
    # provisioner 的 create 读取一个 JSON 文件路径（name/soul/model）
    tf = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    try:
        json.dump({'name': name, 'soul': data.get('soul', ''), 'model': data.get('model', '')},
                  tf, ensure_ascii=False)
        tf.close()
        j, err = _run_provision(['create', tf.name], timeout=180)
    finally:
        try:
            os.unlink(tf.name)
        except Exception:
            pass
    if err:
        return jsonify({'error': err}), 500
    return jsonify(j)

@app.route('/api/admin/assistants/<name>', methods=['DELETE'])
@require_admin
def admin_delete_assistant(name):
    """管理员：删除助手"""
    j, err = _run_provision(['delete', name], timeout=60)
    if err:
        return jsonify({'error': err}), 500
    return jsonify(j)

@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def admin_stats():
    """管理员：系统统计信息"""
    conn = get_db()

    stats = {
        'total_users': conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
        'admin_count': conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0],
        'total_sessions': conn.execute('SELECT COUNT(*) FROM session_owners').fetchone()[0],
        'active_sessions': conn.execute('SELECT COUNT(*) FROM session_owners').fetchone()[0],
        'total_devices': conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0],
    }

    conn.close()

    # 检查 Hermes 状态
    hermes_status = check_hermes_connection()
    stats['hermes_status'] = 'online' if hermes_status else 'offline'

    return jsonify(stats)


@app.route('/health', methods=['GET'])
def health():
    """健康检查（无需认证，供 app / skill 探活与版本校验）"""
    return jsonify({
        'status': 'ok',
        'version': GATEWAY_VERSION,
        'hermes_url': Config.HERMES_URL,
        'hermes_connected': check_hermes_connection(),
        'owner_claimed': get_setting('owner_claimed', '0') == '1',
        'open_registration': get_setting('open_registration', '0') == '1',
    })

def check_hermes_connection():
    """检查 Hermes 连接"""
    try:
        resp = requests.get(
            f"{Config.HERMES_URL}/health",
            headers={'Authorization': f'Bearer {Config.HERMES_API_KEY}'},
            timeout=3
        )
        return resp.status_code == 200
    except:
        return False

# ============ 启动服务 ============

def main():
    """启动网关"""
    print("=" * 60)
    print("🚀 Hermes Gateway - 统一网关服务")
    print("=" * 60)

    # 加载配置
    Config.load()

    # 初始化数据库
    init_db()

    # 检查配置
    if not Config.HERMES_API_KEY:
        print("⚠️  警告：未找到 Hermes API Key")
        print("请确保以下之一：")
        print("  1. 设置环境变量 HERMES_API_KEY")
        print("  2. 在 ~/.hermes/.env 中配置 API_SERVER_KEY")
        print("  3. 在 ~/.hermes-gateway/config.json 中配置")
        return

    # TLS：配置了 cert+key 且文件存在则直接服务 HTTPS，否则纯 HTTP（TLS 交前置反代）
    ssl_ctx = None
    if (Config.SSL_CERTFILE and Config.SSL_KEYFILE
            and Path(Config.SSL_CERTFILE).exists() and Path(Config.SSL_KEYFILE).exists()):
        ssl_ctx = (Config.SSL_CERTFILE, Config.SSL_KEYFILE)
    scheme = "https" if ssl_ctx else "http"

    print(f"\n✅ 配置加载成功")
    print(f"   监听地址: {scheme}://{Config.BIND_HOST}:{Config.GATEWAY_PORT}")
    print(f"   Hermes: {Config.HERMES_URL}")
    print(f"   数据目录: {Config.DATA_DIR}")
    print(f"\n🔐 安全特性:")
    print(f"   ✓ JWT 认证（master key 不出服务器）")
    print(f"   ✓ 用户账号系统")
    print(f"   ✓ 会话隔离")
    print(f"   ✓ 多设备同步")
    print(f"\n📖 API 文档:")
    print(f"   POST /api/auth/register  - 注册")
    print(f"   POST /api/auth/login     - 登录")
    print(f"   GET  /api/sessions       - 会话列表")
    print(f"   All  /v1/*              - Hermes API 代理")
    print("=" * 60)

    # 启动 Flask（ssl_ctx 为 None 时即纯 HTTP）
    app.run(
        host=Config.BIND_HOST,
        port=Config.GATEWAY_PORT,
        debug=False,
        ssl_context=ssl_ctx
    )

if __name__ == '__main__':
    main()
