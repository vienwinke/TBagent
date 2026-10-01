# treatbord 内嵌 · 接口契约（冻结版 v1）

> 配套：[技术栈重设计](treatbord嵌入-技术栈重设计.md) · [提示词包](treatbord嵌入-提示词包.md) · [Java 侧接入要点](treatbord嵌入-Java侧接入要点.md)
>
> 本文是 **Java 侧与边车可以并行开发的依据**：标 ✅ 的接口已经实现并有测试兜底，改动需评估兼容性；标 ⬜ 的接口一旦定稿同样不再随实现变动。
>
> ⚠️ 已知待定项（不影响 L2 的 HTTP 契约，只影响边车内部调用）：单机无身份时的语义、`executor` 是否加硬约束 —— 见文末。

## 0. 全局约定

| 项 | 约定 |
|---|---|
| 传输 | HTTPS（treatbord → 边车走内网）；小程序 → treatbord 走已备案域名 |
| 鉴权 | `Authorization: Bearer <内部JWT>`，HS256（升级路径 RS256） |
| 追踪 | 所有请求带 `X-Trace-Id`，边车原样写进审计表 |
| 超时预算 | 端到端 **8s** 硬超时（`SIDECAR_TIMEOUT_MS`）· 单次 LLM **12s** · SQL **3s**（`SQL_TIMEOUT_MS`）<br>✅ 已实现：超预算给 **error + done.timeout** 的诚实降级（不是 500）；单次模型调用也按预算设上限（实测有 25s 卡顿，不设上限会穿透） |
| 追踪 | `X-Trace-Id` 由 treatbord 生成，边车**原样沿用**（不自己另生成） ✅ 已实现 |
| 幂等 | `client_msg_id` 幂等（TTL 10min）；重发返回**逐字节相同**的结果，不重复计费<br>✅ 已实现（**进程内**）；多副本必须换 Redis —— 否则配额翻倍、幂等失效 |
| 并发 | 同 `session_id` 串行 ✅ · 全局并发上限 ✅（`SIDECAR_MAX_CONCURRENCY`，超限 **429 QUOTA_EXCEEDED**，不排队） |
| 版本 | `policy_version=pol-2026.11-02` · `prompt_version=p2026.11-01`，两者随响应返回 |

## 1. L1 · 小程序 ↔ treatbord

| 项 | 约定 |
|---|---|
| 上行 | `{session_id, question, client_msg_id}` —— **不允许出现 `user_id`** |
| 下行 | 流式帧（见 §2 事件），前端只需处理 `delta` / `table` / `chart` / `citations` / `guard` / `done` / `error`，其余可忽略 |
| 流式通道 | 首选 `wx.connectSocket`（WSS，可中断/重连）；备选 `wx.request({enableChunked:true})` + `onChunkReceived` 自行按 `\n\n` 切分 SSE 分片（⚠️ 小程序不支持标准 `EventSource`） |
| 身份 | openid → user_id / role 全部在 Java 侧完成 |

## 2. L2 · treatbord ↔ AI 边车（HTTP/SSE）

### 2.1 端点

状态（2026-10-01）：✅ 已实现并测试 · ⬜ 待做（会话/审计属 P2）

**鉴权已实现**（HS256 内部 JWT，标准库实现，见 `sidecar/auth.py`）。启用方式：
配置 `SIDECAR_JWT_SECRET`（与 treatbord 侧同一密钥）后，`/v1/ai/chat` 只认
`Authorization: Bearer <内部JWT>`；**未配置密钥时仍 fail-closed 返回 503**，
本地联调可临时用 `SIDECAR_DEV_PRINCIPAL=7:USER`（配了密钥就只认 JWT，开发身份自动失效）。

