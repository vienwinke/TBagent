# -*- coding: utf-8 -*-
"""数据问答 Agent · Streamlit 界面

提问 → 意图路由 → NL2SQL（Schema 检索 → 生成 → 三层护栏 → 只读执行 → 回环修复）
→ 一句话答案 + SQL + 结果表 + 自动图表 + 耗时/成本/护栏信息

运行：streamlit run app.py
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import llm as llm_mod
from agent import chart as chart_mod
from agent import nl2sql
from agent import pipeline
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
        problems = LLM.issues()
        if problems:
            st.error("⚠️ 配置需要修正（否则提问会失败）")
            for pr in problems:
                st.warning(pr)
        else:
            st.success("✅ 配置自检通过")
        with st.expander("配置自检详情", expanded=bool(problems)):
            st.write("key: %s" % ("已配置（%d 字符，纯 ASCII）" % len(LLM.api_key)
                                  if LLM.configured and LLM.api_key.isascii() else "未配置 / 含非 ASCII"))
            st.write("base_url: `%s`" % LLM.base_url)
            st.write("model: `%s`" % LLM.model)
            st.write("db: `%s`" % DB.label)
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


# ---------------- 单轮问答：只是 pipeline 事件流的渲染适配器 ----------------
def handle(question: str, deps: pipeline.Deps | None = None) -> dict:
    """把 (event, data) 事件流翻译成 render_item 需要的 item dict。

    **编排逻辑不在这里** —— 路由、范围判定、分支、护栏提示全在 agent/pipeline.py。
    Streamlit 与将来的 FastAPI 共用同一条链路：这边渲染事件，那边把同一份事件转成 SSE 帧。
    这样才不会出现"两处实现各自漂移"（本项目已经吃过三次同类亏）。
    """
    # 单机演示 = 单机管理员（结果里 isolated=False 可见）；服务化必须换成 JWT 解析出的身份
    principal = nl2sql.local_principal()
    if deps is None:
        deps = pipeline.Deps(scope_llm=nl2sql.default_scope_llm())

    item: dict = {"question": question, "guards": []}
    answer_parts: list[str] = []

    for event, data in pipeline.answer_stream(question, principal, deps=deps):
        if event == "meta":
            item.update(trace_id=data["trace_id"], model=data["model"],
                        policy_version=data["policy_version"])
        elif event == "scope":
            item.update(scope=data["scope"], scope_reason=data["reason"])
        elif event == "route":
            item["intent"] = data["route"]
        elif event == "sql":
            item["sql"] = data.get("sql") or ""
            item["has_sql"] = bool(data.get("sql") or data.get("has_sql"))
            item["tables"] = data.get("tables") or []
        elif event == "table":
            item["query"] = {"columns": data["columns"], "row_count": data["row_count"],
                             "truncated": data["truncated"],
                             "masked_columns": data["masked_columns"]}
            item["rows"] = [tuple(r) for r in data["rows"]]
        elif event == "chart":
            item["chart"] = data
        elif event == "delta":
            answer_parts.append(data["text"])
        elif event == "citations":
            item["citations"] = [c["title"] for c in data]
            item["citation_items"] = data
        elif event == "guard":
            item["guards"].append(data)
        elif event == "error":
            item["error"] = "%s: %s" % (data.get("code"), data.get("message"))
        elif event == "done":
            item.update(elapsed_ms=data["elapsed_ms"], tokens=data["tokens"],
                        cost=data["cost_yuan"], cache_tokens=data.get("cache_tokens", 0),
                        attempts=data.get("attempts", 0), repaired=data.get("repaired", False),
                        cache_hit=data.get("cache_hit", False), denied=data.get("denied", False),
                        deny_reason=data.get("deny_reason"),
                        insufficient=data.get("insufficient", False), stage=data.get("stage"))

    item["answer_text"] = "\n".join(answer_parts) or (
        "⚠️ 未能完成查询：%s" % (item.get("error") or "未知错误"))
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
                    for c in (item.get("citation_items") or []):
                        st.write("· " + c["title"])
                        if c.get("snippet"):
                            st.caption(c["snippet"].replace("\n", " ")[:120])
            return
        if item.get("denied"):
            # 拒答：既没有 SQL 也不该有表格。展示判定依据（可审计），但不泄露内部机制细节。
            st.caption("耗时 %dms · tokens %d · 成本 ¥%.5f · 🛡 已按权限范围拒答"
                       % (item.get("elapsed_ms", 0), item.get("tokens", 0), item.get("cost", 0.0)))
            with st.expander("为什么不答（范围判定依据）"):
                st.code("判定范围：%s\n判定依据：%s" % (item.get("scope") or "-",
                                                      item.get("scope_reason") or "-"), language="text")
            return
        q = item.get("query") or {}
        cols = q.get("columns") or []
        rows = item.get("rows") or []
        if rows:
            st.dataframe(pd.DataFrame(rows, columns=cols), use_container_width=True)
            spec = chart_mod.choose_spec(cols, rows, masked_columns=q.get("masked_columns") or ())
            render_chart(spec, cols, rows)
        bits = ["耗时 %dms" % item.get("elapsed_ms", 0), "tokens %d" % item.get("tokens", 0),
                "成本 ¥%.5f" % item.get("cost", 0.0), "尝试 %d 次" % item.get("attempts", 0)]
        if item.get("repaired"):
            bits.append("✅ 回环修复成功")
        if item.get("cache_tokens"):
            bits.append("前缀缓存命中 %d tokens" % item["cache_tokens"])
        if item.get("denied"):
            bits.append("🛡 已按权限范围拒答")
        # 护栏提示统一来自 pipeline 的 guard 事件（前端不需要理解内部机制）
        for g in (item.get("guards") or []):
            action = g.get("action")
            if action == "limit_added":
                bits.append("已自动补 LIMIT")
            elif action == "masked":
                bits.append("🔒 %s" % g.get("note", "已脱敏"))
            elif action == "truncated":
                bits.append("⚠️ %s" % g.get("note", "结果已截断"))
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