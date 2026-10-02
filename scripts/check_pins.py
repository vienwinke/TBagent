# -*- coding: utf-8 -*-
"""依赖固定自查：三个 requirements 的直接依赖必须 `==` 精确固定。

为什么要有它：这份仓库的指标（EX / 延迟 / 成本）依赖具体库版本 ——
sqlglot 的解析行为、pandas 的类型推断、openai SDK 的重试语义都会随版本变。
一旦有人把 `==` 改回 `>=`，README 里那批数字就又变成"只在这台机器上成立"。

退出码非 0 即阻断；CI 的门禁任务与本地都可直接跑：
    python scripts/check_pins.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 需要精确固定的文件；requirements-vector.txt 是可选项且未纳入门禁环境，故意排除
PINNED_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-sidecar.txt")

# 允许的浮动写法（本仓库一处都不允许，列出来是为了给出可读的错误信息）
FLOATING = re.compile(r"(>=|<=|~=|!=|(?<![=<>!~])>(?!=)|(?<![=<>!~])<(?!=))")
PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[^\]]+\])?==")


def check(path: Path) -> list[str]:
    problems: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-r ") or line.startswith("--"):
            continue
        if not PINNED.match(line):
            problems.append(f"{path.name}:{lineno} 直接依赖未精确固定: {raw.strip()}")
        elif FLOATING.search(line.split("==", 1)[1] if "==" in line else line):
            problems.append(f"{path.name}:{lineno} 版本里混入了范围写法: {raw.strip()}")
    return problems


def main() -> int:
    all_problems: list[str] = []
    for name in PINNED_FILES:
        p = ROOT / name
        if not p.exists():
            all_problems.append(f"{name} 不存在")
            continue
        all_problems.extend(check(p))
    if all_problems:
        print("依赖固定自查未通过：")
        for x in all_problems:
            print("  -", x)
        print("\n修法：把直接依赖写成 `包名==版本`（版本取自门禁通过的 venv），")
        print("      并重新生成 requirements.lock（uv pip compile --universal requirements-dev.txt）。")
        return 1
    print(f"依赖固定自查通过：{len(PINNED_FILES)} 个文件，直接依赖全部 `==` 精确固定")
    return 0


if __name__ == "__main__":
    sys.exit(main())