| 方法 | 路径 | 请求 | 响应 | 状态 |
|---|---|---|---|---|
| POST | `/v1/ai/chat` | `{session_id, question, client_msg_id}` | `text/event-stream`（§2.3） | ✅ |
| GET | `/v1/ai/sessions` | `?limit=20` | `[{id, title, updated_at}]` | ⬜ |
| GET | `/v1/ai/sessions/{id}/messages` | — | `[{role, content, payload, created_at}]` | ⬜ |
| DELETE | `/v1/ai/sessions/{id}` | — | `204` | ⬜ |
| POST | `/v1/ai/feedback` | `{message_id, rating(1/-1), comment?}` | `204` | ⬜ |
| GET | `/healthz` | — | `{status:"ok"}`（进程存活） | ✅ |
| GET | `/readyz` | — | `{status, llm_configured, db_readonly, policy_version, auth_configured}` | ✅ |
| GET | `/metrics` | — | Prometheus 文本（请求数 / 拒绝数 / tokens / 成本） | ✅ |

### 2.2 JWT claims

```json
{ "sub": "7", "role": "USER", "jti": "…", "iat": 1764400000,
  "exp": 1764400300, "aud": "ai-sidecar" }
```

| 约束 | 说明 |
|---|---|
| `sub` | user_id，**正整数**；边车侧 `Principal` 对字符串/0/负数直接 `ValueError` |
| `role` | `USER` / `OPERATOR` / `ADMIN` 三档 |
| `exp` | ≤ 5 分钟；管理员降权后旧 token 最长 5 分钟内仍有效 → 要即时生效就把 `jti` 放 Redis 黑名单 |
| 其他 | **不接受任何客户端自带的 `user_id` 字段或 `X-User-Id` 头**；边车启动时断言请求 schema 里不存在该字段（`_assert_no_identity_fields()`） |
| 实现状态 | ✅ 已实现（`sidecar/auth.py`）：**算法锁定只认 HS256**（拒绝 `alg=none` 与 RS256→HS256 混淆）、签名常量时间比较、`sub` 必须正整数、`role` 限三档、`aud` 必须含 `ai-sidecar`、`exp` 未过期且**签发时长 ≤ 5 分钟**、`iat` 不在未来（5s 时钟容差）；失败按 §2.4 返回 401 `UNAUTHENTICATED` |
| 已知缺口 | `jti` 黑名单（即时降权）**未做** —— 降权后旧 token 最长仍有效 5 分钟（P2 随 Redis 一起做） |

### 2.3 SSE 事件

`event: <name>` + `data: <json>`；顺序即下表顺序（`delta` 可多次）。

| event | data 字段 | 何时发 | 前端建议 |
|---|---|---|---|
| `meta` | `trace_id, prompt_version, policy_version, model` | 首帧 | 立即显示"正在分析…" |
| `scope` | `scope, allowed, reason` | 范围判定后 | `allowed=false` 时后续只剩 `delta`+`done` |
| `route` | `route`（`chat`/`knowledge`/`data`） | 路由后 | 可忽略 |
| `sql` | `sql, tables` | 数据分支生成后 | **仅 ADMIN 下发**；USER 只收 `has_sql:true` |
| `table` | `columns, rows, row_count, truncated, masked_columns` | 执行成功 | 表格卡片 |
| `chart` | `kind, x, y, title, reason` | 选图后 | `metric/line/bar/barh/pie/table` |
| `delta` | `text` | 生成答案时 | 打字机增量拼接 |
| `citations` | `[{title, snippet}]` | 知识分支 | 折叠展示，可核查 |
| `guard` | `action, note` | 触发护栏时 | 提示条：`masked`/`limit_added`/`truncated`/`denied` |
| `done` | `elapsed_ms, tokens, cost_yuan, cache_hit, repaired, attempts` | 收尾 | 只展示 elapsed_ms |
| `error` | `code, message, retryable` | 失败 | 见 §2.4 |

示例（一次完整的用户版问答）：

