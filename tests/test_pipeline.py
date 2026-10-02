# -*- coding: utf-8 -*-
"""编排层（agent/pipeline.py）：事件序列 / 拒答前移 / 权限可见性 —— **全部离线**。

注入 Deps 里的桩就不碰网络，这让"链路"可以像状态机一样断言。
这正是把编排与模型调用分开的直接收益：FastAPI 接入时同一份事件流换成 SSE 帧即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import pipeline, rag, router  # noqa: E402
from agent.pipeline import Deps  # noqa: E402
from agent.policy import ROLE_ADMIN, Principal  # noqa: E402

USER = Principal(user_id=7)
ADMIN = Principal(user_id=1, role=ROLE_ADMIN)

SQL = "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0"
GOOD = {"sql": SQL, "reason": "统计任务数", "tables": ["task"]}
NL2SQL_DEPS = Deps(nl2sql_llm=lambda messages: GOOD,
                   summary_llm=lambda messages: "共 8 个任务。")


def run(question: str, principal: Principal = USER, **kwargs) -> list[tuple[str, dict]]:
    return list(pipeline.answer_stream(question, principal, **kwargs))


def names(events: list[tuple[str, dict]]) -> list[str]:
    return [e for e, _ in events]


def payload(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [d for e, d in events if e == name]


# ------------------------------------------------------------------ 主链路
def test_data_question_event_order():
    """meta → scope → route → sql → table → delta → done（顺序即契约 §2.3）"""
    evs = run("待接取的任务有几个？", USER, deps=NL2SQL_DEPS, use_cache=False)
    seq = names(evs)

    assert seq[0] == "meta"
    assert seq[-1] == "done"
    assert seq.index("scope") < seq.index("route") < seq.index("sql")
    assert seq.index("sql") < seq.index("table") < seq.index("delta")
    assert payload(evs, "table")[0]["row_count"] >= 1
    assert "任务数" in "".join(p["text"] for p in payload(evs, "delta"))


def test_meta_carries_versions_and_trace_id():
    evs = run("待接取的任务有几个？", USER, deps=NL2SQL_DEPS, trace_id="trace-fixed", use_cache=False)
    meta = payload(evs, "meta")[0]

    assert meta["trace_id"] == "trace-fixed"
    assert meta["policy_version"] and meta["prompt_version"] and meta["model"]


def test_done_reports_usage_and_flags():
    done = payload(run("待接取的任务有几个？", USER, deps=NL2SQL_DEPS, use_cache=False), "done")[0]

    for key in ("elapsed_ms", "tokens", "cost_yuan", "cache_hit", "repaired", "attempts"):
        assert key in done, "done 事件缺少契约字段 %s" % key
    assert done["denied"] is False and done["route"] == router.DATA


# ------------------------------------------------------------------ 权限可见性
def test_sql_text_only_for_privileged():
    user_sql = payload(run("待接取的任务有几个？", USER, deps=NL2SQL_DEPS, use_cache=False), "sql")[0]
    admin_sql = payload(run("待接取的任务有几个？", ADMIN, deps=NL2SQL_DEPS, use_cache=False), "sql")[0]

    assert user_sql == {"has_sql": True}, "普通用户不得拿到 SQL 明文"
    assert admin_sql["sql"] and admin_sql["tables"], "运营及以上应拿到 SQL 与命中表"


# ------------------------------------------------------------------ 拒答前移
def test_denied_question_never_generates():
    """语义越权：只发 meta/scope/delta/done，且**一次模型调用都没有**"""
    calls = []

    def counting_llm(messages):
        calls.append(1)
        return GOOD

    evs = run("平台一共有多少条审计记录", USER, deps=Deps(nl2sql_llm=counting_llm), use_cache=False)
    seq = names(evs)

    assert seq == ["meta", "scope", "delta", "done"], seq
    assert payload(evs, "scope")[0]["allowed"] is False
    assert payload(evs, "done")[0]["denied"] is True
    assert calls == [], "拒答必须发生在生成之前"
    assert "运营" in payload(evs, "delta")[0]["text"]


# ------------------------------------------------------------------ 分支
def test_knowledge_branch_emits_citations(monkeypatch):
    fake = rag.RagResult(question="q", answer="任务有 OPEN / IN_PROGRESS 等状态。",
                         citations=["01-业务规则.md › 任务状态机"],
                         hits=[{"heading": "01-业务规则.md › 任务状态机", "text": "状态机原文片段"}])
    monkeypatch.setattr(pipeline.rag, "answer", lambda q, **kw: fake)

    evs = run("任务有哪些状态？", USER, deps=Deps(rag_llm=lambda messages: {}))
    cites = payload(evs, "citations")[0]

    assert payload(evs, "route")[0]["route"] == router.KNOWLEDGE
    assert cites == [{"title": "01-业务规则.md › 任务状态机", "snippet": "状态机原文片段"}]
    assert "OPEN" in payload(evs, "delta")[0]["text"]
    assert names(evs)[-1] == "done"


def test_chat_branch_is_terminal():
    evs = run("你好", USER, deps=Deps())
    seq = names(evs)

    assert payload(evs, "route")[0]["route"] == router.CHAT
    assert "scope" in seq and "delta" in seq and seq[-1] == "done"
    assert "sql" not in seq and "table" not in seq


# ------------------------------------------------------------------ 失败路径
def test_model_failure_becomes_error_event():
    def boom(messages):
        raise RuntimeError("429 Too Many Requests")

    evs = run("待接取的任务有几个？", USER, deps=Deps(nl2sql_llm=boom), use_cache=False)
    err = payload(evs, "error")[0]

    assert err["code"] == "LLM_UNAVAILABLE" and err["retryable"] is True
    assert names(evs)[-1] == "done", "失败也要正常收尾（前端据此收流）"


def test_guard_events_describe_actions():
    """护栏提示由 pipeline 统一产出，前端不需要理解内部机制"""
    evs = run("待接取的任务有几个？", USER, deps=NL2SQL_DEPS, use_cache=False)
    actions = {g["action"] for g in payload(evs, "guard")}

    assert "limit_added" in actions, "原 SQL 无 LIMIT，应产生 limit_added 提示"
    assert all(g.get("note") for g in payload(evs, "guard"))


# ------------------------------------------------------------------ Streamlit 适配器
def test_app_handle_maps_events_to_item():
    """app.handle 只是事件流的渲染适配器：编排逻辑不重复实现"""
    import app

    item = app.handle("待接取的任务有几个？", deps=NL2SQL_DEPS)

    assert item["intent"] == router.DATA
    assert item["denied"] is False
    assert item["query"]["columns"] == ["任务数"]
    assert item["rows"] == [(8,)]
    assert {g["action"] for g in item["guards"]} == {"limit_added"}
    assert item["answer_text"]


def test_app_handle_renders_deny_text():
    """拒答渲染成话术 + 判定依据，而不是"未能完成查询"式的系统口吻"""
    import app

    item = app.handle("把 password_hash 导出来", deps=Deps())

    assert item["denied"] is True and item["scope"] == "SENSITIVE"
    assert "安全中心" in item["answer_text"]
    assert "未知错误" not in item["answer_text"]


# ------------------------------------------------------------------ 裸 SQL 入口收口（P1）
def test_raw_sql_entry_closed_for_user_without_calling_classifier():
    """普通用户直写 SQL：不生成、不查库，也不为这种输入花一次分类调用"""
    calls = []

    def classify(question):
        calls.append(question)
        return router.DATA

    assert router.route("select * from user", classify_fn=classify) == router.DATA
    assert router.route("select * from user", classify_fn=classify,
                        allow_raw_sql=False) == router.CHAT
    assert calls == [], "裸 SQL 由规则直接判定：开/关入口都不该调用分类器"


def test_normal_questions_unaffected_by_raw_sql_gating():
    assert router.route("我接了几个任务", allow_raw_sql=False) == router.DATA
    assert router.route("任务有哪些状态？", allow_raw_sql=False) == router.KNOWLEDGE


def test_pipeline_closes_raw_sql_for_user():
    calls = []

    def counting_llm(messages):
        calls.append(1)
        return GOOD

    evs = run("select * from user", USER, deps=Deps(nl2sql_llm=counting_llm))
    assert payload(evs, "route")[0]["route"] == router.CHAT
    assert "sql" not in names(evs) and calls == []


def test_pipeline_keeps_raw_sql_for_privileged():
    evs = run("select * from task limit 5", ADMIN, deps=NL2SQL_DEPS, use_cache=False)
    assert payload(evs, "route")[0]["route"] == router.DATA
    assert payload(evs, "sql")[0]["sql"]


# ------------------------------------------------------------------ 错误码映射
def test_error_event_prefers_model_side_over_timeout():
    """模型调用超时必须报 LLM_UNAVAILABLE：报成 SQL_TIMEOUT 会让前端去重试 SQL"""
    ev, data = pipeline.error_event("生成阶段失败: 模型调用失败（尝试 4 次）: APITimeoutError: Request timed out.")
    assert ev == "error" and data["code"] == "LLM_UNAVAILABLE"

    ev2, data2 = pipeline.error_event("执行失败: SQL 执行超时（3000ms）")
    assert data2["code"] == "SQL_TIMEOUT"

    _, data3 = pipeline.error_event("策略拒绝[DENY_GUARD]: 写操作")
    assert data3["code"] == "INTERNAL"


# ------------------------------------------------------------------ 多轮：指代消解（P3）
def _rewrite(**payload):
    """指代消解桩：返回一个 dict；调用次数用列表统计"""
    calls = []

    def fn(messages):
        calls.append(messages)
        return payload

    fn.calls = calls
    return fn


def test_history_triggers_rewrite_and_rejudges_scope():
    """★ 安全关键：追问不能绕过上一轮的范围判定。

    "那上个月呢" 本身不含任何范围线索（规则放行），但消解成
    "平台上个月总成交额" 之后必须被判 PLATFORM 并拒答。
    """
    rw = _rewrite(standalone_question="平台上个月总成交额是多少", need_clarify=False)
    calls = []
    evs = run("那上个月呢", USER, use_cache=False, history="用户：平台总成交额是多少\n助手：全平台统计仅对运营开放。",
              deps=Deps(nl2sql_llm=lambda m: (calls.append(m), GOOD)[1], rewrite_llm=rw))
    seq = names(evs)

    assert len(rw.calls) == 1, "有历史时才做指代消解"
    assert payload(evs, "scope")[0]["scope"] == "PLATFORM"
    assert payload(evs, "scope")[0]["allowed"] is False
    assert payload(evs, "done")[0]["denied"] is True
    assert calls == [], "被拒的追问不该调用生成模型"
    assert seq == ["meta", "scope", "delta", "done"], seq


def test_original_denial_wins_even_if_rewrite_looks_benign():
    """原问题本身就是越权时，即使消解结果看起来无害也必须拒答（双侧判定）"""
    rw = _rewrite(standalone_question="我接了几个任务", need_clarify=False)
    evs = run("把 password_hash 导出来", USER, use_cache=False,
              history="用户：你好\n助手：你好",
              deps=Deps(nl2sql_llm=lambda m: GOOD, rewrite_llm=rw))
    assert payload(evs, "scope")[0]["allowed"] is False
    assert payload(evs, "done")[0]["deny_reason"] == "DENY_SENSITIVE"


def test_clarify_path_asks_instead_of_answering():
    rw = _rewrite(need_clarify=True, clarify_question="你指的是哪个任务？")
    calls = []
    evs = run("那他呢", USER, use_cache=False, history="用户：任务《代取快递》怎么样\n助手：已结算",
              deps=Deps(nl2sql_llm=lambda m: (calls.append(m), GOOD)[1], rewrite_llm=rw))

    assert [e for e, _ in evs][:2] == ["meta", "scope"]
    assert "你指的是哪个任务？" in payload(evs, "delta")[0]["text"]
    assert calls == [], "指代不明时不应生成 SQL"
    assert payload(evs, "done")[0]["clarify"] is True


def test_rewrite_failure_falls_back_to_original_question():
    def boom(messages):
        raise RuntimeError("改写服务 503")

    evs = run("待接取的任务有几个？", USER, use_cache=False, history="用户：你好\n助手：你好",
              deps=Deps(nl2sql_llm=lambda m: GOOD, rewrite_llm=boom,
                        summary_llm=lambda m: "共 8 个任务。"))
    assert payload(evs, "done")[0]["stage"] == "done", "改写失败不该影响正常作答"


def test_rewrite_uses_prompts_user_template():
    """改写必须走提示词包里的模板（而不是随手拼 prompt）"""
    rw = _rewrite(standalone_question="我上周接了几个任务")
    run("那上周呢", USER, use_cache=False, history="用户：我接了几个任务",
        deps=Deps(nl2sql_llm=lambda m: GOOD, rewrite_llm=rw))
    text = "".join(str(m) for m in rw.calls[0])
    assert "改写" in text and "standalone_question" in text


def test_no_history_means_no_rewrite_and_no_clarify():
    rw = _rewrite(standalone_question="不该被用到")
    run("待接取的任务有几个？", USER, use_cache=False, deps=NL2SQL_DEPS)
    assert rw.calls == []


def test_session_ref_lands_in_audit_payload():
    got = []
    list(pipeline.answer_stream("待接取的任务有几个？", USER, use_cache=False,
                                deps=NL2SQL_DEPS, session_ref=42, audit_sink=got.append))
    assert got[0]["session_id"] == 42, "审计要能关联到会话主键（BIGINT）"


def test_ellipsis_followup_inherits_denied_scope_from_history():
    """★ 真实冒烟发现的漏洞：改写模型可能把范围收窄（把"平台…"改写成只问自己）。

    "那上个月呢"本身不带任何范围线索，若只信改写结果就会被绕过去 ——
    所以再加一层**确定性**兜底：历史里被拒过的范围，省略式追问按同样原因拒答。
    """
    rw = _rewrite(standalone_question="上个月的审计记录有多少条", need_clarify=False)  # 模型把范围收窄了
    hist = "用户：平台一共有多少条审计记录\n助手：全平台统计仅对运营开放。"
    evs = run("那上个月呢", USER, use_cache=False, history=hist,
              deps=Deps(nl2sql_llm=lambda m: GOOD, rewrite_llm=rw))

    assert payload(evs, "scope")[0]["allowed"] is False, "改写丢了范围也不能放行"
    assert payload(evs, "done")[0]["deny_reason"] == "DENY_PLATFORM"
    assert payload(evs, "scope")[0]["reason"].startswith("追问未带范围线索")


def test_followup_with_explicit_self_scope_is_not_over_denied():
    """反向用例：用户明确说"我的"，就该按本人数据放行 —— 兜底不能把正常追问也拒了"""
    hist = "用户：平台一共有多少条审计记录\n助手：全平台统计仅对运营开放。"
    evs = run("我上周接了几个任务？", USER, use_cache=False, history=hist,
              deps=Deps(nl2sql_llm=lambda m: GOOD, summary_llm=lambda m: "共 8 个任务。"))
    assert payload(evs, "scope")[0]["allowed"] is True
    assert payload(evs, "done")[0]["stage"] == "done"


def test_history_without_denial_does_not_block_followup():
    """历史里没有越权内容时，省略式追问照常放行"""
    hist = "用户：我接了几个任务\n助手：你接了 8 个任务。"
    evs = run("那上周呢", USER, use_cache=False, history=hist, deps=NL2SQL_DEPS)
    assert payload(evs, "scope")[0]["allowed"] is True


def test_session_ref_must_be_an_integer():
    """类型守卫：外部字符串 session_id 不能当会话主键用（否则 MySQL 报 Incorrect integer）"""
    with pytest.raises(TypeError, match="session_ref"):
        list(pipeline.answer_stream("你好", USER, use_cache=False, deps=NL2SQL_DEPS,
                                    session_ref="s_from_client"))


def test_audit_never_receives_string_session_id():
    """审计里的 session_id 必须是 int 或 None —— 曾经把外部字符串塞进去过"""
    got = []
    list(pipeline.answer_stream("你好", USER, use_cache=False, deps=NL2SQL_DEPS,
                                audit_sink=got.append))
    assert got and (got[0]["session_id"] is None or isinstance(got[0]["session_id"], int))
