# -*- coding: utf-8 -*-
"""NL2SQL 主链路：Schema 检索 → 生成 → 静态校验 → 只读执行 → 失败回环自修复

链路（每一步都可单独观测，便于评估与排障）：
  1. Schema 检索：只把 Top-K 相关表注入提示词（省 token、少选错表）
  2. 生成：LLM 输出 JSON（sql + reason）
  3. 护栏①：sqlglot 静态校验（只读/白名单/禁多语句/强制 LIMIT）
  4. 护栏②③：只读沙箱 + EXPLAIN 成本预估（在 executor 内完成）
  5. 回环自修复：静态校验失败 / 执行报错 / 返回 0 行 → 把错误与上次 SQL 回灌，自动重试 1 次

llm_fn 可注入（默认走 llm.chat_json），因此**不依赖 API Key 也能单测整条链路**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger
from config import setup_logging

import llm as llm_mod
from agent import cache as cache_mod
from agent import executor as ex
from agent import prompts
from agent import sql_guard
from agent.schema_index import SchemaIndex

LlmFn = Callable[[list[dict[str, str]]], dict[str, Any]]


@dataclass
class Nl2SqlResult:
    question: str
    sql: str = ""
    reason: str = ""
    tables: list[str] = field(default_factory=list)       # 检索命中的表
    guard: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)   # QueryResult.summary()
    rows: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    repaired: bool = False
    cache_hit: bool = False                              # 是否命中"问题→SQL"缓存（省掉生成调用）
    raw_sqls: list[str] = field(default_factory=list)   # 每次尝试模型给出的原始 SQL（未过护栏）
    error: str | None = None
    stage: str = "init"          # init|generate|guard|execute|done|failed
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.stage == "done"

    def summary(self) -> dict[str, Any]:
        return {"question": self.question, "sql": self.sql, "reason": self.reason,
                "tables": self.tables, "guard": self.guard, "query": self.query,
                "row_count": self.query.get("row_count"), "attempts": self.attempts,
                "repaired": self.repaired, "cache_hit": self.cache_hit,
                "error": self.error, "stage": self.stage,
                "raw_sqls": self.raw_sqls,
                "usage": self.usage}


def _default_llm_fn(messages: list[dict[str, str]]) -> dict[str, Any]:
    return llm_mod.chat_json(messages, tag="nl2sql")


def _extract(raw: Any) -> tuple[str, str]:
    """从模型输出里取出 sql / reason，结构不对就报错（触发回环）"""
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是 JSON 对象")
    sql = str(raw.get("sql", "")).strip()
    if not sql:
        raise ValueError("模型输出缺少 sql 字段")
    return sql, str(raw.get("reason", "")).strip()


def answer(
    question: str,
    *,
    llm_fn: LlmFn | None = None,
    max_repair: int = 1,
    execute: bool = True,
    top_k: int | None = None,
    use_cache: bool = True,
) -> Nl2SqlResult:
    """把自然语言问题回答成「一条安全 SQL + 执行结果」

    成本优化：命中「问题→SQL」缓存时**跳过生成**（0 次 LLM 调用），
    但仍然重新过护栏 + 重新执行 —— 数据不陈旧，省的只是最贵的那次生成。
    """
    call_llm = llm_fn or _default_llm_fn
    usage_before = llm_mod.USAGE.summary()
    res = Nl2SqlResult(question=question)

    # 0) 缓存命中：复用上次的 SQL，重新执行
    if use_cache and execute:
        hit = cache_mod.cache().get(question, top_k)
        if hit:
            try:
                g_cached = sql_guard.validate(hit["sql"])
                qr_cached = ex.execute_readonly(g_cached.sql, check_cost=False)
                res.sql, res.guard = g_cached.sql, g_cached.summary()
                res.query, res.rows = qr_cached.summary(), qr_cached.as_dicts()
                res.reason = "缓存命中：复用上次生成的 SQL（数据为本次重新执行）"
                res.tables = hit.get("tables") or []
                res.cache_hit, res.stage, res.attempts = True, "done", 0
                return res
            except Exception as exc:  # noqa: BLE001  缓存里的 SQL 已不可用 → 静默走正常生成
                logger.debug("[nl2sql] 缓存 SQL 不可用，改为重新生成：{}", str(exc)[:80])

    # 1) Schema 检索
    idx = SchemaIndex()
    hits = idx.search(question, top_k)
    res.tables = [h.table for h in hits]
    schema_text = idx.describe(res.tables)
    logger.debug("[nl2sql] 检索到表: {}", res.tables)

    error: str | None = None
    prev_sql: str | None = None
    for attempt in range(max_repair + 1):
        res.attempts = attempt + 1
        # 2) 生成
        res.stage = "generate"
        try:
            raw = call_llm(prompts.nl2sql_messages(schema_text, question, error=error, prev_sql=prev_sql))
            sql, reason = _extract(raw)
            res.raw_sqls.append(sql)
        except Exception as exc:  # noqa: BLE001
            error, res.stage = "生成阶段失败: %s" % str(exc)[:200], "generate"
            logger.warning("[nl2sql] 第 {} 次生成失败: {}", res.attempts, error)
            continue

        # 3) 护栏① 静态校验
        res.stage = "guard"
        try:
            g = sql_guard.validate(sql)
        except sql_guard.SqlGuardError as exc:
            error, prev_sql = "静态校验未通过: %s" % exc, sql
            logger.warning("[nl2sql] 第 {} 次被护栏拦截: {}", res.attempts, exc)
            continue

        res.sql, res.reason, res.guard = g.sql, reason, g.summary()
        if not execute:
            res.stage = "done"
            break

        # 4) 护栏②③ + 执行
        res.stage = "execute"
        try:
            qr = ex.execute_readonly(g.sql, check_cost=False)
        except ex.SqlError as exc:
            error, prev_sql = "执行失败: %s" % exc, g.sql
            logger.warning("[nl2sql] 第 {} 次执行失败: {}", res.attempts, exc)
            continue

        res.query, res.rows = qr.summary(), qr.as_dicts()
        if use_cache:
            cache_mod.cache().put(question, g.sql, tables=res.tables, top_k=top_k)
        if qr.row_count == 0 and attempt < max_repair:
            error = "查询返回 0 行：条件或枚举值可能不对（例如状态值、时间范围）"
            prev_sql = g.sql
            logger.info("[nl2sql] 第 {} 次返回 0 行，触发回环修复", res.attempts)
            continue
        res.stage = "done"
        break

    if res.stage != "done":
        res.stage, res.error = "failed", error
        if prev_sql:
            res.sql = prev_sql

    res.repaired = res.attempts > 1 and res.ok
    usage_after = llm_mod.USAGE.summary()
    res.usage = {k: usage_after[k] - usage_before.get(k, 0)
                 for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens", "retries")}
    return res


def main() -> None:
    setup_logging()
    import argparse
    import json

    parser = argparse.ArgumentParser(description="数据问答 Agent（NL2SQL）")
    parser.add_argument("question", type=str, help="自然语言问题")
    parser.add_argument("--dry-run", action="store_true", help="只生成并校验 SQL，不执行")
    parser.add_argument("--top-k", type=int, default=None)
    args = parser.parse_args()

    r = answer(args.question, execute=not args.dry_run, top_k=args.top_k)
    print(json.dumps(r.summary(), ensure_ascii=False, indent=2))
    if r.rows:
        print("\n结果预览:")
        for row in r.rows[:5]:
            print("  ", row)


if __name__ == "__main__":
    main()