```
event: meta
data: {"trace_id":"7f3a…","prompt_version":"p2026.11-01","policy_version":"pol-2026.11-02","model":"…"}

event: scope
data: {"scope":"SELF","allowed":true,"reason":"只涉及本人数据"}

event: route
data: {"route":"data"}

event: sql
data: {"has_sql":true}

event: table
data: {"columns":["接取数"],"rows":[[12]],"row_count":1,"truncated":false,"masked_columns":[]}

event: chart
data: {"kind":"metric","x":null,"y":null,"title":"接取数","reason":"单行单列 → 大数字卡片"}

event: delta
data: {"text":"你上周共接取 12 个任务。"}

event: guard
data: {"action":"limit_added","note":"原 SQL 无 LIMIT，已自动追加 LIMIT 200"}

event: done
data: {"elapsed_ms":1840,"tokens":2413,"cost_yuan":0.0062,"cache_hit":false,"repaired":false,"attempts":1}
```

### 2.4 错误码

| code | HTTP | 含义 | 可重试 |
|---|---|---|---|
| `UNAUTHENTICATED` | 401 | JWT 缺失/过期/签名不对 | 否（重新登录） |
| `FORBIDDEN_ROLE` | 403 | 角色不足 | 否 |
| `QUOTA_EXCEEDED` | 429 | 日配额或 QPS 超限 | 次日 |
| `POLICY_DENIED` | 200 | 越权（**不是错误**，正常话术回复） | 否 |
| `LLM_UNAVAILABLE` | 200 | 模型侧失败，已给兜底话术 | 是 |
| `SQL_TIMEOUT` | 200 | 执行超时，已给诚实回答 | 是 |
| `INTERNAL` | 500 | 未预期异常（含重写自检未通过） | 是 |

> 越权与执行失败都用 **HTTP 200 + 事件流**返回，不用 4xx —— 前端只需处理一份渲染逻辑，且不泄露"边界在哪"。

## 3. L3 · 边车内部接口（Python）

### 3.1 已实现（✅ 有测试兜底）

```python
# ── 策略层（agent/policy.py）
Principal(user_id: int, role: str = "USER")            # 非法身份直接 ValueError
policy.visible_tables(principal)        -> frozenset[str]
policy.rewrite(sql, principal)          -> RewrittenSql          # 失败抛 PolicyDenied(reason)
policy.execute(rewritten, **kw)         -> QueryResult           # 只收 RewrittenSql
policy.unfiltered_refs(rewritten)       -> list[str]             # fail-closed 自检（空=通过）
policy.cache_scope(principal)           -> str                   # "ROLE|pol-2026.11-02"
policy.cache_scope_key(question, top_k, principal) -> str
policy.POLICY_TO_KIND: dict[str, str | None]
policy.POLICY_VERSION: str

# ── 提示词包（agent/prompts_user.py）—— 全部返回 OpenAI messages
prompts_user.policy_prelude(principal) -> str
prompts_user.scope_guard_messages(question, *, history="")
prompts_user.query_rewrite_messages(question, *, history="")
prompts_user.nl2sql_messages(schema_text, question, principal, *, error=None, prev_sql=None, kind=None)
prompts_user.summarize_messages(question, columns, rows, principal, *, truncated=False)
prompts_user.rag_messages(context, question, principal)
prompts_user.suggest_messages(question, answer, principal)
prompts_user.chart_messages(columns, rows, question)
prompts_user.deny_text(reason, *, suggestions=None) -> str
prompts_user.REPAIR_KINDS / REPAIR_HINTS / DENY_TEMPLATES / PROMPT_VERSION

# ── 语义层范围判定（agent/scope.py）—— ★ 已接线：判定在**生成之前**，拒答不消耗模型调用
scope.judge(question, principal, *, history="", llm_fn=None) -> ScopeDecision
scope.judge_by_rules(question, principal) -> ScopeDecision | None   # 纯规则，确定性
scope.SELF / MARKET / PLATFORM / OTHER_USER / SENSITIVE / NON_BUSINESS
scope.DENY_PLATFORM / DENY_OTHER_USER / DENY_SENSITIVE / DENY_INJECTION / DENY_MODEL_REFUSE
#   规则命中即返回，绝不调模型：同一问题必然同一结论
#     （曾因判定依赖采样，同一平台级问题出现"一次拒答、一次错答"）
#   llm_fn 仅在调用方显式传入、且规则一个都没命中时兜底；
#     未命中且无兜底 → 默认放行（行级隔离仍是硬底线，语义层是它之上的第二道网）
#   角色闸门：SENSITIVE 对所有角色拒答；PLATFORM / OTHER_USER 只对非特权角色拒答

# ── 数据分支（agent/nl2sql.py）—— ★ 已接线：语义判定 → 生成 → policy.rewrite → policy.execute
nl2sql.answer(question, *, principal: Principal | None = None, llm_fn=None,
              scope_llm_fn=None, history="", max_repair=1, execute=True,
              top_k=None, use_cache=True) -> Nl2SqlResult
#   Nl2SqlResult 新增：scope（判定范围）、scope_reason（判定依据）、
#   denied / deny_message()（拒答话术，确定性模板）
#   拒答三类来源：语义层（INJECTION/SENSITIVE/PLATFORM/OTHER_USER）、
#   策略层（PolicyDenied 且不可回环）、模型主动拒答（{"refuse":true} → DENY_MODEL_REFUSE）

# ── 既有模块
router.route(question, *, classify_fn=None) -> "chat" | "knowledge" | "data"
rag.answer(question, *, top_k=None, llm_fn=None, kb=None) -> RagResult
cache.SqlCache.get(question, top_k=None, *, scope="") / .put(question, sql, *, tables=None, top_k=None, scope="")
executor.execute_readonly(rewritten: policy.RewrittenSql, *, max_rows=None,
                             check_cost=True, row_limit=None) -> QueryResult
#   ★ 只接受 RewrittenSql（裸字符串 TypeError）—— 见 §6 决策 B
llm.chat(...) / llm.chat_json(...)
```

