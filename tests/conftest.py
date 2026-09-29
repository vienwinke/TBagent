# -*- coding: utf-8 -*-
"""测试隔离：SQL 缓存是进程级单例且落盘，会在用例之间串味。

例如 test_nl2sql_pipeline 里多个用例都问"随便问问"，前一个把结果写进缓存后，
后一个期望"模型报错"的用例会直接命中缓存 → 断言失败（实测踩到）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolate_sql_cache(tmp_path, monkeypatch):
    from agent import cache as cache_mod

    fresh = cache_mod.SqlCache(path=tmp_path / "sql_cache.json", ttl_sec=600, enabled=True)
    monkeypatch.setattr(cache_mod, "_CACHE", fresh, raising=False)
    yield fresh
    cache_mod._CACHE = None
