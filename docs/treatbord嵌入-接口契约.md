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
| 超时预算 | 端到端 **8s** 硬超时 · 单次 LLM **12s** · SQL **3s**（`SQL_TIMEOUT_MS`） |
| 幂等 | `client_msg_id` 进 Redis `SETNX`（TTL 10min）；重发返回同一结果，不重复计费 |
| 并发 | 同 `session_id` 串行；全局并发上限保护数据库 |
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

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/v1/ai/chat` | `{session_id, question, client_msg_id}` | `text/event-stream`（§2.3） |
| GET | `/v1/ai/sessions` | `?limit=20` | `[{id, title, updated_at}]` |
| GET | `/v1/ai/sessions/{id}/messages` | — | `[{role, content, payload, created_at}]` |
| DELETE | `/v1/ai/sessions/{id}` | — | `204` |
| POST | `/v1/ai/feedback` | `{message_id, rating(1/-1), comment?}` | `204` |
| GET | `/healthz` | — | `{status:"ok"}`（进程存活） |
| GET | `/readyz` | — | `{status, llm_configured, db_readonly, policy_version}` |
| GET | `/metrics` | — | Prometheus 文本 |

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
| 其他 | **不接受任何客户端自带的 `user_id` 字段或 `X-User-Id` 头**；边车启动时断言请求 schema 里不存在该字段 |

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

# ── 数据分支（agent/nl2sql.py）—— ★ 已接线：生成后经 policy.rewrite → policy.execute
nl2sql.answer(question, *, principal: Principal | None = None, llm_fn=None,
              max_repair=1, execute=True, top_k=None, use_cache=True) -> Nl2SqlResult

# ── 既有模块
router.route(question, *, classify_fn=None) -> "chat" | "knowledge" | "data"
rag.answer(question, *, top_k=None, llm_fn=None, kb=None) -> RagResult
cache.SqlCache.get(question, top_k=None, *, scope="") / .put(question, sql, *, tables=None, top_k=None, scope="")
executor.execute_readonly(sql, *, max_rows=None, check_cost=True, row_limit=None) -> QueryResult
llm.chat(...) / llm.chat_json(...)
```

### 3.2 待建（⬜ 定稿后即为稳定接口）

```python
# agent/pipeline.py —— 编排层：Streamlit 与 FastAPI 共用同一条链路
answer_stream(question: str, principal: Principal, *,
              session_id: str | None = None,
              trace_id: str | None = None) -> Iterator[tuple[str, dict]]
#   yield ("meta", {...}); yield ("scope", {...}); … yield ("done", {...})
#   内部顺序：会话装载 → 指代消解 → 范围判定 → 路由 → 分支 → 事件
```

## 4. L4 · 数据契约

| 契约 | 内容 |
|---|---|
| `PolicyDenied.reason` | `DENY_GUARD` / `DENY_SENSITIVE` / `DENY_PLATFORM` / `DENY_TABLE` / `DENY_CTE` / `DENY_INTERNAL` |
| → 回环 `kind` | `guard_rejected` / `column_denied` / `cte_name_conflict` / `sql_error` / `empty_result` / `too_expensive` / `timeout`；`DENY_PLATFORM` 与 `DENY_INTERNAL` 映射为 `None`（不回环，直接拒答） |
| 缓存不变式 | **只存重写前 SQL**；命中后必须重新 `rewrite()` + 重新执行；键含 `role + POLICY_VERSION`，**不含 user_id**（同角色共享是安全的） |
| `{{ME}}` 占位符 | 模型可写 `{{ME}}`；重写层在解析前替换为整数 user_id（来自 JWT，无注入面） |
| 审计行 | `trace_id, user_id, session_id, question, route, scope, detected_tables, generated_sql, rewritten_sql, policy_version, verdict, deny_reason, row_count, truncated, masked_columns, latency_ms, prompt_tokens, completion_tokens, cost_yuan, cache_hit, repaired, model, created_at` |

## 5. 验收门禁

```bash
# 越权红线（离线：不连库、不需要 API Key），退出码非 0 即阻断
python -m eval.security_suite          # 分类表 + 拦截率/泄漏条数
python -m eval.security_suite --json   # 给 CI 消费

# 单元/集成回归
python -m pytest -q                    # 含 policy 42 条 + 安全套件门禁
```

## 6. 已知待定项（不影响 L2 契约）

| # | 待定 | 影响 |
|---|---|---|
| A | ~~单机 `principal=None` 的语义~~ **已定**：按「单机管理员」处理（不做行级隔离），但 `Nl2SqlResult.isolated=False` + 首次使用时告警；服务层必须显式传 principal | 已实现 |
| B | `executor.execute_readonly` 是否加硬约束（只接受 `RewrittenSql`） | 决定 `policy.execute` 是否成为**唯一**入口 |
| C | `main`/`内嵌` 历史里的旧密钥处置（轮换 / 擦除历史 / 忽略） | 安全收尾 |