### 3.2 已实现（✅ 2026-10-01 起；FastAPI/SSE 包装仍待做）

```python
# agent/pipeline.py —— 编排层：Streamlit 与 FastAPI 共用同一条链路
pipeline.answer_stream(question: str, principal: Principal, *,
                       session_id: str | None = None,
                       trace_id: str | None = None,
                       history: str = "",
                       deps: pipeline.Deps | None = None,
                       execute: bool = True,
                       top_k: int | None = None,
                       use_cache: bool = True) -> Iterator[tuple[str, dict]]
#   yield ("meta", {...}); yield ("scope", {...}); … yield ("done", {...})
#   事件顺序即 §2.3 的表序：meta → scope → route → sql → table → chart → delta
#                            → citations → guard → done / error（delta 可多次）

# 注入点：把"调哪个模型"从编排里拆出去，链路因此可以完全离线断言
pipeline.Deps(nl2sql_llm=None, scope_llm=None, classify_llm=None,
              rag_llm=None, summary_llm=None)
pipeline.CHAT_REPLY        # 闲聊回复文案（Streamlit 与 SSE 共用同一句）
pipeline.error_event(msg)  # 失败 → 契约 §2.4 的错误码

# app.py 侧只是事件流的渲染适配器：handle(question, deps=None) -> item
#   —— 编排逻辑不在 UI 里重复实现（本项目此前踩过"同一逻辑两份实现"的坑）
```

**已落实的契约约束**：拒答（scope.allowed=False）只发 `delta + done`、**零模型调用**；
`sql` 明文仅对运营及以上下发，普通用户只收 `has_sql:true`；失败走 `error` 事件（带 code/retryable）
而不是异常穿透；`meta` 携带 `trace_id / prompt_version / policy_version / model`。

> 仍待做（P0 的另一半）：FastAPI 包装 + SSE 输出 + `/healthz`。届时直接把同一份事件流转成 SSE 帧。

## 4. L4 · 数据契约

