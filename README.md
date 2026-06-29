# hermes-skills

[青鸟 (Bluebird)](https://github.com/caohongz/hermes-app) App 的接入网关 skill，遵循 [agentskills.io](https://agentskills.io) 标准、明文可审计。

当前 skill：**`bluebird-gateway`** —— 一个 skill 铺好青鸟 App 所需的整套后端：网络侦测 + 认证代理层 + 多用户管理 + 助手管理。

## 设计原则：把 LLM 从关键路径上拿掉

终端用户拿到青鸟 App 只填网关地址 + 账号就能用，运行时全走确定性 HTTP，不过大模型。安装/部署由 owner 一次性完成。

## 安装（owner 一次性）

> ⚠️ **不要用 `hermes skills install`**：本 skill 要读 master key、起代理服务，Hermes 的安全扫描器会判它 dangerous 并拦截（这是它防"陌生危险 skill"的本分）。你是 owner、装自己的后端，**手动放置**即可——明文可审、你就是审查者。

在 Hermes 主机上：

```bash
git clone https://github.com/caohongz/hermes-skills.git ~/hermes-skills 2>/dev/null || git -C ~/hermes-skills pull
cp -r ~/hermes-skills/bluebird-gateway ~/.hermes/skills/      # 放进 skills 目录，agent 即可发现
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py install
```

`install` 会打印 `[[HAM:BEGIN]]{...owner_claim_token...}[[HAM:END]]`。记下 `owner_claim_token` / `port` / `local_ip`，去青鸟 App 注册时填令牌即成管理员（并自动关闭开放注册）。

放好 skill 后也可**对话部署**（对 Hermes 说一句话即可），见 [INSTALL-PROMPT.md](INSTALL-PROMPT.md)。

## 为什么这样可信

- **明文可审计**：`scripts/` 下全是明文，你、agent、扫描器都能逐行看。无 base64、无"下载未知脚本执行"。
- **owner 手动**：安装与部署都由你跑，不依赖模型判断。
- **master key 不出服务器**：`install` 在本机读 `~/.hermes/.env` 注入网关，客户端只拿 JWT。
- **可核对**：`manifest.json` 列出各文件 SHA256。

## 目录结构

```
bluebird-gateway/            # skill 包
├── SKILL.md                 # agentskills 标准：frontmatter + 指令
├── manifest.json            # 各文件 SHA256（供手动核对）
└── scripts/
    ├── setup.py             # 入口：install/status/detect/restart/stop/info
    ├── gateway.py           # 认证代理网关（明文）
    └── provision.py         # 助手 provisioner（明文）
build.py                     # 规范化 LF + 刷新 manifest
```

## 开发 → 发布

```bash
# 改 bluebird-gateway/scripts/ 或 SKILL.md
python build.py                       # 规范化 LF + 刷新 manifest.json
git add -A && git commit -m "..." && git push
```
NAS 上更新：`git -C ~/hermes-skills pull && cp -r ~/hermes-skills/bluebird-gateway ~/.hermes/skills/`，再跑 `setup.py install`（幂等）。

## skill 动作

| 动作 | 说明 |
|---|---|
| `install` | 复制网关+provisioner、装依赖（失败自动换国内镜像）、注入 master key、生成 JWT 密钥与 owner 令牌、起常驻进程、`/health` 自检 |
| `status` | 探测装没装 / 在跑否 / 版本 / 端口 |
| `detect` | 网络侦测（本地IP/公网IP/NAT/hermes 是否在跑）|
| `restart` / `stop` | 生命周期（NAS 重启后恢复 / 省内存）|
| `info` | 连接信息 + owner 状态 |

详细步骤见 [INSTALL-PROMPT.md](INSTALL-PROMPT.md)。
