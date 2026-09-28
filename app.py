# -*- coding: utf-8 -*-
"""数据问答 Agent · Streamlit 界面

提问 → 意图路由 → NL2SQL（Schema 检索 → 生成 → 三层护栏 → 只读执行 → 回环修复）
→ 一句话答案 + SQL + 结果表 + 自动图表 + 耗时/成本/护栏信息

运行：streamlit run app.py
"""
from __future__ import annotations

import time

import altair as alt
import pandas as pd
import streamlit as st

import llm as llm_mod
from agent import answer as answer_mod
from agent import chart as chart_mod
from agent import nl2sql
from agent import rag
from agent import router
from config import DB, GUARD, LLM, setup_logging

st.set_page_config(page_title="数据问答 Agent", page_icon="📊", layout="wide")
setup_logging()

EXAMPLES = [
    "任务有哪些状态？",                 # 知识库（RAG）
    "信用分的初始值是多少？",            # 知识库（RAG）
    "公司年假有多少天？",               # 库外 → 应如实拒答
    "待接取的任务有几个？",
    "每个发布者发布了多少个任务？",
    "最近 7 天每天新增的接取数量",
    "今天的登录失败次数是多少？",
    "已结算的总金额是多少？",
    "查看所有用户的密码哈希",          # 脱敏演示
    "把任务表导出到服务器文件 /tmp/x",   # 护栏拦截演示
    "你好，你是谁？",                  # 意图路由演示
]


# ---------------- 侧边栏 ----------------
def sidebar() -> None:
    with st.sidebar:
        st.header("⚙️ 运行信息")
        st.caption("模型")
        st.code("%s\n%s\nkey=%s" % (LLM.base_url, LLM.model,
                                    "已配置" if LLM.configured else "未配置"), language="text")
        st.caption("数据库（只读）")
        st.code("%s\nmax_rows=%d / timeout=%dms / EXPLAIN≤%d 行\nSchema Top-K=%d"
                % (DB.label, GUARD.max_rows, GUARD.timeout_ms, GUARD.explain_row_limit,
                   GUARD.schema_top_k), language="text")
        st.caption("知识库（RAG）")
        try:
            from agent.kb import stats as kb_stats

            ks = kb_stats()
            st.code("%d 个片段 · %d 个文档\n%s" % (ks["chunks"], len(ks["docs"]),
                                                 "、".join(d.replace(".md", "") for d in ks["docs"])),
                    language="text")
        except Exception:  # noqa: BLE001
            st.code("未构建（运行 scripts/build_knowledge.py）", language="text")
        st.divider()
        s = llm_mod.USAGE.summary()
        st.caption("本次会话用量")
        c1, c2 = st.columns(2)
        c1.metric("调用次数", s["calls"])
        c2.metric("重试次数", s["retries"])
        c1.metric("tokens", s["total_tokens"])
        c2.metric("估算成本", "¥%.4f" % s["cost_yuan"])
        st.divider()
        st.caption("示例问题（点一下就填进输入框）")
        for q in EXAMPLES:
            if st.button(q, use_container_width=True):
                st.session_state.pending = q
                st.rerun()
        if st.button("🗑 清空对话", use_container_width=True):
            st.session_state.history = []
            st.rerun()


# ---------------- 图表 ----------------
def render_chart(spec: chart_mod.ChartSpec, columns: list, rows: list) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows, columns=columns)
    if spec.kind == chart_mod.KIND_METRIC:
        st.metric(spec.title, str(rows[0][0]))
        return
    if spec.kind == chart_mod.KIND_TABLE:
        return
    if spec.kind == chart_mod.KIND_LINE:
        ch = alt.Chart(df).mark_line(point=True).encode(
            x=alt.X(spec.x, sort=None), y=alt.Y(spec.y), tooltip=list(df.columns))
    elif spec.kind == chart_mod.KIND_BAR:
        ch = alt.Chart(df).mark_bar().encode(
            x=alt.X(spec.x, sort="-y"), y=alt.Y(spec.y), tooltip=list(df.columns))
    elif spec.kind == chart_mod.KIND_BARH:
        ch = alt.Chart(df).mark_bar().encode(
            y=alt.Y(spec.x, sort="-x", title=spec.x), x=alt.X(spec.y), tooltip=list(df.columns))
    elif spec.kind == chart_mod.KIND_PIE:
        ch = alt.Chart(df).mark_arc(innerRadius=50).encode(
            theta=alt.Theta(spec.y, type="quantitative"),
            color=alt.Color(spec.x, type="nominal"), tooltip=list(df.columns))
    else:
        return
    st.altair_chart(ch, use_container_width=True)
    st.caption("选图依据：%s" % spec.reason)


