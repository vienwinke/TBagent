# -*- coding: utf-8 -*-
"""密钥扫描核心：纯函数，无副作用，可被单测与 pre-commit 钩子直接复用。

为什么单独一个模块：prepublish_check.py 是 CLI 流程（import 即执行），
判定逻辑放这里才能被 tests 直接 import 而不触发起一次全量审计。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

# (正则, 描述, 取值组号；0 = 取整个匹配)
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI/DeepSeek 风格密钥", 0),
    # 注意用 [ \t]* 而非 \s*：否则 "LLM_API_KEY=" 会跨行匹配到下一行（实测误报）。
    # 该条覆盖非 sk- 前缀的供应商密钥（如 CommandCode 的 user_… 形态，
    # 26bc0b2 泄漏进 .env.example 的就是这种，纯 sk- 正则会漏掉）。
    (r"(LLM_API_KEY|DEEPSEEK_API_KEY)[ \t]*=[ \t]*[\"']?([A-Za-z0-9_\-]{12,})",
     "环境变量里的真实密钥", 2),
    (r"password\s*=\s*[\"']([^\"']{8,})[\"']", "硬编码密码", 1),
]

# 占位符白名单：这些写法不是密钥。缺了它门禁会对 .env.example 常年误报
# （实测退出码 1），而常年变红的门禁等于没有门禁 —— 那正是密钥被提交进来的土壤。
PLACEHOLDER_HINTS = ("your", "placeholder", "changeme", "change-me", "example", "dummy",
                     "sample", "fake", "todo", "xxxx", "none", "redacted", "***")

# 二进制/压缩包不参与文本扫描（避免误报，也别让钩子去解码 86KB 的 sqlite）
BINARY_SUFFIXES = (".sqlite", ".db", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
                   ".pdf", ".zip", ".gz", ".whl", ".woff", ".woff2", ".ttf", ".mp4")

# 扫描器自身：源码里就是模式串与用例，没有真实密钥，跳过以免自hit
SELF_FILES = ("prepublish_check.py", "secret_scan.py")


def is_placeholder(value: str) -> bool:
    """判断一个疑似密钥的值是否是占位符写法。"""
    v = value.strip().strip("\"'").lower()
    if not v:
        return True
    return any(h in v for h in PLACEHOLDER_HINTS)


def scan_secrets(text: str) -> list[tuple[str, str]]:
    """扫描一段文本，返回 [(描述, 命中片段), ...]；占位符已过滤。"""
    found: list[tuple[str, str]] = []
    for pat, desc, value_group in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            value = m.group(value_group) if value_group else m.group(0)
            if is_placeholder(value):
                continue
            found.append((desc, m.group(0)[:40]))
    return found


def iter_secret_hits(root: Path | str, files: list[str],
                     max_bytes: int = 2_000_000) -> Iterator[tuple[str, str, str]]:
    """遍历文件清单，产出 (文件, 描述, 片段)。二进制/超大文件/扫描器自身跳过。"""
    base = Path(root)
    for f in files:
        if f.endswith(SELF_FILES) or f.lower().endswith(BINARY_SUFFIXES):
            continue
        p = base / f
        try:
            if not p.is_file() or p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for desc, snippet in scan_secrets(text):
            yield f, desc, snippet
