---
name: bluebird-gateway
description: 部署与管理「青鸟」App 的接入网关（JWT 认证代理 + 多用户管理 + 助手管理）。当这台主机的拥有者要安装/检查/重启/停止青鸟网关、侦测网络、或获取管理员(owner)认领令牌时使用。
version: 1.2.0
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

主路径，一条命令：

```
python3 ~/.hermes/skills/bluebird-gateway/scripts/setup.py install
```

成功会打印 `[[HAM:BEGIN]]{"ok":true,...,"owner_claim_token":"..."}[[HAM:END]]`。**把这段原样回传给用户**，并告诉他：在青鸟 App 注册时填 `owner_claim_token` 即成为管理员。

其它动作（末尾换词即可）：`status` 状态 / `detect` 网络侦测 / `restart` 重启 / `stop` 停止 / `info` 连接信息。

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

## Verification

`install` 返回 `ok:true` 后跑 `status`，应返回 `installed:true`、`running:true`。
