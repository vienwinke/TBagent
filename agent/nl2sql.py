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

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger
from config import setup_logging

import llm as llm_mod
from agent import cache as cache_mod
from agent import executor as ex
from agent import policy
from agent import prompts_user
from agent import scope as scope_mod
from agent.policy import ROLE_ADMIN, Principal
from agent.schema_index import SchemaIndex, load_schema

LlmFn = Callable[[list[dict[str, str]]], dict[str, Any]]

_local_warned = False


def _local_principal() -> Principal:
    """单机（Streamlit / CLI / 评测）没有身份时使用的合成身份

    ⚠️ 按「单机管理员」处理 = **不做行级隔离**。为防止服务化时静默漏传身份：
      1) Nl2SqlResult.isolated 会标记为 False（每个结果都可见）；
      2) 进程内首次使用时打一条 WARNING。
    服务层（pipeline）应把 principal 作为必填参数，从根上避免漏传。
    用户 id 可用 LOCAL_USER_ID 覆盖，仅用于审计归属。
    """
    global _local_warned
    if not _local_warned:
        logger.warning("[nl2sql] 未传 principal：按单机管理员处理，不做行级隔离。"
                       "服务化调用必须显式传入身份（见 docs/treatbord嵌入-接口契约.md）")
        _local_warned = True
    return Principal(user_id=int(os.getenv("LOCAL_USER_ID", "1") or 1), role=ROLE_ADMIN)


def _scoped_index(principal: Principal) -> SchemaIndex:
    """只让角色可见表进入 Schema 检索与提示词

    否则普通用户的 prompt 里会出现 audit_log / login_log / app_config 的列描述
    （实测过：一次普通提问命中了 4 张对 USER 不可见的表）——
    既是 token 浪费，也是信息暴露。
    """
    visible = policy.visible_tables(principal)
    tables = [t for t in load_schema()["tables"] if t["name"].lower() in visible]
    return SchemaIndex(tables=tables)


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
    isolated: bool = False                               # 是否做了行级隔离（USER 视角为 True）
    scope: str | None = None                             # 语义层判定的数据范围（SELF/MARKET/…）
    scope_reason: str = ""                               # 判定依据（命中规则 / 模型判定 / 默认）
    deny_reason: str | None = None                       # 被拒绝时的原因（DENY_*）
    raw_sqls: list[str] = field(default_factory=list)   # 每次尝试模型给出的原始 SQL（未过护栏）
    error: str | None = None
    stage: str = "init"          # init|generate|guard|execute|done|failed
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.stage == "done"

    @property
    def denied(self) -> bool:
        """是否被拒答（语义层越权 / 策略越权）。拒答不是错误：它是正常业务结果。"""
        return self.stage == "denied" and bool(self.deny_reason)

    def deny_message(self) -> str:
        """拒答话术：确定性模板，不调模型（前端直接渲染）"""
        return prompts_user.deny_text(self.deny_reason) if self.deny_reason else ""

    def summary(self) -> dict[str, Any]:
        return {"question": self.question, "sql": self.sql, "reason": self.reason,
                "tables": self.tables, "guard": self.guard, "query": self.query,
                "row_count": self.query.get("row_count"), "attempts": self.attempts,
                "repaired": self.repaired, "cache_hit": self.cache_hit,
                "isolated": self.isolated, "scope": self.scope,
                "scope_reason": self.scope_reason, "denied": self.denied,
                "deny_reason": self.deny_reason, "deny_message": self.deny_message(),
                "error": self.error, "stage": self.stage,
                "raw_sqls": self.raw_sqls,
                "usage": self.usage}


def _default_llm_fn(messages: list[dict[str, str]]) -> dict[str, Any]:
    return llm_mod.chat_json(messages, tag="nl2sql")


def local_principal() -> Principal:
    """单机（Streamlit / CLI / 评测）的**显式**身份入口。

    与 `_local_principal` 是同一实现，公开出来是为了让入口层显式声明
    "这里是单机管理员"，而不是靠不传参数隐式落到默认值 ——
    嵌入服务化时这一行必须换成由 JWT 构造的 Principal（见接口契约 §2.2）。
    """
    return _local_principal()


def default_scope_llm() -> LlmFn | None:
    """按配置给出语义层兜底用的模型函数；未开启则返回 None。

    默认关闭（SCOPE_LLM_FALLBACK=false）：范围判定必须可复现，
    规则层已覆盖明确的越权问法；兜底只在规则完全没命中时介入，
    开了会提高召回但引入采样不确定性 —— 由部署方按合规强度决定。
    """
    from config import SCOPE_LLM_FALLBACK

    return _default_llm_fn if SCOPE_LLM_FALLBACK else None


