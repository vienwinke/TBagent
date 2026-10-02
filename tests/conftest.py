# -*- coding: utf-8 -*-
"""测试隔离：SQL 缓存是进程级单例且落盘，会在用例之间串味。

例如 test_nl2sql_pipeline 里多个用例都问"随便问问"，前一个把结果写进缓存后，
后一个期望"模型报错"的用例会直接命中缓存 → 断言失败（实测踩到）。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ============================================================================
# 让测试**不依赖 .env 里有没有 key**（「离线门禁」名实相符的关键一步）
#
# 为什么不能只靠各用例自己 monkeypatch llm.client：
#   `llm.chat()` 在 llm.py:212 有一道前置校验 `if not LLM.configured: raise`，
#   它在 `client()` **之前**抛错 —— 没有 key 的环境里，桩永远轮不到。
#   于是那些用例只是"碰巧"过（本机有 .env / CI 没有）。
#   实测：干净检出 5 failed，本机 401 passed。
#
# 为什么放在 conftest 顶层而不是 fixture 里：
#   `config.py` 的 LLM/DB 是**模块级单例**，import 即定型；必须在任何测试模块
#   导入 config 之前设好。而 `load_dotenv(ROOT/'.env')` 默认**不覆盖**已有环境
#   变量，所以这里的值会赢过 .env —— 本地与 CI 因此行为一致。
#
# 占位值必须通过 `config.LLMConfig.issues()` 的三项检查：纯 ASCII、长度 ≥20、
# 不含 your/你的/changeme/placeholder/xxxx/todo（否则会以"配置有误"再抛一次）。
# 它**不是**密钥，也不会被密钥门禁误报（不含 `LLM_API_KEY=` 字面量形态）。
# ============================================================================
os.environ["LLM_API_KEY"] = "unit-test-only-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def isolate_sql_cache(tmp_path, monkeypatch):
    from agent import cache as cache_mod

    fresh = cache_mod.SqlCache(path=tmp_path / "sql_cache.json", ttl_sec=600, enabled=True)
    monkeypatch.setattr(cache_mod, "_CACHE", fresh, raising=False)
    yield fresh
    cache_mod._CACHE = None
