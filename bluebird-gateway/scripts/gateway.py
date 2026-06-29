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

# 网关版本（与 skill 版本联动，便于 app 校验"装的是哪一版"）
GATEWAY_VERSION = "2.0.0"

# ============ 配置 ============

class Config:
    """网关配置"""
    # 数据目录
    DATA_DIR = Path.home() / ".hermes-gateway"
    DB_PATH = DATA_DIR / "gateway.db"
    CONFIG_PATH = DATA_DIR / "config.json"

    # 默认配置
    GATEWAY_PORT = 8443
    HERMES_URL = "http://127.0.0.1:8642"  # Hermes 本地地址
    HERMES_API_KEY = None  # 从环境变量或配置文件读取
    JWT_SECRET = None  # 首次运行时生成
    JWT_EXPIRE_DAYS = 30

    @classmethod
    def load(cls):
        """加载配置"""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 加载或生成配置文件
        if cls.CONFIG_PATH.exists():
            with open(cls.CONFIG_PATH, 'r') as f:
                config = json.load(f)
                cls.GATEWAY_PORT = config.get('gateway_port', cls.GATEWAY_PORT)
                cls.HERMES_URL = config.get('hermes_url', cls.HERMES_URL)
                cls.HERMES_API_KEY = config.get('hermes_api_key')
                cls.JWT_SECRET = config.get('jwt_secret')

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
            'hermes_url': cls.HERMES_URL,
            'hermes_api_key': cls.HERMES_API_KEY,
            'jwt_secret': cls.JWT_SECRET,
        }
        with open(cls.CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        os.chmod(cls.CONFIG_PATH, 0o600)  # 仅所有者可读写

# ============ 数据库 ============

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

    # 创建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id)")

    conn.commit()
    conn.close()

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

def generate_token(user_id: int) -> dict:
    """生成 JWT Token"""
    now = datetime.utcnow()
    access_payload = {
        'user_id': user_id,
        'type': 'access',
        'exp': now + timedelta(days=Config.JWT_EXPIRE_DAYS)
    }
    refresh_payload = {
        'user_id': user_id,
        'type': 'refresh',
        'exp': now + timedelta(days=Config.JWT_EXPIRE_DAYS * 2)
    }

    return {
        'access_token': jwt.encode(access_payload, Config.JWT_SECRET, algorithm='HS256'),
        'refresh_token': jwt.encode(refresh_payload, Config.JWT_SECRET, algorithm='HS256'),
        'expires_at': int((now + timedelta(days=Config.JWT_EXPIRE_DAYS)).timestamp())
    }

def verify_token(token: str) -> int:
    """验证 JWT Token 并返回 user_id"""
    try:
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=['HS256'])
        return payload['user_id']
    except:
        return None