def _extract(raw: Any) -> tuple[str, str, str]:
    """从模型输出里取出 (sql, reason, refuse_reason)；结构不对就报错（触发回环）

    提示词包要求模型在"这个问题在本角色范围内没法答"时返回
    {"refuse": true, "refuse_reason": "…"} —— 把它当作**模型主动拒答**，
    而不是当成缺字段的生成失败去回环（回环只会再换来一次拒答）。
    """
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是 JSON 对象")
    if raw.get("refuse"):
        return "", "", str(raw.get("refuse_reason") or "模型判定超出当前角色范围")
    sql = str(raw.get("sql", "")).strip()
    if not sql:
        raise ValueError("模型输出缺少 sql 字段")
    return sql, str(raw.get("reason", "")).strip(), ""


def _error_kind(exc: Exception) -> str:
    """把执行期异常映射成提示词包的定向修复类型（REPAIR_KINDS）

    定向回环比笼统回灌有效：too_expensive 要收窄范围，sql_error 要核对列名，
    二者给模型的提示完全不同（见 prompts_user.REPAIR_HINTS）。
    """
    if isinstance(exc, ex.SqlCostError):
        return "too_expensive"
    text = str(exc).lower()
    if "timeout" in text or "超时" in text:
        return "timeout"
    return "sql_error"


