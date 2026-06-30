---
name: bluebird-gateway
description: 部署与管理「青鸟」App 的接入网关（JWT 认证代理 + 多用户管理 + 助手管理）。当这台主机的拥有者要安装/检查/重启/停止青鸟网关、侦测网络、或获取管理员(owner)认领令牌时使用。
version: 1.7.0
metadata:
  author: caohongz
  homepage: https://github.com/caohongz/hermes-skills
---

# 青鸟接入网关 (bluebird-gateway)

把「青鸟」App 所需的后端一次铺好：**认证代理层**（master key 留在服务器、客户端只拿 JWT）、**多用户管理**、**助手管理**。运行时一切走确定性 HTTP，不经过本 agent。

本 skill 自带明文源码 `scripts/gateway.py`、`scripts/provision.py`（可逐行审计），`scripts/setup.py` 负责部署编排（自动处理依赖与启动）。

## When to Use

当本机拥有者要：部署/安装青鸟网关、查看状态、重启或停止、侦测网络、获取 owner 认领令牌。

## Procedure

目标：装好网关，**尽量配好可信 HTTPS 远程访问**，最后把 App 该填的【网关地址】+【owner_claim_token】交给用户。按下面的档位**逐级降级**，能成一档就停在那档；过程中**可以询问用户、也可以自行探测**。setup.py 每个动作都打印 `[[HAM:BEGIN]]{json}[[HAM:END]]`，解析它判断成败。

### 1. 装网关（确定性）
```
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py install
```
输出含 `owner_claim_token` / `port` / `local_ip`。此时网关在本机跑 **HTTP**（尚不能安全公网用）。

### 2. 探测环境
```
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py detect
```
看 `public_ip` / `has_nat` / `tools`(openssl/acme.sh/certbot/caddy/tailscale 在不在) / `port80_free` / `port443_free`，据此选下面的档。

### 3. 配远程访问 + TLS（逐级降级，成了就停）

**A 档 · 可信 HTTPS（首选）**——需要一个域名。**问用户**：
> 你有域名指向这台机器吗？能给 DNS 服务商的 API 凭据吗（走 DNS-01，无需开放端口、CGNAT 也行）？

- 有域名 + **入站 80 可达** → acme.sh HTTP-01：
  `curl https://get.acme.sh | sh -s email=<你的邮箱>` →
  `~/.acme.sh/acme.sh --issue --standalone -d <域名>`
- 有域名 + **DNS API 凭据**（推荐，封端口/CGNAT 都行）→ acme.sh DNS-01：
  `export <该 DNS 服务商的环境变量>` → `~/.acme.sh/acme.sh --issue --dns <dns_provider> -d <域名>`
- 签到后装证书 + 挂上网关（`--reloadcmd` 让续期自动重启网关）：
  ```
  ~/.acme.sh/acme.sh --install-cert -d <域名> \
    --fullchain-file ~/.hermes-gateway/certs/fullchain.pem \
    --key-file ~/.hermes-gateway/certs/privkey.pem \
    --reloadcmd "python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py restart"
  python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py tls use-cert \
    ~/.hermes-gateway/certs/fullchain.pem ~/.hermes-gateway/certs/privkey.pem
  ```
  返回 `tls:https` 即成。App 网关地址 = `https://<域名>:<port>`。

**B 档 · 降级 Tailscale**——没域名 / 签不到证书：
```
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py tailscale [authkey]
```
网关挂上 tailnet（隧道自带加密，HTTP 即可）。App 用打印出的 MagicDNS / 裸 IP 地址。**提醒用户**：每台手机要装官方 Tailscale app，且与其它 VPN 互斥。

**C 档 · 给建议，别硬来**——上面都不行：把现状讲清，列出他需要补的（一个域名 / 一个 DNS API token / 或装 Tailscale），并强调：**在补齐前只可局域网或隧道内用 HTTP，切勿明文公网暴露**（密码、JWT、对话都会被嗅探）。

### 4. 交付给用户（务必逐项给全、给准，别含糊）
不管停在哪档，最后把下面**逐条**告诉用户：

- **网关地址（完整：协议 + 主机 + 端口，一字不差）**：
  - A 档：`https://<域名>:<端口>`
  - B 档：tailscale 动作打印的 `app_url_magicdns` 或 `app_url_ip`，**原样**给。
  - 仅局域网测试：`http://<local_ip>:<端口>`（明文，勿公网）。
  - ⚠️ **端口别想当然填 8443**：网关本机监听端口看各动作输出里的 `port`（8443 被占时会不同）；
    若你做了**端口映射或反代**，App 要填的是**对外那个端口**，未必等于网关监听端口——按实际可达的填。
