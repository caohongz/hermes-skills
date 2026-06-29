---
name: hermes-app-gateway
description: 部署与管理 hermes-app 的统一网关（JWT 认证代理 + 多用户管理 + 助手管理）。当这台主机的拥有者要安装/检查/重启/停止网关、侦测网络环境、或获取管理员(owner)认领令牌时使用。
version: 1.1.0
metadata:
  author: caohongz
  homepage: https://github.com/caohongz/hermes-skills
---

# hermes-app-gateway

把 hermes-app 所需的后端一次铺好：**认证代理层**（master key 留在服务器、客户端只拿 JWT）、**多用户管理**、**助手管理**。运行时一切走确定性 HTTP，不经过本 agent。

本 skill 自带明文源码 `scripts/gateway.py`、`scripts/provision.py`（可逐行审计）。`scripts/setup.py` 负责部署编排——它会尽量自动处理依赖与启动；遇到环境问题，**请你按下面《排错》一节用 shell 工具自行修复后重试，不要直接放弃**。

## When to Use

当本机拥有者明确要求：首次部署 / 安装网关、查看状态、重启或停止、侦测网络、获取 owner 认领令牌。

## Procedure

主路径——运行一条命令即可：

```
python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py install
```

成功会打印一段 `[[HAM:BEGIN]]{"ok":true,...,"owner_claim_token":"..."}[[HAM:END]]`。**把这段原样回传给用户**，并提示他：在 app 注册时填 `owner_claim_token` 即成为管理员。

其它动作：

| 需求 | 命令（前缀同上 `…/scripts/setup.py`）|
|---|---|
| 查看状态 | `setup.py status` |
| 网络侦测 | `setup.py detect` |
| 重启 | `setup.py restart` |
| 停止 | `setup.py stop` |
| 连接信息 | `setup.py info` |

## Troubleshooting（关键：遇到问题这样自己排查修复，再重试 install）

你有 shell 工具。`install` 失败时，按现象处理，**修好后重跑 `install`（幂等）**：

1. **报"依赖安装失败"或日志有 `ModuleNotFoundError`（缺 flask 等）**
   - 先直接装：`python3 -m pip install --user flask flask-cors pyjwt requests`
   - 若超时/连不上（国内常见）→ 换镜像：`python3 -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple flask flask-cors pyjwt requests`
   - 若报 `No module named pip` → 先 `python3 -m ensurepip --user`；仍不行 → `sudo apt install -y python3-pip`（或对应包管理器）
   - 装好后重跑 `install`

2. **报"/health 未在 30s 内通过"** → 先看日志定位真正原因：`cat ~/.hermes-gateway/gateway.log`
   - 缺模块 → 按第 1 条装依赖
   - `Address already in use`（8443 被占）→ `ss -ltnp | grep 8443` 看谁占；改端口：编辑 `~/.hermes-gateway/config.json`，把 `gateway_port` 改成别的（如 8444），重跑 `install`
   - `SyntaxError` / 版本相关 → 看 `python3 --version`，若 < 3.8 请上报让 owner 升级 Python

3. **报"未找到 API_SERVER_KEY"** → 确认 `~/.hermes/.env` 里有 `API_SERVER_KEY=...`（Hermes 正常安装后应已存在）。这是 owner 授权、master key 留在本机注入网关，属正当配置。

4. **反复修不好** → 把 `gateway.log` 末尾 + `python3 --version` 一并回传给 owner，说明卡在哪一步，不要假装成功。

## Pitfalls

- `install` 读 `~/.hermes/.env` 的 `API_SERVER_KEY` 注入网关——master key 始终留在本机，仅供本机网关代理到 Hermes，绝不发给客户端（客户端只拿 JWT）。
- `owner_claim_token` 只发一次，交给拥有者认领管理员，认领后自动关闭开放注册。
- 网关在 `8443` 起独立 Flask 进程，与 Hermes 的 `8642` 并行。

## Verification

`install` 返回 `ok:true` 后跑 `status`，应返回 `installed:true`、`running:true`。