def answer(
    question: str,
    *,
    principal: Principal | None = None,
    llm_fn: LlmFn | None = None,
    scope_llm_fn: LlmFn | None = None,
    history: str = "",
    max_repair: int = 1,
    execute: bool = True,
    top_k: int | None = None,
    use_cache: bool = True,
) -> Nl2SqlResult:
    """把自然语言问题回答成「一条安全 SQL + 执行结果」

    成本优化：命中「问题→SQL」缓存时**跳过生成**（0 次 LLM 调用），
    但仍然重新过护栏 + 重新执行 —— 数据不陈旧，省的只是最贵的那次生成。

    语义层（scope_llm_fn）：范围判定默认**只跑确定性规则**；只有调用方显式传入
    scope_llm_fn 时才在"规则一个都没命中"的情况下用模型兜底。这样设计的原因：
    判定结果必须可复现 —— 同一个平台级问题曾两次运行给出不同结果（一次拒答、一次错答）。
    服务层要开兜底，就显式传 `scope_llm_fn=nl2sql._default_llm_fn`。
    """
    call_llm = llm_fn or _default_llm_fn
    usage_before = llm_mod.USAGE.summary()
    res = Nl2SqlResult(question=question)
    if principal is None:
        principal = _local_principal()
    res.isolated = not principal.is_privileged
    # 缓存隔离维度：角色 + 策略版本（同一个 principal 贯穿本次请求的所有环节）
    cache_scope = policy.cache_scope(principal)

    def _finish(r: Nl2SqlResult) -> Nl2SqlResult:
        """统一收尾：无论从哪条路径返回，用量与 repaired 都要结算（早期 return 曾漏掉过）"""
        after = llm_mod.USAGE.summary()
        r.usage = {k: after[k] - usage_before.get(k, 0)
                   for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens", "retries")}
        r.repaired = r.attempts > 1 and r.ok
        return r

    # 0) ★ 语义层范围判定：必须在缓存之前 —— 被判定越权的问题既不查缓存也不生成。
    #    否则"以前答过并被缓存"会绕过后加的规则。
    decision = scope_mod.judge(question, principal, history=history, llm_fn=scope_llm_fn)
    res.scope, res.scope_reason = decision.scope, decision.reason
    if not decision.allowed:
        res.stage, res.deny_reason = "denied", decision.deny_reason
        res.error = "语义越权[%s]: %s" % (decision.deny_reason, decision.reason)
        logger.warning("[nl2sql] 语义层拒答 {}：{}（{}）", decision.deny_reason,
                       decision.reason, decision.source)
        return _finish(res)

    # 1) 缓存命中：复用上次的 SQL，重新执行
    if use_cache and execute:
        hit = cache_mod.cache().get(question, top_k, scope=cache_scope)
        if hit:
            try:
                # ★ 缓存里存的是**重写前**的 SQL：命中后仍然重新过策略层（唯一出口）
                rw_cached = policy.rewrite(hit["sql"], principal)
                qr_cached = policy.execute(rw_cached, check_cost=False)
                res.sql, res.guard = rw_cached.sql, rw_cached.guard
                res.query, res.rows = qr_cached.summary(), qr_cached.as_dicts()
                res.reason = "缓存命中：复用上次生成的 SQL（数据为本次重新执行）"
                res.tables = hit.get("tables") or []
                res.cache_hit, res.stage, res.attempts = True, "done", 0
                return _finish(res)
            except Exception as exc:  # noqa: BLE001  缓存里的 SQL 已不可用 → 静默走正常生成
                logger.debug("[nl2sql] 缓存 SQL 不可用，改为重新生成：{}", str(exc)[:80])

    # 2) Schema 检索（★ 只在角色可见表内检索与注入）
    idx = _scoped_index(principal)
    hits = idx.search(question, top_k)
    res.tables = [h.table for h in hits]
    schema_text = idx.describe(res.tables)
    logger.debug("[nl2sql] 角色={} 检索到表: {}", principal.role, res.tables)

    error: str | None = None
    prev_sql: str | None = None
    kind: str | None = None
    for attempt in range(max_repair + 1):
        res.attempts = attempt + 1
        # 3) 生成（嵌入版提示词包：带身份策略前置 + {{ME}} 占位符 + 定向回环）
        res.stage = "generate"
        try:
            raw = call_llm(prompts_user.nl2sql_messages(
                schema_text, question, principal,
                error=error, prev_sql=prev_sql, kind=kind))
            sql, reason, refuse_reason = _extract(raw)
        except Exception as exc:  # noqa: BLE001
            error, res.stage = "生成阶段失败: %s" % str(exc)[:200], "generate"
            kind = "sql_error"
            logger.warning("[nl2sql] 第 {} 次生成失败: {}", res.attempts, error)
            continue

        if refuse_reason:
            # 模型主动拒答：它已经按提示词判断"本角色答不了"，回环只会再换来一次拒答
            res.stage, res.deny_reason = "denied", scope_mod.DENY_MODEL_REFUSE
            res.error = "模型拒答: %s" % refuse_reason
            logger.warning("[nl2sql] 模型主动拒答（不回环）: {}", refuse_reason[:120])
            break
        res.raw_sqls.append(sql)

        # 3) ★ 唯一出口：静态护栏 + 行级隔离重写（policy.rewrite 内部先跑 sql_guard）
        res.stage = "guard"
        try:
            rw = policy.rewrite(sql, principal)
        except policy.PolicyDenied as exc:
            res.deny_reason = exc.reason
            kind = policy.POLICY_TO_KIND.get(exc.reason)
            if kind is None:
                # 语义越权 / 重写层异常：重试无意义 → 直接拒答（不回环、不查库）
                res.stage, res.error = "denied", "策略拒绝[%s]: %s" % (exc.reason, exc)
                logger.warning("[nl2sql] 被策略直接拒绝（不回环）: {}", exc.reason)
                break
            error, prev_sql = "策略拦截[%s]: %s" % (exc.reason, exc), sql
            logger.warning("[nl2sql] 第 {} 次被策略拦截（{}）: {}", res.attempts, kind, exc)
            continue

        res.sql, res.reason, res.guard = rw.sql, reason, rw.guard
        if not execute:
            res.stage = "done"
            break

        # 4) 执行（护栏②③在 executor 内：只读会话 + 超时 + EXPLAIN 限额）
        res.stage = "execute"
        try:
            qr = policy.execute(rw, check_cost=False)
        except ex.SqlError as exc:
            error, prev_sql = "执行失败: %s" % exc, rw.sql
            kind = _error_kind(exc)
            logger.warning("[nl2sql] 第 {} 次执行失败（{}）: {}", res.attempts, kind, exc)
            continue

        res.query, res.rows = qr.summary(), qr.as_dicts()
        if use_cache:
            # ★ 只缓存**重写前**的 SQL（带身份的重写结果绝不能复用给他人）
            cache_mod.cache().put(question, sql, tables=res.tables, top_k=top_k, scope=cache_scope)
        if qr.row_count == 0 and attempt < max_repair:
            error = "查询返回 0 行：条件或枚举值可能不对（例如状态值、时间范围）"
            prev_sql, kind = rw.sql, "empty_result"
            logger.info("[nl2sql] 第 {} 次返回 0 行，触发回环修复", res.attempts)
            continue
        res.stage = "done"
        break

    if res.stage == "denied":
        pass                          # 拒答（语义层 / 策略层 / 模型主动）：保留 stage/error/deny_reason
    elif res.stage != "done":
        res.stage, res.error = "failed", error
        if prev_sql:
            res.sql = prev_sql

    return _finish(res)


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