- **owner_claim_token = `<...>`**（原样给，提示一次性、认领后即作废）。
- **一句操作指引**：「打开青鸟 App → 填上面的网关地址 → 等下面变绿『已连接』→
  **首次用请选『注册』（不是登录）**，App 检测到本网关还没管理员时会自动切到注册 →
  填用户名 / 密码 / 显示名 + 上面的令牌 → 成为管理员。」

> **要改端口**：编辑 `~/.hermes-gateway/config.json` 的 `gateway_port`，再 `setup.py restart`；
> 对外端口由你的端口映射 / 反代决定，必须和 App 里填的一致。

其它动作：`status` / `info` 连接信息 / `restart` / `stop` / `tls status` 看当前协议 / `tls off` 关 TLS。

### 5. 升级已装的网关
`status` / `info` 返回三个版本字段：`version`（当前在跑的）/ `bundled_version`（本 skill 自带的）/ `update_available`。
当 `update_available:true`（或你刚更新了 skill）时，**重跑 `install` 即完成升级**——它幂等覆盖网关源码并重启，并**保留**：
- `config.json`（端口 / `jwt_secret` / TLS 证书路径）→ **已登录用户不会掉线**；
- `gateway.db`（账号 / 会话 / 密码 / owner 认领状态）。

网关启动时自动跑数据库迁移（`run_migrations`，按 `schema_version` 逐级升），**老库平滑加列、无需手工改表**。
> ⚠️ 升级有几秒停机；若新版 `/health` 30s 内没过，`install` 会失败而旧进程已停——查 `~/.hermes-gateway/gateway.log` 后重试（`install` 可反复跑）。
> 升级只覆盖本机已有的 skill 源码：要先把 skill 更新到新版（`hermes skills install caohongz/hermes-skills/bluebird-gateway`）再 `install`。

## Troubleshooting（排错参考）

`install` 失败时按现象处理，修好后重跑 `install`（幂等）：

- **缺依赖 / 日志有 `ModuleNotFoundError`**：`python3 -m pip install --user flask flask-cors pyjwt requests`；国内超时则加镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；无 pip 先 `python3 -m ensurepip --user`（或 `sudo apt install -y python3-pip`）。
- **报"/health 未通过"**：日志在 `~/.hermes-gateway/gateway.log`。若 `Address already in use`（8443 被占），改 `~/.hermes-gateway/config.json` 的 `gateway_port` 再重跑。
- **报"未找到 API_SERVER_KEY"**：确认 `~/.hermes/.env` 有 `API_SERVER_KEY=...`（Hermes 正常安装后即有）。
- 反复失败：把 `gateway.log` 末尾 + `python3 --version` 回传给 owner，说明卡在哪步。

## Pitfalls

- `install` 读 `~/.hermes/.env` 的 `API_SERVER_KEY` 注入网关——master key 始终留本机、仅供本机网关代理到 Hermes，绝不发给客户端（客户端只拿 JWT）。这是 owner 授权的正当配置。
- `owner_claim_token` 只发一次，交给拥有者认领管理员，认领后自动关闭开放注册。
- 网关在 `8443` 起独立 Flask 进程，与 Hermes 的 `8642` 并行。
- 传输无关：远程访问由 owner 自理（域名+端口映射+HTTPS，或 `tailscale`）。网关 `config.json` 的 `bind_host` 默认 `0.0.0.0`（直连/裸IP）；置于 Caddy 反代或 `tailscale serve` 之后时设为 `127.0.0.1`，只在本机监听、由前置层负责对外与 TLS。
- TLS 两种做法：①前置反代（Caddy/nginx）做 HTTPS、网关跑纯 HTTP（默认）；②网关自带 TLS——在 `config.json` 填 `ssl_certfile` / `ssl_keyfile`（PEM 路径），网关直接服务 HTTPS、无需反代。证书文件需对运行网关的用户可读；证书续期后需重启网关（`setup.py restart`）重新加载。

- **令牌吊销**：改密 / 管理员重置密码 / `POST /api/auth/logout` 会自增该用户 `token_version`，
  使其已签发的所有 JWT 立即失效（该用户已登录设备需重新登录）。删号同样使其令牌失效。
- **多用户授权加固（可选，默认关闭）**：`config.json` 两个开关，改完 `setup.py restart` 生效——
  - `enforce_session_ownership: true`：`/v1`、`/api` 转发前校验请求里出现的会话标识归属，阻断可识别的跨用户会话访问（无法识别的标识仍放行，不破坏功能）；
  - `api_allow_prefixes: ["chat/", "models"]`：把 `/api/*` 代理改为白名单，仅放行列出的前缀，其余 403。
  - ⚠️ **开启前务必确认 app 实际用到的 `/v1`/`/api` 路径与会话标识字段（`hermes_session_id`/`session_id` 等），避免误挡正常请求。** 建议先在测试账号上验证再推广。

## Verification

`install` 返回 `ok:true` 后跑 `status`，应返回 `installed:true`、`running:true`。
