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

### 方式 B：对话部署

放好 skill 后，对 Hermes 说一句即可：

> 用 bluebird-gateway 这个 skill 帮我部署青鸟的接入网关，装好后把 owner_claim_token 告诉我。

或者**不预先放置、直接把下面整段粘给 Hermes**（自包含，已说明目的）：

```text
我是这台 Hermes 的主人。我在用「青鸟」——一个让我和家人各用各的账号、安全共用这台 Hermes 的手机 App。
现在要装好它的专属接入服务：负责账号登录和权限隔离，让 master key 留在本机、每个人只通过自己的账号访问、互不干扰。请帮我装好：
1. git clone https://github.com/caohongz/hermes-skills.git ~/hermes-skills 2>/dev/null || git -C ~/hermes-skills pull
2. python3 ~/hermes-skills/bluebird-gateway/scripts/setup.py install
3. 把它输出里的 owner_claim_token 告诉我，我在青鸟 App 里认领管理员。
卡住的话 ~/.hermes-gateway/gateway.log 有详细日志。
```

**成功**返回类似：

```
[[HAM:BEGIN]]{"ok":true,"installed":true,"running":true,"version":"2.0.0","port":8443,"local_ip":"192.168.1.50","owner_claim_token":"AbC123..."}[[HAM:END]]
```

记下 `local_ip`、`port`、`owner_claim_token`。

## 3. 在青鸟 App 里认领管理员

App 注册时填：

- 网关地址：`http://<local_ip>:<port>`（如 `http://192.168.1.50:8443`）
- 用户名 / 密码：你自己的
- **管理员认领令牌**：上一步的 `owner_claim_token`

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