def get_current_user():
    """从请求中获取当前用户"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None

    token = auth_header[7:]
    user_id = verify_token(token)

    if not user_id:
        return None

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()

    return dict(user) if user else None

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
            'email': user['email']
        },
        **tokens
    })

@app.route('/api/auth/refresh', methods=['POST'])
def refresh():
    """刷新 Token"""
    data = request.json
    refresh_token = data.get('refresh_token', '')

    user_id = verify_token(refresh_token)
    if not user_id:
        return jsonify({'error': '无效的刷新令牌'}), 401

    tokens = generate_token(user_id)
    return jsonify(tokens)

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
               (SELECT COUNT(*) FROM sessions WHERE user_id = u.id AND archived = 0) as session_count
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

    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(new_password), user_id))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'用户 {user["username"]} 的密码已重置'
    })

# ============ 会话管理 API ============

@app.route('/api/sessions', methods=['GET'])
@require_auth
def list_sessions():
    """获取当前用户的会话列表（管理员也只能看到自己的）"""
    user_id = request.current_user['id']

    conn = get_db()
    sessions = conn.execute(
        '''SELECT * FROM sessions
           WHERE user_id = ? AND archived = 0
           ORDER BY pinned DESC, updated_at DESC''',
        (user_id,)
    ).fetchall()
    conn.close()

    return jsonify([dict(s) for s in sessions])

@app.route('/api/sessions', methods=['POST'])
@require_auth
def create_session():
    """创建新会话"""
    user_id = request.current_user['id']
    data = request.json

    session_id = data.get('id')
    hermes_session_id = data.get('hermes_session_id', session_id)
    title = data.get('title', 'New Chat')
    profile = data.get('profile', '')

    conn = get_db()
    conn.execute(
        '''INSERT INTO sessions (id, user_id, hermes_session_id, title, profile)
           VALUES (?, ?, ?, ?, ?)''',
        (session_id, user_id, hermes_session_id, title, profile)
    )
    conn.commit()
    conn.close()

    return jsonify({'id': session_id, 'hermes_session_id': hermes_session_id})

@app.route('/api/sessions/<session_id>', methods=['PATCH'])
@require_auth
def update_session(session_id):
    """更新会话"""
    user_id = request.current_user['id']
    data = request.json

    updates = []
    params = []

    if 'title' in data:
        updates.append('title = ?')
        params.append(data['title'])
    if 'pinned' in data:
        updates.append('pinned = ?')
        params.append(1 if data['pinned'] else 0)
    if 'archived' in data:
        updates.append('archived = ?')
        params.append(1 if data['archived'] else 0)
    if 'preview' in data:
        updates.append('preview = ?')
        params.append(data['preview'])

    if not updates:
        return jsonify({'error': '没有可更新的字段'}), 400

    updates.append('updated_at = CURRENT_TIMESTAMP')
    params.extend([session_id, user_id])

    conn = get_db()
    conn.execute(
        f'UPDATE sessions SET {", ".join(updates)} WHERE id = ? AND user_id = ?',
        params
    )
    conn.commit()
    conn.close()

    return jsonify({'success': True})

@app.route('/api/sessions/<session_id>', methods=['DELETE'])
@require_auth
def delete_session(session_id):
    """删除会话"""
    user_id = request.current_user['id']

    conn = get_db()
    conn.execute('DELETE FROM sessions WHERE id = ? AND user_id = ?', (session_id, user_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

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

@app.route('/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@require_auth
def proxy_hermes(path):
    """代理 Hermes API 请求"""
    # 构建目标 URL
    url = f"{Config.HERMES_URL}/v1/{path}"

    # 复制查询参数
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    # 准备请求头（注入 master key）
    headers = {
        'Authorization': f'Bearer {Config.HERMES_API_KEY}',
        'Content-Type': 'application/json',
    }

    # 转发请求
    try:
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, stream=True)
        else:
            resp = requests.request(
                method=request.method,
                url=url,
                headers=headers,
                json=request.json,
                stream=True
            )

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
    """代理 Hermes /api/* 请求"""
    # 不代理网关自己的 API
    if path.startswith('auth/') or path.startswith('sessions') or path.startswith('admin/'):
        return jsonify({'error': '无效的路径'}), 404

    # 助手管理相关的路径需要管理员权限
    if path.startswith('assistants') or path.startswith('profiles'):
        current_user = request.current_user
        if current_user['role'] != 'admin':
            return jsonify({'error': '只有管理员可以管理助手'}), 403

    url = f"{Config.HERMES_URL}/api/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    headers = {
        'Authorization': f'Bearer {Config.HERMES_API_KEY}',
        'Content-Type': 'application/json',
    }

    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            json=request.json if request.method != 'GET' else None,
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
        'total_sessions': conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
        'active_sessions': conn.execute('SELECT COUNT(*) FROM sessions WHERE archived = 0').fetchone()[0],
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

    print(f"\n✅ 配置加载成功")
    print(f"   网关地址: http://0.0.0.0:{Config.GATEWAY_PORT}")
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

    # 启动 Flask
    app.run(
        host='0.0.0.0',
        port=Config.GATEWAY_PORT,
        debug=False
    )

if __name__ == '__main__':
    main()
