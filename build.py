#!/usr/bin/env python3
"""把 src/* 打包成单文件 dist/setup.py，并生成 manifest.json（含 SHA256）。

用法:  python build.py

流程：把 src/gateway.py 与 src/provision.py 以 base64 注入 src/setup_main.py 的
占位符，输出 dist/setup.py（hermes 只需下载这一个文件），再算它的 SHA256 写入
dist/manifest.json。安装提示词里带的哈希应取自这里。
"""
import base64
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
SRC = ROOT / "src"
DIST = ROOT / "dist"


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def b64(name: str) -> str:
    # 规范化为 LF 再编码，保证跨平台字节稳定（Windows 写文件默认会插入 CRLF）
    data = _lf((SRC / name).read_text(encoding="utf-8")).encode("utf-8")
    return base64.b64encode(data).decode()


def main():
    DIST.mkdir(exist_ok=True)
    setup = _lf((SRC / "setup_main.py").read_text(encoding="utf-8"))
    setup = setup.replace('"__GATEWAY_PY_B64__"', json.dumps(b64("gateway.py")))
    setup = setup.replace('"__PROVISION_PY_B64__"', json.dumps(b64("provision.py")))

    if "__GATEWAY_PY_B64__" in setup or "__PROVISION_PY_B64__" in setup:
        raise SystemExit("✗ 占位符未完全替换，检查 setup_main.py")

    out = DIST / "setup.py"
    raw = setup.encode("utf-8")  # 纯 LF 字节，与 git/jsDelivr/Linux 主机一致
    out.write_bytes(raw)

    digest = hashlib.sha256(raw).hexdigest()
    m = re.search(r"SKILL_VERSION\s*=\s*(\d+)", setup)
    version = int(m.group(1)) if m else 0

    manifest = {
        "name": "hermes-app-gateway",
        "skill_version": version,
        "entry": "setup.py",
        "sha256": digest,
        "size": len(raw),
    }
    (DIST / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    print(f"✓ dist/setup.py        {len(raw)} bytes")
    print(f"  skill_version        {version}")
    print(f"  sha256               {digest}")


if __name__ == "__main__":
    main()
