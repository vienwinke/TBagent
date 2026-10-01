# -*- coding: utf-8 -*-
"""成本核算与档位接线。

两个真实缺陷：
1. **成本统计用单一单价**：项目有主档 / 便宜档两个模型，`cost_yuan` 却拿一个单价乘总 token ——
   便宜档的调用被按主档计价，账目偏高且无法解释（成本面板与审计金额都跟着错）。
2. **`LLM_MODEL_CHEAP` 全仓无人使用**：设计文档明确要求 route / scope / summary / suggest 走便宜档，
   但转述与路由分类一直跑主档 —— 与"policy 实现了却没接线"是同一类问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from agent import answer, router  # noqa: E402
from config import LLM  # noqa: E402

MANY_ROWS = [["A", 1], ["B", 2], ["C", 3]]      # 多行多列 → 不走模板，必须调模型


def test_price_for_returns_main_price_for_main_and_unknown_models():
    assert LLM.price_for(LLM.model) == (LLM.price_in, LLM.price_out)
    assert LLM.price_for("某个不认识的模型") == (LLM.price_in, LLM.price_out)
    assert LLM.price_for(None) == (LLM.price_in, LLM.price_out)


def test_price_for_returns_cheap_price_for_cheap_model():
    if LLM.model == LLM.model_cheap:
        return          # 两档配成同一个模型时无从区分（本地演示环境可能如此）
    assert LLM.price_for(LLM.model_cheap) == (LLM.price_in_cheap, LLM.price_out_cheap)


def test_usage_cost_is_summed_per_model():
    """两个模型的用量必须各按自己的单价计价"""
    u = llm.Usage()
    u.add(LLM.model, 1_000_000, 0, 1.0)
    u.add(LLM.model_cheap, 1_000_000, 0, 1.0)
    expect = LLM.price_in + LLM.price_for(LLM.model_cheap)[0]
    assert abs(u.cost_yuan - expect) < 1e-9
    # 单一单价算法（旧实现）在便宜档单价不同时必然给出不同结果
    naive = 2_000_000 / 1e6 * LLM.price_in
    if LLM.price_for(LLM.model_cheap)[0] != LLM.price_in:
        assert abs(u.cost_yuan - naive) > 1e-9, "旧口径会把便宜档按主档计价"


def test_usage_keeps_totals_and_per_model_breakdown():
    u = llm.Usage()
    u.add(LLM.model, 100, 20, 0.5)
    u.add(LLM.model_cheap, 50, 5, 0.5)
    assert (u.prompt_tokens, u.completion_tokens, u.calls) == (150, 25, 2)
    assert u.total_tokens == 175
    assert u.summary()["by_model"] == {LLM.model: 1, LLM.model_cheap: 1}


def test_summary_uses_cheap_tier(monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return "一句话答案"

    monkeypatch.setattr(llm, "chat", fake_chat)
    out = answer.summarize("问个数", ["名称", "数量"], MANY_ROWS, template_first=False)

    assert out == "一句话答案"
    assert seen.get("model") == LLM.model_cheap, "转述必须走便宜档"


def test_summary_payload_is_compact(monkeypatch):
    """转述 payload 用表头 + 行的 CSV，而不是每行重复列名的 dict 列表"""
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "x"

    monkeypatch.setattr(llm, "chat", fake_chat)
    answer.summarize("问个数", ["名称", "数量"], MANY_ROWS, template_first=False)

    text = captured["user"]
    assert "名称,数量" in text, "应有 CSV 表头"
    assert text.count("名称") == 1, "列名不应每行重复"
    assert "A,1" in text and "行数：3" in text


def test_summary_truncates_long_cells(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "x"

    monkeypatch.setattr(llm, "chat", fake_chat)
    long_text = "很长的内容" * 30
    answer.summarize("问", ["说明", "数量"], [[long_text, 1], ["b", 2], ["c", 3]], template_first=False)

    assert "…" in captured["user"], "超长单元格应被截断，避免撑爆转述 prompt"


def test_router_classify_uses_cheap_tier(monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return "data"

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert router.llm_classify("随便问问") == router.DATA
    assert seen.get("model") == LLM.model_cheap, "分类只需 8 个输出 token，必须走便宜档"
