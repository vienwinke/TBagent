# -*- coding: utf-8 -*-
"""知识库 / RAG / 三分类路由 的单测（全部可离线，不依赖 API Key）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import kb as kb_mod  # noqa: E402
from agent import rag, router  # noqa: E402


# ---------------- 切分 ----------------
def test_split_preserves_headings():
    text = "# 标题A\n内容一。\n\n## 标题B\n内容二。"
    chunks = kb_mod.split_markdown(text, chunk_size=100)
    heads = [h for h, _ in chunks]
    assert "标题A" in heads and "标题B" in heads


def test_split_respects_chunk_size_and_overlaps():
    body = "。".join("句子%d这是内容" % i for i in range(60))
    chunks = kb_mod.split_markdown("# H\n" + body, chunk_size=120, overlap=20)
    assert len(chunks) > 1
    # 允许重叠，但不应远超上限
    assert all(len(c) <= 120 + 40 for _, c in chunks)


def test_split_single_short_section():
    chunks = kb_mod.split_markdown("# H\n很短的内容", chunk_size=300)
    assert chunks == [("H", "很短的内容")]


# ---------------- RRF ----------------
def test_rrf_prefers_docs_in_both_rankings():
    fused = kb_mod.rrf_fuse([[1, 2, 3], [3, 1, 4]])
    order = [i for i, _ in fused]
    assert order[0] in (1, 3)          # 两路都出现的 1 与 3 应排前
    assert order.index(4) > order.index(1)


# ---------------- 检索 ----------------
def test_kb_loads_corpus():
    s = kb_mod.stats()
    assert s["chunks"] >= 10
    assert len(s["docs"]) == 3


def test_retrieval_hits_expected_sections():
    cases = [
        ("任务有哪些状态？", "状态机"),
        ("信用分初始是多少？", "信用分"),
        ("怎么发布任务？", "发布任务"),
        ("敏感字段会被怎么处理？", "安全与隐私"),
        ("审核超时了怎么办？", "审核"),
    ]
    k = kb_mod.KnowledgeBase()
    ok = 0
    for q, expect in cases:
        hits = k.retrieve(q, top_k=3)
        blob = " ".join(h.chunk.heading + h.chunk.text for h in hits)
        if expect in blob:
            ok += 1
    assert ok == len(cases), "检索命中 %d/%d" % (ok, len(cases))


def test_context_has_citations():
    k = kb_mod.KnowledgeBase()
    hits = k.retrieve("信用分规则", top_k=2)
    ctx = k.context(hits)
    assert "【资料" in ctx and hits[0].chunk.doc in ctx


# ---------------- RAG ----------------
def test_rag_returns_answer_and_citations():
    def fake(messages):
        return {"answer": "新用户信用分初始为 100。", "used_sources": [1], "insufficient": False}

    r = rag.answer("信用分初始是多少？", llm_fn=fake)
    assert r.ok and "100" in r.answer
    assert r.citations and not r.insufficient


def test_rag_marks_insufficient():
    def fake(messages):
        return {"answer": "资料中没有相关内容。", "used_sources": [], "insufficient": True}

    r = rag.answer("公司年假有多少天？", llm_fn=fake)
    assert r.insufficient


def test_rag_prompt_contains_constraint_and_context():
    captured = {}

    def fake(messages):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return {"answer": "ok", "used_sources": [1], "insufficient": False}

    rag.answer("接取名额怎么算？", llm_fn=fake)
    assert "仅根据提供的资料" in captured["system"]
    assert "【资料" in captured["user"] and "【问题】" in captured["user"]


def test_rag_handles_llm_error():
    def boom(messages):
        raise RuntimeError("429")

    r = rag.answer("任务状态", llm_fn=boom)
    assert not r.ok and "429" in (r.error or "")


# ---------------- 三分类路由 ----------------
def test_route_chat():
    for q in ["你好", "你是谁？", "谢谢"]:
        assert router.route(q) == router.CHAT, q


def test_route_knowledge():
    for q in ["任务有哪些状态？", "怎么发布任务？", "信用分规则是什么", "审核超时了怎么办"]:
        assert router.route(q) == router.KNOWLEDGE, q


def test_route_data():
    for q in ["待接取的任务有几个？", "最近 7 天每天新增的接取数量", "平均赏金是多少",
              "统计各状态任务分布", "今天的登录失败次数是多少？"]:
        assert router.route(q) == router.DATA, q


def test_direct_sql_goes_to_data():
    assert router.route("SELECT COUNT(*) FROM task") == router.DATA


def test_route_falls_back_to_classifier():
    def classify(q):
        return router.DATA

    # 含业务名词但无线索 → 交给分类器
    assert router.route("任务那块东西", classify_fn=classify) == router.DATA


def test_route_classifier_failure_defaults_to_data():
    def boom(q):
        raise RuntimeError("network")

    assert router.route("嗯嗯这个问题有点意思啊随便聊聊", classify_fn=boom) == router.DATA

def test_citations_use_labels_and_drop_invalid():
    """回归：模型引用错来源时，不得展示不存在的来源（实测曾把业务规则引成常见问题）"""
    captured = {}

    def fake(messages):
        # 故意给一个不存在的来源 + 一个真实标签
        captured["n"] = 1
        return {"answer": "6 种状态。", "used_sources": ["不存在的文档", "01-业务规则.md › 任务状态机"],
                "insufficient": False}

    r = rag.answer("任务有哪些状态？", llm_fn=fake)
    assert r.citations == ["01-业务规则.md › 任务状态机"]


def test_citations_fallback_when_all_invalid():
    def fake(messages):
        return {"answer": "x", "used_sources": ["完全不对的来源"], "insufficient": False}

    r = rag.answer("任务有哪些状态？", llm_fn=fake)
    assert r.citations, "全部无效时应回退到 Top-2 来源"
    assert all(c.endswith((".md", "机", "则", "题", "隐私", "规则", "状态机")) or ".md" in c for c in r.citations)


def test_out_of_domain_questions_never_go_to_data():
    """回归：库外问题（无业务名词）不得进入 SQL 生成分支

    实测踩坑："公司年假有多少天" 因含"多少"被送进数据分支，生成了无关 SQL。
    """
    for q in ["公司年假有多少天？", "今天北京的天气怎么样？", "你们支持信用卡支付吗？"]:
        got = router.route(q)
        assert got != router.DATA, q
