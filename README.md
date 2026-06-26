# hermes-skills

给 [hermes-app](https://github.com/caohongz/hermes-app) 用的、发布到公网（GitHub + jsDelivr）、由 **Hermes 自己下载安装**的 skill。

当前 skill：**`hermes-app-gateway`** —— 一个 skill 一键铺好 app 所需的整套后端：网络侦测 + 认证代理层 + 用户管理 + 助手管理。

---

## 设计原则：LLM 只在"安装那一次"出现

终端用户拿到 app 后只填**网关地址**就能用。登录、用户管理、助手管理、收发消息**全部走确定性的 HTTP 网关**，一个字都不过大模型。只有"首次安装/配对"这一步借助 Hermes agent（它在主机上、有文件 + shell 工具）完成。这样把"模型不可靠"的暴露面压缩到字面意义上的一次安装。

```
阶段 0 安装（你，一次，在 app 之外）
  你 ──对话──> Hermes（CLI/dashboard/Telegram 任一入口）
      发 INSTALL-PROMPT.md 里的提示词
  Hermes agent ──> 下载 dist/setup.py → 校验 SHA256 → 落盘 → 跑 install
      ↓ 返回 [[HAM:BEGIN]]{...owner_claim_token...}[[HAM:END]]

阶段 1 认领（你，在 app 里，一次）
  app 注册时带 owner_claim_token → 你成为管理员，自动关闭开放注册

阶段 2 日常（终端用户，永远只到这层）
  app 只填 [网关地址] ──HTTP──> 网关 → 注册/登录 → 用所有功能
```

## 目录结构

```
src/
  gateway.py       认证代理网关（JWT + 用户管理 + 会话隔离 + 助手管理 + /health + owner 令牌）
  provision.py     助手 provisioner（每个助手 = 独立 profile + 独立常驻网关）
  setup_main.py    skill 入口：detect / status / install / restart / stop / info
build.py           把 src/* 打包成单文件 dist/setup.py，并算 SHA256
dist/
  setup.py         发布产物（gateway/provision 以 base64 内嵌，hermes 只下载这一个文件）
  manifest.json    { name, skill_version, entry, sha256, size }
```

## 开发 → 构建 → 发布

```bash
# 1. 改 src/ 里的源码
# 2. 重新打包并算哈希
python build.py
# 3. 提交并打一个【不可变】tag（务必用 tag，不要用分支名）
git add -A && git commit -m "skill vN"
git tag vN && git push && git push --tags
```

发布后 jsDelivr 永久指向该 tag 的字节：

```
https://cdn.jsdelivr.net/gh/caohongz/hermes-skills@vN/dist/setup.py
```

> ⚠️ jsDelivr 用 `@vN`（tag）或 `@<commit>` 才不可变；`@main` 有缓存且会变，**绝不能**用来配哈希校验。

## 安全模型（为什么这样就安全）

- **完整性**：发布时 `build.py` 算出 `dist/setup.py` 的 SHA256。安装提示词里**硬编码**这个哈希，经"你 → Hermes"这条可信通道传达；Hermes 下载公网文件后校验，**先校验、通过才落盘执行**（避免 TOCTOU）。能挡公网投毒、传输篡改、版本错配。
- **信任根在两端、都在你手里**：Hermes 端靠提示词里带的哈希；app 端靠**出厂内置**的期望哈希/版本（查网关 `/health` 的 `version` 比对，绝不采信网关自报）。
- **master key 不出服务器**：`install` 在主机本地读 `~/.hermes/.env` 的 `API_SERVER_KEY` 注入网关，app 永远拿不到它。
- **托管平台无需可信**：有哈希校验兜底，jsDelivr 只管"放文件"。
- 边界：哈希校验**挡不住"服务器被拿到 root"**（沦陷主机可自报正确哈希却跑改过的代码）。那一层要靠最小权限 / 外部监控，不在本 skill 范围。

## skill 动作

| 动作 | 说明 |
|---|---|
| `status` | 探测网关是否安装 / 在跑 / 版本 / 端口 |
| `detect` | 侦测网络（本地IP / 公网IP / NAT / hermes 是否在跑），用于配对建议 |
| `install` | 写网关+provisioner、装依赖、注入 master key、生成 JWT 密钥与 owner 一次性令牌、起常驻进程、`/health` 自检；返回连接信息 + `owner_claim_token` |
| `restart` | 重启网关进程（NAS 重启后恢复用）|
| `stop` | 停止网关进程 |
| `info` | 返回连接信息（地址/端口/版本/是否已认领 owner）|

完整安装提示词见 [INSTALL-PROMPT.md](INSTALL-PROMPT.md)。
