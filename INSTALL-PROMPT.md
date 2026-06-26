# 安装 / 运维提示词

把下面的提示词发给 **Hermes 默认助手**（CLI / dashboard / Telegram 任一你信任的入口）。
当前版本：**v1**，`dist/setup.py` 的 SHA256：

```
c1734781eaf96d5aef279ccd0d163a9dcc6571890dd0baa2a218f2ed3a524059
```

> 每次 `python build.py` 后哈希会变，发布新 tag 时记得同步更新本文件里的 URL（`@vN`）和哈希。

---

## 1. 安装（阶段 0，你只做一次）

```text
你现在在 Hermes 主机上，拥有文件和 shell 工具。请严格按步骤把"hermes-app 统一网关"安装好。
任何一步失败都立即停止，并只回复：[[HAM:BEGIN]]{"ok":false,"error":"<失败原因>"}[[HAM:END]]

【第 1 步 下载】把下面 URL 下载到 /tmp/hagw-setup.py（curl / wget / python3 任一成功即可）：
https://cdn.jsdelivr.net/gh/caohongz/hermes-skills@v1/dist/setup.py
参考命令（任选其一）：
  curl -fsSL "https://cdn.jsdelivr.net/gh/caohongz/hermes-skills@v1/dist/setup.py" -o /tmp/hagw-setup.py
  python3 -c "import urllib.request as u; u.urlretrieve('https://cdn.jsdelivr.net/gh/caohongz/hermes-skills@v1/dist/setup.py','/tmp/hagw-setup.py')"

【第 2 步 校验完整性】计算 sha256，必须与下面这一行【完全一致】：
c1734781eaf96d5aef279ccd0d163a9dcc6571890dd0baa2a218f2ed3a524059
命令：
  python3 -c "import hashlib;print(hashlib.sha256(open('/tmp/hagw-setup.py','rb').read()).hexdigest())"
若不一致：删除 /tmp/hagw-setup.py，并回复
[[HAM:BEGIN]]{"ok":false,"error":"sha256 校验失败"}[[HAM:END]]，不要继续。

【第 3 步 落盘】校验通过后，创建目录 ~/.hermes/skills/hermes-app-gateway/，
把文件移动为 ~/.hermes/skills/hermes-app-gateway/setup.py，并 chmod +x。

【第 4 步 安装】运行：
  python3 ~/.hermes/skills/hermes-app-gateway/setup.py install

【第 5 步 回传】只回复第 4 步打印的那段 [[HAM:BEGIN]]…[[HAM:END]]，不要输出任何其它文字。
```

**安装成功**会返回类似：

```
[[HAM:BEGIN]]{"ok":true,"installed":true,"running":true,"skill_version":1,"version":"2.0.0","port":8443,"local_ip":"192.168.1.50","owner_claim_token":"AbC123..."}[[HAM:END]]
```

记下 `local_ip`、`port`、`owner_claim_token`。

## 2. 在 app 里认领管理员（阶段 1）

在 app 注册时填：

- 网关地址：`http://<local_ip>:<port>`（如 `http://192.168.1.50:8443`）
- 用户名 / 密码：你自己的
- **管理员认领令牌**：上一步的 `owner_claim_token`

提交后你即成为管理员，网关**自动关闭开放注册**，令牌作废。之后新用户只能由你在 app 里创建。

## 3. 运维提示词（按需）

探活 / 看版本：
```text
运行 python3 ~/.hermes/skills/hermes-app-gateway/setup.py status，只回复它打印的那段 [[HAM:BEGIN]]…[[HAM:END]]，不要其它任何文字。
```

侦测网络（配对建议）：
```text
运行 python3 ~/.hermes/skills/hermes-app-gateway/setup.py detect，只回复 [[HAM:BEGIN]]…[[HAM:END]]，不要其它任何文字。
```

NAS 重启后网关没起来 / 想重启：
```text
运行 python3 ~/.hermes/skills/hermes-app-gateway/setup.py restart，只回复 [[HAM:BEGIN]]…[[HAM:END]]，不要其它任何文字。
```

停止网关：
```text
运行 python3 ~/.hermes/skills/hermes-app-gateway/setup.py stop，只回复 [[HAM:BEGIN]]…[[HAM:END]]，不要其它任何文字。
```

## 4. 升级到新版本

改完 `src/` 后 `python build.py` → 提交并打新 tag（如 `v2`）→ 把本文件第 1 步的 `@v1` 换成 `@v2`、哈希换成新值 → 重新发安装提示词即可（`install` 幂等，会覆盖脚本、保留已有用户与 JWT 密钥）。
