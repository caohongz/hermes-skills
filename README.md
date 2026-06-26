# hermes-skills

给 [hermes-app](https://github.com/caohongz/hermes-app) 用的、发布到公网、由 **Hermes 框架原生安装**的 skill（遵循 [agentskills.io](https://agentskills.io) 开放标准）。

当前 skill：**`hermes-app-gateway`** —— 一个 skill 一键铺好 app 所需的整套后端：网络侦测 + 认证代理层 + 用户管理 + 助手管理。

## 设计原则：把 LLM 从关键路径上拿掉

终端用户拿到 app 后只填**网关地址**就能用，运行时全走确定性 HTTP，不过大模型。而安装/部署这步——

- **安装 skill**：由 Hermes 框架完成（下载 + 安全扫描 + 落盘），**不经过 agent 模型**。
- **部署网关**：owner 手动跑一条命令，**也不经过 agent 模型**。

模型保守不保守，都不影响你装上、跑起来。

## 安装（owner 一次性）

在 Hermes 主机上，让框架安装 skill：

```bash
hermes skills install caohongz/hermes-skills/hermes-app-gateway
```

> 路径格式 `<github-owner>/<repo>/<skill-path>` 按 Hermes 文档推断。若框架不识别，先用 `hermes skills browse` / `hermes skills search` 查正确写法。

然后部署网关（推荐你手动跑，最稳）：

```bash
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py install
```

`install` 会打印 `[[HAM:BEGIN]]{...owner_claim_token...}[[HAM:END]]`。记下 `owner_claim_token` / `port` / `local_ip`，去 app 注册时填令牌即成管理员（并自动关闭开放注册）。

## 为什么这样可信

- **明文可审计**：`scripts/` 下 `gateway.py` / `provision.py` / `setup.py` 全是明文，agent、框架扫描、你本人都能逐行看。无 base64、无"下载未知脚本执行"。
- **框架安装**：`hermes skills install` 由框架下载 + security-scan + 落盘，不依赖 agent 判断。
- **master key 不出服务器**：`install` 在本机读 `~/.hermes/.env` 注入网关，客户端只拿 JWT。
- **可核对**：`manifest.json` 列出各文件 SHA256，owner 可手动核对。

## 目录结构

```
hermes-app-gateway/          # skill 包（hermes skills install 拉取它）
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
# 改 hermes-app-gateway/scripts/ 或 SKILL.md
python build.py                       # 规范化 LF + 刷新 manifest.json
git add -A && git commit -m "..."
git push                              # 提交即生效（hermes skills install 从 GitHub 拉）
```
版本化：同步升 `SKILL.md` 的 `version` 与 `scripts/setup.py` 的 `SKILL_VERSION`。

## skill 动作

| 动作 | 说明 |
|---|---|
| `install` | 复制网关+provisioner、装依赖、注入 master key、生成 JWT 密钥与 owner 令牌、起常驻进程、`/health` 自检 |
| `status` | 探测装没装 / 在跑否 / 版本 / 端口 |
| `detect` | 网络侦测（本地IP/公网IP/NAT/hermes 是否在跑）|
| `restart` / `stop` | 生命周期（NAS 重启后恢复 / 省内存）|
| `info` | 连接信息 + owner 状态 |

详细步骤见 [INSTALL-PROMPT.md](INSTALL-PROMPT.md)。