| 契约 | 内容 |
|---|---|
| `PolicyDenied.reason` | `DENY_GUARD` / `DENY_SENSITIVE` / `DENY_PLATFORM` / `DENY_TABLE` / `DENY_CTE` / `DENY_INTERNAL` |
| → 回环 `kind` | `guard_rejected` / `column_denied` / `cte_name_conflict` / `sql_error` / `empty_result` / `too_expensive` / `timeout`；`DENY_PLATFORM` 与 `DENY_INTERNAL` 映射为 `None`（不回环，直接拒答） |
| 缓存不变式 | **只存重写前 SQL**；命中后必须重新 `rewrite()` + 重新执行；键含 `role + POLICY_VERSION`，**不含 user_id**（同角色共享是安全的） |
| `{{ME}}` 占位符 | 模型可写 `{{ME}}`；重写层在解析前替换为整数 user_id（来自 JWT，无注入面） |
| 审计落库 | ✅ **已实现**（2026-10-01）：`agent/audit.py` best-effort 写入（失败只记日志，绝不影响问答）；由 `AUDIT_ENABLED` 控制（默认关闭）。SQL 明文经 pipeline 的**服务端专属 `_audit` 事件**进入审计 —— 客户端 SSE 里普通用户仍只有 `has_sql:true` |
| `session_id` | 审计表的 `session_id` 指向 `ai_chat_session.id`（BIGINT）。会话落库（P3）之前**一律写 NULL**，绝不把 L2 的字符串 session_id 硬塞进 BIGINT |
| 建表脚本 | ✅ `sql/ai_tables.sql`（5 张 `ai_*` 表，幂等可重跑）。⚠️ 执行需要**可写连接**：当前边车只有只读账号，建表与落库必须用另开的账号，且审计表**只给 INSERT/SELECT**（只增不改，脚本末尾附授权模板与自检方法） |
| 审计行 | `trace_id, user_id, session_id, question, route, scope, detected_tables, generated_sql, rewritten_sql, policy_version, verdict, deny_reason, row_count, truncated, masked_columns, latency_ms, prompt_tokens, completion_tokens, cost_yuan, cache_hit, repaired, model, created_at` |

## 5. 验收门禁

```bash
# 越权红线（离线：不连库、不需要 API Key），退出码非 0 即阻断
python -m eval.security_suite          # 54 条（含 13 条语义越权）；分类表 + 拦截率/泄漏条数
python -m eval.security_suite --json   # 给 CI 消费

# 单元/集成回归
python -m pytest -q                    # 195 条（含 policy 42 条 + 语义层 + 安全套件门禁）

# 密钥门禁（已挂 .githooks/pre-commit；启用见 docs/开发环境.md §5）
python scripts/prepublish_check.py --secrets-only
```

> 条数为 **2026-10-01** 快照。门禁的判据是"退出码为 0"，不是条数 —— 条数会随用例增长。

## 6. 已知待定项（不影响 L2 契约）

| # | 待定 | 影响 |
|---|---|---|
| A | ~~单机 `principal=None` 的语义~~ **已定**：按「单机管理员」处理（不做行级隔离），但 `Nl2SqlResult.isolated=False` + 首次使用时告警；服务层必须显式传 principal | 已实现 |
| B | ~~`executor.execute_readonly` 是否加硬约束~~ **已定并已实现**（2026-10-01）：只接受 `policy.RewrittenSql`，裸字符串一律 `TypeError`；`policy.execute` 是唯一入口。内部诊断脚本也改走 `policy.rewrite()`（见 `scripts/smoke_sqlite.py`） | 唯一出口从"约定 + 源码守卫"升级为**类型约束**，新增代码路径绕不过去 |
| C | **已查明**（不再是"待定"，是待执行的安全动作）：`26bc0b2` 把一把真实密钥（`user_…` 形态，非 `sk-` 前缀）写进 `.env.example`，随 `origin/内嵌` 推上远端；两侧远端 **tip 已干净**，但**历史可完整取回**。本机 `main` 分支 tip 仍带明文。仓库侧扫描器本可命中该形态（是没人跑，不是规则漏），现已接进 pre-commit。**待你执行**：① 供应商侧确认吊销旧值；② 决定公开历史是否重写 | 安全收尾 |
