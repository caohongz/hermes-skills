# 远程访问部署清单（青鸟接入网关）

网关只做**认证 + 用户/助手管理 + 代理 Hermes**，它**传输无关**——“怎么从外面连进来”由 owner 自理。本文给两条路线，二选一即可。

> 前置：已装好 Hermes、并按 [INSTALL-PROMPT.md](INSTALL-PROMPT.md) 跑过 `setup.py install`（网关在本机 `8443` 起来、`/health` 通、拿到 `owner_claim_token`）。

## 统一前提：网关监听姿态

网关 `~/.hermes-gateway/config.json` 有 `bind_host` 字段：

| 场景 | `bind_host` | 说明 |
|---|---|---|
| 直连 / 端口映射直达 8443 / Tailscale 裸 IP | `0.0.0.0`（默认） | 网关自己对外监听 |
| 躲在 Caddy 反代 或 `tailscale serve` 之后 | `127.0.0.1` | 只在本机监听，前置层负责对外 + TLS |

改完 `bind_host` 需重启网关：`python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py restart`。

App 两条路线的操作完全一样：**填一个网关地址 + 登录账号**（首次填 `owner_claim_token` 认领管理员）。区别只在“网关地址填什么”和“你事先做了什么让它可达”。

---

## 路线 1：域名 + 端口（自理可达性与 HTTPS）

**适合**：想要“发个网址、手机零额外安装”，且愿意自己搞定网络与证书。

1. **可达性**：
   - 有**公网 IPv4** → 路由器做端口映射（外部端口 → NAS 内网 IP:8443）。国内运营商常**封禁入站 80/443**，用非标端口（如 8443）。
   - 只有**公网 IPv6** → DNS 配 AAAA 记录指向 IPv6；路由器**放行 IPv6 入站**对应端口。
   - **CGNAT / 无公网 IP** → 端口映射走不通，改用 frp + 一台国内 VPS（或内网穿透服务）把 8443 穿出去。
2. **DDNS**：家庭 IP 多为动态，用 DDNS 让域名跟踪当前 IP（IPv4 更 A 记录 / IPv6 更 AAAA）。
3. **HTTPS（必做）**：网关裸 HTTP，公网上密码/JWT 会被嗅探。前面摆 **Caddy** 反代到 `127.0.0.1:8443`、自动签发+续期证书；并把网关 `bind_host` 设 `127.0.0.1`。
   - 国内关键坑：入站 80/443 常被封 → Let's Encrypt 用 **DNS-01 验证**（Caddy 配 DNSPod/阿里云插件），不依赖入站 80/443。
   - Caddy 这层也是做**登录限流**的好位置（网关本身没有防爆破）。
4. **App 填**：`https://你的域名[:端口]`。

---

## 路线 2：Tailscale（私有网格，零公网暴露）

**适合**：自己 + 几个肯装 Tailscale 的人；不想开放任何公网入口。

1. 在 **Hermes 主机**装 Tailscale 并登录你的 tailnet。
2. 跑可选动作把网关挂上 tailnet（带自动 HTTPS）：
   ```bash
   python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py tailscale [authkey]
   ```
   - 没带 authkey 且未登录时，先 `tailscale up`（浏览器登录）或在 Tailscale 后台生成 auth key 传进来。
   - 成功会打印 `app_url_magicdns`（如 `https://nas.tailxxxx.ts.net`）和 `app_url_ip`（如 `http://100.x.y.z:8443`）。
   - 用 MagicDNS(serve) 方案 → 把 `bind_host` 设 `127.0.0.1` 后重启网关；用裸 IP 方案 → `bind_host` 保持 `0.0.0.0`。
3. **每台手机**：装**官方 Tailscale app** → 登录同一 tailnet → 在后台确认设备已授权。
4. **App 填**：`app_url_magicdns`（推荐，自动 HTTPS）或 `app_url_ip`（http 即可，隧道已加密）。

⚠️ 手机系统**同一时刻只能有一个 VPN**：用青鸟连家里时，科学上网那类 VPN 需关闭（官方 Tailscale app 占系统 VPN 插槽）。路线 1 无此问题。

---

## App 端填址速查

| 路线 | 网关地址栏填 | http/https | 手机前置条件 |
|---|---|---|---|
| 1 域名 | `https://你的域名[:端口]` | https（Caddy 证书） | 无 |
| 2 Tailscale（MagicDNS） | `https://主机名.tailnet名.ts.net` | https（Tailscale 证书） | 装官方 Tailscale app + 加入 tailnet + 设备授权 |
| 2 Tailscale（裸 IP） | `http://100.x.y.z:8443` | http（隧道已加密） | 同上 |

登录两条路一致：用户名 + 密码（首次额外填 `owner_claim_token`）。填完地址 App 会自动探测 `/health`——显示“● 已连接 · 网关版本 · Hermes 在线”即通；显示“无法连接”则是地址错或（路线 2）手机没进 tailnet。

## 安全清单（公网暴露时尤其注意）

- **TLS 必开**（路线 1 用 Caddy；路线 2 由 Tailscale 隧道/serve 保证）。
- **强密码**；首个管理员用 `owner_claim_token` 认领后，开放注册自动关闭，新号由管理员在 App 内创建。
- 公网入口建议在反代层加**登录限流 / fail2ban**。
- **Hermes 始终只听 `127.0.0.1:8642`**，永不直接对外——网关是唯一的门。
- 备份 `~/.hermes-gateway/`（用户表 + JWT 密钥），丢了全员需重置。
