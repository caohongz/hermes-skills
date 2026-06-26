---
name: hermes-app-gateway
description: 部署与管理 hermes-app 的统一网关（JWT 认证代理 + 多用户管理 + 助手管理）。当这台主机的拥有者要安装/检查/重启/停止网关、侦测网络环境、或获取管理员(owner)认领令牌时使用。
version: 1.0.0
metadata:
  author: caohongz
  homepage: https://github.com/caohongz/hermes-skills
---

# hermes-app-gateway

把 hermes-app 所需的后端一次铺好：**认证代理层**（master key 留在服务器、客户端只拿 JWT）、**多用户管理**、**助手管理**。运行时一切走确定性 HTTP，不经过本 agent。

本 skill 自带明文源码 `scripts/gateway.py`、`scripts/provision.py`（可逐行审计），`install` 时把它们复制到运行位置并起常驻进程。

## When to Use

当本机拥有者明确要求：
- 首次部署 / 安装 hermes-app 网关
- 查看网关状态、版本、是否在运行
- 重启或停止网关（如 NAS 重启后恢复）
- 侦测网络环境以决定 app 连接方式
- 获取 owner 一次性认领令牌

## Procedure

按需运行 `scripts/setup.py` 的对应动作，脚本会打印一段 `[[HAM:BEGIN]]{json}[[HAM:END]]`：

| 需求 | 命令 |
|---|---|
| 安装 / 部署 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py install` |
| 查看状态 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py status` |
| 网络侦测 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py detect` |
| 重启 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py restart` |
| 停止 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py stop` |
| 连接信息 | `python3 ~/.hermes/skills/hermes-app-gateway/scripts/setup.py info` |

运行后，**只把脚本打印的那段 `[[HAM:BEGIN]]…[[HAM:END]]` 原样回传**给用户，不要添加其它内容。

## Pitfalls

- `install` 会读取 `~/.hermes/.env` 的 `API_SERVER_KEY` 并写入网关配置——这是**正当设计**：master key 始终留在本机，仅供本机网关进程代理到 Hermes，绝不发给任何客户端（客户端只拿 JWT）。
- `install` 返回的 `owner_claim_token` **只发一次**：交给拥有者，在 app 注册时填入即可成为管理员，并自动关闭开放注册。
- 网关在 `8443` 端口起一个独立 Flask 进程，与 Hermes 自身的 `8642` 并行；它只用 Python 标准库 + 运行时按需 pip 安装 flask/flask-cors/pyjwt/requests（bcrypt 可选），不会从网络下载其它代码。

## Verification

`install` 成功后运行 `status`，应返回 `installed: true`、`running: true`，且 `version` 与期望一致。
