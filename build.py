#!/usr/bin/env python3
"""规范化 skill 文件为 LF，并生成 manifest.json（各文件 SHA256，供手动核对/版本追溯）。

明文 skill：hermes-app-gateway/ 下就是最终内容，无需打包。本脚本只做两件事：
1) 把所有文本文件统一为 LF（保证跨平台字节稳定，与 GitHub/jsDelivr 一致）
2) 算每个文件的 SHA256，写 hermes-app-gateway/manifest.json

安装由 Hermes 框架完成：`hermes skills install caohongz/hermes-skills/hermes-app-gateway`
（框架下载 + 安全扫描 + 落盘，不经过 LLM）。manifest.json 仅供 owner 手动核对。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "hermes-app-gateway"

# 纳入 manifest 的文件（相对 SKILL 目录）；manifest.json 自身不纳入（避免自引用）
FILES = ["SKILL.md", "scripts/setup.py", "scripts/gateway.py", "scripts/provision.py"]


def main():
    files = {}
    for rel in FILES:
        p = SKILL / rel
        if not p.exists():
            raise SystemExit(f"✗ 缺少 {rel}")
        # 规范化为 LF 并写回，保证工作区字节 == git 存储字节 == 这里算的哈希
        raw = p.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        p.write_bytes(raw)
        files[rel] = hashlib.sha256(raw).hexdigest()

    setup = (SKILL / "scripts/setup.py").read_text(encoding="utf-8")
    m = re.search(r"SKILL_VERSION\s*=\s*(\d+)", setup)
    version = int(m.group(1)) if m else 0

    manifest = {
        "name": "hermes-app-gateway",
        "skill_version": version,
        "files": files,
    }
    (SKILL / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    print(f"✓ 规范化 {len(FILES)} 个文件为 LF，已写 manifest.json (skill_version={version})")
    for rel, h in files.items():
        print(f"  {rel:28} {h[:16]}…")


if __name__ == "__main__":
    main()
