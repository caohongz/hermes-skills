# 安装 / 部署 / 运维

青鸟接入网关（`bluebird-gateway`）是明文 skill。因它要读 master key、起代理服务，**不能走 `hermes skills install`**（会被安全扫描判 dangerous 拦截）。owner 手动放置即可——你装自己的后端，明文可审。

## 1. 放置 skill（owner 一次性）

```bash
git clone https://github.com/caohongz/hermes-skills.git ~/hermes-skills 2>/dev/null || git -C ~/hermes-skills pull
cp -r ~/hermes-skills/bluebird-gateway ~/.hermes/skills/
```

## 2. 部署：两种方式任选

### 方式 A：你手动跑（最稳，完全不经 LLM）

```bash
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py install
```

### 方式 B：对话部署（推荐——让 Hermes 按 SKILL.md 决策树尽量配好 HTTPS）

放好 skill 后，对 Hermes 说一句即可：

> 用 bluebird-gateway 这个 skill 帮我部署青鸟接入网关，并尽量配好可信 HTTPS：装好后探测环境，能签 Let's Encrypt 就签（先问我有没有域名 / DNS API 凭据），签不了就降级 Tailscale，再不行就告诉我还缺什么；最后把 App 该填的网关地址和 owner_claim_token 告诉我。

或**不预先放置、直接把下面整段粘给 Hermes**（自包含）：

```text
我是这台 Hermes 的主人。我在用「青鸟」手机 App——让我和家人各用各账号、安全共用这台 Hermes。
请部署它的接入网关 bluebird-gateway，并尽量配好可信 HTTPS（master key 留本机，客户端只拿 JWT）：
1. git clone https://github.com/caohongz/hermes-skills.git ~/hermes-skills 2>/dev/null || git -C ~/hermes-skills pull
2. cp -r ~/hermes-skills/bluebird-gateway ~/.hermes/skills/
3. 按 ~/.hermes/skills/bluebird-gateway/SKILL.md 的 Procedure 执行：install → detect → 配 TLS
   （A 档 Let's Encrypt：先问我有没有域名 / DNS 服务商 API 凭据，用 acme.sh 签 HTTP-01 或 DNS-01，
   再 `setup.py tls use-cert` 挂上；签不了就 B 档 `setup.py tailscale`；再不行 C 档告诉我还缺什么）。
4. 最后逐条明确告诉我：App「网关地址」该填什么（完整含协议和端口——注意端口未必是 8443，
   取决于监听端口/端口映射，按实际可达的给）、owner_claim_token、以及「首次要在 App 选『注册』
   而不是登录」。
卡住的话 ~/.hermes-gateway/gateway.log 有日志。
```

**成功**返回类似：

```
[[HAM:BEGIN]]{"ok":true,"installed":true,"running":true,"version":"2.0.0","port":8443,"local_ip":"192.168.1.50","owner_claim_token":"AbC123..."}[[HAM:END]]
```

记下 `local_ip`、`port`、`owner_claim_token`。

## 3. 在青鸟 App 里认领管理员

App「注册」时填：

- 网关地址（按你停在哪档）：
  - A 档可信 HTTPS：`https://<你的域名>:<port>`
  - B 档 Tailscale：tailscale 动作打印的 MagicDNS / 裸 IP 地址
  - 仅局域网测试：`http://<local_ip>:<port>`（明文，勿公网用）
- 用户名 / 密码：你自己的
- **管理员认领令牌**：部署时返回的 `owner_claim_token`

提交后即成管理员，网关**自动关闭开放注册**、令牌作废。之后新用户由你在 App 里创建。

## 4. 运维（按需）

```bash
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py status    # 状态/版本
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py detect    # 网络侦测
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py restart   # 重启（NAS 重启后）
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py stop      # 停止
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py info      # 连接信息
```

## 5. 升级

改 `scripts/` 或 `SKILL.md` → `python build.py` → `git push`。NAS 上 `git -C ~/hermes-skills pull && cp -r ~/hermes-skills/bluebird-gateway ~/.hermes/skills/`，再跑 `setup.py install`（幂等：覆盖脚本、保留已有用户与 JWT 密钥）。

## 6. 手动核对完整性（可选）

`manifest.json` 列出各文件 SHA256：

```bash
python3 -c "import hashlib;print(hashlib.sha256(open('$HOME/.hermes/skills/bluebird-gateway/scripts/gateway.py','rb').read()).hexdigest())"
# 与 manifest.json 中 scripts/gateway.py 的值比对
```
