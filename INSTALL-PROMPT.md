# 安装 / 运维

`hermes-app-gateway` 是标准 agentskills skill，由 **Hermes 框架安装**，不经过 agent 模型。

## 1. 安装 skill（owner 一次性）

在 Hermes 主机上：

```bash
hermes skills install caohongz/hermes-skills/hermes-app-gateway
```

> 路径格式 `<github-owner>/<repo>/<skill-path>` 按 Hermes 文档推断。若框架不识别它，先用
> `hermes skills search hermes-app-gateway` 或 `hermes skills browse` 查正确写法，
> 或用直接 URL 形式安装 `.../hermes-app-gateway/SKILL.md`。

框架会下载、安全扫描，并把 skill 装到 `~/.hermes/skills/hermes-app-gateway/`。

## 2. 部署网关

skill 装好后，**你自己跑**（最稳，完全不经 LLM）：

```bash
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py install
```

或对话让 agent 跑（它执行的是已装、已扫描的**本地** skill 脚本，接受度比"下载远程脚本"高）：

```text
运行 python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py install，
只回复它打印的那段 [[HAM:BEGIN]]…[[HAM:END]]，不要其它任何文字。
```

**安装成功**返回类似：

```
[[HAM:BEGIN]]{"ok":true,"installed":true,"running":true,"skill_version":1,"version":"2.0.0","port":8443,"local_ip":"192.168.1.50","owner_claim_token":"AbC123..."}[[HAM:END]]
```

记下 `local_ip`、`port`、`owner_claim_token`。

## 3. 在 app 里认领管理员

app 注册时填：

- 网关地址：`http://<local_ip>:<port>`（如 `http://192.168.1.50:8443`）
- 用户名 / 密码：你自己的
- **管理员认领令牌**：上一步的 `owner_claim_token`

提交后你即成为管理员，网关**自动关闭开放注册**、令牌作废。之后新用户只能由你在 app 里创建。

## 4. 运维（按需）

```bash
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py status    # 状态/版本
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py detect    # 网络侦测
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py restart   # 重启（NAS 重启后）
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py stop      # 停止
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py info      # 连接信息
```

## 5. 升级

改 `scripts/` 或 `SKILL.md` → 升 `version` / `SKILL_VERSION` → `python build.py` → push →
重新 `hermes skills install`（或框架更新）后再跑一次 `setup.py install`（幂等：覆盖脚本、保留已有用户与 JWT 密钥）。

## 6. 手动核对完整性（可选）

`manifest.json` 列出各文件 SHA256。校验某个文件：

```bash
python3 -c "import hashlib;print(hashlib.sha256(open('$HOME/.hermes/skills/hermes-app-gateway/scripts/gateway.py','rb').read()).hexdigest())"
# 与 manifest.json 中 scripts/gateway.py 的值比对
```
