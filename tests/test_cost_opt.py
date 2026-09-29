# -*- coding: utf-8 -*-
"""成本优化三件事的单测：SQL 缓存 / 结果转述模板化 / Schema 精简"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import answer as ans
from agent import cache as cache_mod
from agent import nl2sql
from agent import schema_index as si

GOOD = {"sql": "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0", "reason": "统计"}


# ---------- 1) SQL 缓存 ----------
def _tmp_cache(tmp_path, ttl=600):
    return cache_mod.SqlCache(path=tmp_path / "c.json", ttl_sec=ttl, enabled=True)


def test_cache_put_get_and_normalization(tmp_path):
    c = _tmp_cache(tmp_path)
    c.put("待接取的任务有几个？", "SELECT 1")
    assert c.get("待接取的任务有几个？")["sql"] == "SELECT 1"
    # 归一化：标点/空格差异应命中同一条
    assert c.get("待接取的任务有几个?")["sql"] == "SELECT 1"
    assert c.get(" 待接取的任务有几个？ ")["sql"] == "SELECT 1"


def test_cache_ttl_expiry(tmp_path):
    c = _tmp_cache(tmp_path, ttl=600)
    c.put("q", "SELECT 1")
    assert c.get("q") is not None
    # 回拨时间戳 → 超过 TTL 应视为过期（ttl<=0 表示永不过期）
    key = c._key("q", None)
    c._data[key]["ts"] = time.time() - 1200
    assert c.get("q") is None
    c.ttl = 0
    c._data[key]["ts"] = time.time() - 10 ** 6
    assert c.get("q") is not None, "ttl<=0 应永不过期"


def test_cache_disabled(tmp_path):
    c = cache_mod.SqlCache(path=tmp_path / "c.json", enabled=False)
    c.put("q", "SELECT 1")
    assert c.get("q") is None and c.summary()["entries"] == 0


def test_cache_hit_rate_and_clear(tmp_path):
    c = _tmp_cache(tmp_path)
    c.put("q", "SELECT 1")
    c.get("q"); c.get("miss")
    s = c.summary()
    assert s["hits"] == 1 and s["misses"] == 1 and s["hit_rate"] == 0.5
    assert c.clear() == 1 and c.summary()["entries"] == 0


def test_nl2sql_second_call_uses_cache_and_skips_llm():
    """关键收益验证：同样的问题第二次问，不调用模型"""
    calls = {"n": 0}

    def stub(messages):
        calls["n"] += 1
        return GOOD

    first = nl2sql.answer("缓存测试：任务总数是多少？", llm_fn=stub, use_cache=True)
    assert first.ok and not first.cache_hit and calls["n"] == 1
    second = nl2sql.answer("缓存测试：任务总数是多少？", llm_fn=stub, use_cache=True)
    assert second.ok and second.cache_hit and calls["n"] == 1, "第二次不应再调模型"
    assert second.query["row_count"] == first.query["row_count"]  # 重新执行过，结果一致


def test_nl2sql_cache_can_be_disabled():
    calls = {"n": 0}

    def stub(messages):
        calls["n"] += 1
        return GOOD

    nl2sql.answer("禁用缓存测试：任务数？", llm_fn=stub, use_cache=False)
    nl2sql.answer("禁用缓存测试：任务数？", llm_fn=stub, use_cache=False)
    assert calls["n"] == 2


# ---------- 2) 结果转述模板化 ----------
def test_simple_result_uses_template_without_llm():
    called = {"n": 0}

    def boom(messages):
        called["n"] += 1
        return "不应被调用"

    out = ans.summarize("任务总数？", ["任务数"], [(8,)], llm_fn=boom)
    assert called["n"] == 0 and "8" in out and "任务数" in out


def test_single_column_multi_row_uses_template():
    called = {"n": 0}
    out = ans.summarize("所有任务标题", ["标题"], [("a",), ("b",)], llm_fn=lambda m: called.update(n=1))
    assert called["n"] == 0 and "2 行" in out


def test_complex_result_still_calls_llm():
    called = {"n": 0}

    def fake(messages):
        called["n"] += 1
        return "多行多列结论"

    out = ans.summarize("每个发布者发布多少任务", ["昵称", "任务数"],
                        [("a", 1), ("b", 2)], llm_fn=fake)
    assert called["n"] == 1 and out == "多行多列结论"


# ---------- 3) Schema 精简 ----------
def test_compact_schema_is_shorter():
    idx = si.SchemaIndex()
    full = idx.describe(["task"], max_columns=0, desc_max=0)
    compact = idx.describe(["task"], max_columns=6, desc_max=12)
    assert len(compact) < len(full), "精简后应更短"
    assert compact.count("\n") < full.count("\n")
    assert "其余列" in compact          # 未展示的列仍以名字列出，模型知道它们存在
    assert "id" in compact and "status" in compact