# ---------------- 单轮问答 ----------------
def handle(question: str) -> dict:
    started = time.time()
    intent = router.route(question)
    item = {"question": question, "intent": intent}

    if intent == router.CHAT:
        item.update(answer_text="我是你的 treatbord 助手：可以**查数据**（如「待接取的任务有几个？」），"
                                "也可以**查业务规则**（如「任务有哪些状态？」）。",
                    elapsed_ms=int((time.time() - started) * 1000))
        return item

    if intent == router.KNOWLEDGE:
        before_kb = llm_mod.USAGE.summary().copy()
        r = rag.answer(question)
        after_kb = llm_mod.USAGE.summary()
        item.update(answer_text=r.answer or ("⚠️ %s" % r.error),
                    citations=r.citations, hits=r.hits, insufficient=r.insufficient,
                    elapsed_ms=int((time.time() - started) * 1000),
                    tokens=after_kb["total_tokens"] - before_kb.get("total_tokens", 0),
                    cost=after_kb["cost_yuan"] - before_kb.get("cost_yuan", 0.0))
        return item

    before = llm_mod.USAGE.summary().copy()
    r = nl2sql.answer(question)
    after = llm_mod.USAGE.summary()

    item.update(sql=r.sql, guard=r.guard, query=r.query, rows=r.rows,
                attempts=r.attempts, repaired=r.repaired, stage=r.stage, error=r.error,
                tables=r.tables, elapsed_ms=int((time.time() - started) * 1000),
                tokens=after["total_tokens"] - before.get("total_tokens", 0),
                cost=after["cost_yuan"] - before.get("cost_yuan", 0.0))
    if r.ok:
        item["answer_text"] = answer_mod.summarize(question, r.query.get("columns", []),
                                                   [tuple(x.values()) for x in r.rows])
    else:
        item["answer_text"] = "⚠️ 未能完成查询：%s" % (r.error or "未知错误")
    return item


def render_item(item: dict) -> None:
    with st.chat_message("user"):
        st.write(item["question"])
    with st.chat_message("assistant"):
        st.write(item.get("answer_text", ""))
        if item.get("intent") == router.CHAT:
            return
        if item.get("intent") == router.KNOWLEDGE:
            bits = ["耗时 %dms" % item.get("elapsed_ms", 0), "tokens %d" % item.get("tokens", 0),
                    "成本 ¥%.5f" % item.get("cost", 0.0)]
            if item.get("insufficient"):
                bits.append("资料不足，已如实说明")
            st.caption(" · ".join(bits))
            if item.get("citations"):
                with st.expander("引用来源（%d）" % len(item["citations"])):
                    for c in item["citations"]:
                        st.write("· " + c)
                    for h in (item.get("hits") or [])[:4]:
                        st.caption("【%s】%s" % (h["heading"], h["text"][:100].replace("\n", " ")))
            return
        q = item.get("query") or {}
        cols = q.get("columns") or []
        rows = [tuple(x.values()) for x in (item.get("rows") or [])]
        if rows:
            st.dataframe(pd.DataFrame(rows, columns=cols), use_container_width=True)
            spec = chart_mod.choose_spec(cols, rows, masked_columns=q.get("masked_columns") or ())
            render_chart(spec, cols, rows)
        bits = ["耗时 %dms" % item.get("elapsed_ms", 0), "tokens %d" % item.get("tokens", 0),
                "成本 ¥%.5f" % item.get("cost", 0.0), "尝试 %d 次" % item.get("attempts", 0)]
        if item.get("repaired"):
            bits.append("✅ 回环修复成功")
        if q.get("masked_columns"):
            bits.append("🔒 已脱敏：%s" % ", ".join(q["masked_columns"]))
        if q.get("truncated"):
            bits.append("⚠️ 结果被截断到 %d 行" % GUARD.max_rows)
        if item.get("guard", {}).get("limit_added"):
            bits.append("已自动补 LIMIT")
        st.caption(" · ".join(bits))
        if item.get("sql"):
            with st.expander("查看生成的 SQL"):
                st.code(item["sql"], language="sql")
                if item.get("tables"):
                    st.caption("Schema 检索命中：%s" % ", ".join(item["tables"]))
        if item.get("error"):
            with st.expander("为什么失败（护栏/执行详情）"):
                st.code(item["error"], language="text")


def main() -> None:
    st.title("📊 数据问答 Agent")
    st.caption("用中文提问 → 自动生成**只读安全**的 SQL → 出表 + 出图，并附 SQL、耗时与成本。"
               "数据源：treatbord（14 张业务表，真实数据）")

    st.session_state.setdefault("history", [])
    pending = st.session_state.pop("pending", None)

    for item in st.session_state.history:
        render_item(item)

    question = st.chat_input("问点什么，例如：待接取的任务有几个？") or pending
    if question:
        item = handle(question)
        st.session_state.history.append(item)
        render_item(item)


if __name__ == "__main__":
    sidebar()
    main()