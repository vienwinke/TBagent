# -*- coding: utf-8 -*-
"""源码可解析性门禁：仓库里每个 Python 文件都必须能被 `ast.parse`。

为什么值得单独立一条：写测试/文档时很容易写出**引号嵌套导致的未闭合字符串**
（本项目在三次不同文件上踩到同一个坑：docstring 结尾紧跟 `"` 系列），
以及历史上出现过的 UTF-8 BOM（会让按纯 utf-8 读取的工具链在第 1 行崩掉）。
这类错误在 `pytest` 收集阶段才炸，且报错指向的是"测试文件本身"，容易误判成环境问题。
这里提前全仓扫一遍，报错直接指出文件与行号。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True).stdout.split()
    return [ROOT / f for f in out if f and (ROOT / f).is_file()]


def test_all_python_sources_parse():
    files = _tracked_python_files()
    assert files, "没有发现 Python 文件，检查 git ls-files 是否可用"
    broken = []
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append("%s:%s %s" % (path.relative_to(ROOT), exc.lineno, exc.msg))
    assert not broken, "以下文件无法解析：\n" + "\n".join(broken)


def test_no_utf8_bom_in_tracked_files():
    """BOM 会让 ast.parse / 某些工具链在首行崩掉（曾经有 18 个文件带 BOM）"""
    with_bom = []
    for path in _tracked_python_files():
        if path.read_bytes()[:3] == b"\xef\xbb\xbf":
            with_bom.append(str(path.relative_to(ROOT)))
    assert not with_bom, "以下文件带 UTF-8 BOM：%s" % ", ".join(with_bom)
