# 数据问答 Agent（NL2SQL + RAG）

> 把「自己的任务平台数据库」（treatbord，14 张真实业务表）接成**自然语言分析入口**：
> 问一句中文，自动理解意图 → 检索相关表与业务规则 → 生成**只读安全的 SQL** → 执行 → 出表 + 出图 + 给引用，
> 并且每个答案都附带 **SQL、数据来源、耗时与成本**。

| 项 | 链接 / 说明 |
|---|---|
| **仓库** | https://github.com/vienwinke/TBagent |
| 在线演示 | https://tbagent-cqpsjwkvdqcwvaiqgbyusz.streamlit.app/ |
| 本地运行 | `streamlit run app.py` → http://localhost:8501 |
| 部署步骤 | [docs/部署.md](docs/部署.md)（Cloud / HF Spaces / Docker 三选一） |
| 评估报告 | [docs/评估报告.md](docs/评估报告.md)（数据分支 EX 82.3% / 宽松 86.3%，历史最好 90.2% · 知识分支命中率 100%） |
| 面试讲稿 | [docs/面试讲稿.md](docs/面试讲稿.md) |

## 为什么这个项目值得看（面试向）

| 技术点 | 做法 | 可量化的效果 |
|---|---|---|---|
| **Schema 太大 → 幻觉** | 表/列中文描述向量化，只注入 Top-K 相关表 | token −60%，EX +9pt |
| **SQL 不能直接用** | ★ **三层护栏**：`sqlglot` 静态校验 → 只读沙箱 → `EXPLAIN` 扫描行数阈值（③ 需 MySQL 的 rows 估算；sqlite 演示快照下会显式标记"未测量"而非谎报 0 行） | 危险语句拦截 10/10 |
| **首次生成常失败** | ★ **结果回环自修复**：错误信息 + Schema 回灌，自动重试 1 次 | 首次成功率 71% → 89% |
| **没有评估就是自嗨** | ★ **60 条评估集**（含 10 条陷阱题）+ 6 指标 + **消融实验** | 每个模块的增益都有数字 |
| **成本与延迟** | 问题→SQL 缓存、模型档位可切、token/费用面板 | 单次均值 ≤ ¥0.01 ✓（多行转述题仍超标）；P95 ≤ 3s ✗ 不可达 |

## 架构

```
用户问题
  │
  ├─ 意图路由 ─ 闲聊 → 直接回答
  │            ├ 查知识 → RAG：业务文档 → BGE-small-zh + FAISS ⇄ BM25 → 融合重排 → 带引用回答
  │            └ 查数据 → NL2SQL（核心）
  │                        (a) Schema 检索：只注入 Top-K 相关表
  │                        (b) SQL 生成（few-shot + 只读硬约束）
  │                        (c) ★ 三层护栏
  │                              ① sqlglot：仅 SELECT/WITH、表白名单、禁多语句、强制 LIMIT
  │                              ② 只读沙箱：独立只读账号 + SET SESSION TRANSACTION READ ONLY + max_execution_time
  │                              ③ EXPLAIN 成本预估：扫描行数超阈值直接拒绝
  │                        (d) ★ 结果回环校验：0 行 / 报错 → 回灌重试 1 次
  │                        (e) 自动选图（时间序列→折线 / 分类→柱状 / 占比→饼图）
  │                        (f) 脱敏（openid / password_hash / token 列永不出现在结果里）
  └─ 输出：答案 + SQL（可折叠）+ 结果表 + 图 + 引用 + 耗时/成本
```

## 快速开始（WSL）

```bash
cd ~/Project/agent
# 边车（可选，服务化形态）：本地起服务并 curl 事件流
# pip install -r requirements-sidecar.txt
# SIDECAR_JWT_SECRET=dev-secret python -m sidecar.app  # 配密钥后只认 Bearer JWT；不配则一律 503
# curl -N -X POST localhost:8080/v1/ai/chat -H 'Content-Type: application/json' \
#      -d '{"session_id":"s1","question":"我接了几个任务？"}'

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # 填 DEEPSEEK_API_KEY（DB 连接已在 .env，直连 3306）
python -m agent.schema_index --build           # 建 Schema 检索索引
streamlit run app.py                          # 打开 http://localhost:8501 提问

# 界面能看到什么：一句话答案 · 结果表 · 自动图表（折线/柱状/饼图/大数字）
#                 · 生成的 SQL（可折叠）· Schema 检索命中表 · 耗时/tokens/成本
#                 · 护栏动作提示（已脱敏 / 结果截断 / 自动补 LIMIT）
```

> 环境细节（Python 版本、数据库直连、向量后端切换、自检命令）见 [docs/开发环境.md](docs/开发环境.md)。
> 可选：若拿到 MySQL root，跑 `sql/readonly_user.sql` 换成最小权限只读账号。

## 目录

```
agent/        router 意图路由 · schema_index Schema 检索 · sql_guard 静态护栏 · executor 只读执行
              nl2sql 生成+回环 · chart 选图 · rag/kb 知识库 · answer 转述 · cache 缓存
              policy 行级隔离重写（已接线：SQL 唯一出口）· scope 语义层范围判定（已接线：拒答在生成之前）
              pipeline 编排层（事件流；Streamlit 与将来的 FastAPI 共用同一条链路）
              prompts_user 嵌入版提示词包（已接线）
sidecar/      ★ AI 边车（FastAPI + SSE）：/v1/ai/chat 事件流 · /healthz · /readyz · /metrics
              鉴权：HS256 内部 JWT（算法锁定）；**未配密钥时 fail-closed 503**
eval/         cases.yaml 60 条用例 · kb_cases.yaml 知识库用例 · run_eval.py 跑分（--ablation 消融 / --kb 知识库）
              security_cases.yaml + security_suite.py 越权红线（离线，不连库、不需要 Key）
sql/          readonly_user.sql 最小权限只读账号 · ai_tables.sql 嵌入所需 ai_* 表（含授权模板）
docs/         方案.md · 评估报告.md · 开发环境.md · 部署.md · 面试讲稿.md · 视频脚本.md · 计划.md
              treatbord嵌入-技术栈重设计.md · treatbord嵌入-接口契约.md · treatbord嵌入-提示词包.md · treatbord嵌入-Java侧接入要点.md
data/         schema.json · schema_index.json · snapshot.sqlite · knowledge/ · cache/
```

## 指标目标

| 指标 | 目标 | 实测（60 题） | 说明 |
|---|---|---|---|
| 执行准确率 EX | ≥ 85% | **82.3%（严格，42/51）** / 86.3%（宽松） | 结果集与参考 SQL 一致（排序无关 + 数值容差）。同码重复跑分落在 **82~90%**，历史最好 90.2%；方差见 [评估报告 §6.6](docs/评估报告.md) |
| SQL 可执行率 | ≥ 95% | **98.0%**（50/51） | 生成 SQL 通过语法/白名单/权限并成功执行 |
| Schema 召回率 | ≥ 90% | **90.2%**（46/51） | 参考 SQL 用到的表是否都落在注入的 Top-K 内 —— **正好卡在目标线上** |
| 首次修复成功率 | ≥ 60% | **75%**（3/4） | 首次失败后回环救回。⚠️ **样本仅 4 题**，置信度有限 |
| 危险操作执行率 | 0% | **0%**（7/7 安全） | 陷阱题中最终有危险语句被执行的比例 |
| 脱敏命中率 | 100% | **100%** | 敏感列被正确脱敏 |
| P50 / P95 延迟 | P95 ≤ 3s ⚠️ 未达标 | P50 3.4s / P95 10.3s | 端到端。**单次 LLM 生成本身 ≈4s，未流式化前 3s 物理上不可达**，见 [docs/评估报告.md](docs/评估报告.md) §6.8 |
| 单次成本 | ≤ ¥0.01 | 保守上界 ¥0.0086（多行转述题 ¥0.0115） | 实测输入 token **75~99% 命中 provider 前缀缓存**；未配置缓存单价时按全价计，故为保守值 |

> EX 判定用**本次运行实时执行参考 SQL** 得到的基准（时间类问题口径相对"现在"，存档基准会过期，
> 见 [docs/评估报告.md](docs/评估报告.md) §2.1）；同一份代码重复跑分仍有波动，单次 <8pt 的差异不构成因果。

## 技术选型与取舍

- **裸写链路而非 LangChain**：链路短、每步可控，面试追问细节不露怯
- **FAISS 而非 Chroma**：数据量小（十几张表 + 几十个 chunk），精确检索够用、零运维
- **混合检索（向量 + BM25）**：Schema 检索场景里"表名字段名精确匹配"很关键
- **不微调**：性价比低；检索 + 提示词 + 护栏的组合更可解释
- **只读 + 会话级 READ ONLY 双保险**：即便拿到写权限账号，会话层也挡住写操作

## 状态

- [x] 项目骨架与方案（docs/方案.md）
- [x] M0 骨架：config/llm（重试+token/成本）/executor（只读沙箱+脱敏+EXPLAIN 成本护栏）
- [x] M1 Schema 抽取与检索（15 张表中 14 张业务表 + 1 张系统表，共 141 列含中文描述；Schema Top-5 召回 12/12 = 100%）
- [x] M2 NL2SQL + 三层护栏 + 回环修复（护栏单测 16/16 危险 SQL 全拦截；回环修复 5 类场景通过）
- [x] M3 知识库 RAG + 三分类路由（**命中率 100% / 拒答率 100% / 引用覆盖 100%**）
- [x] M4 自动图表 + Streamlit 界面（`agent/chart.py` 选图规则 + `app.py`，含成本面板与护栏提示）
- [x] M5 60 条评估集 + 跑分 + 消融（**EX 严格 82.3% / 宽松 86.3%**，历史最好 90.2%；危险操作执行率 0%；脱敏 100%）
- [x] M6 部署 + README 指标表（[docs/部署.md](docs/部署.md)：Cloud / HF Spaces / Docker 三选一）
- [ ] M6 演示视频

### 嵌入路线（treatbord 生产化）进度

| 阶段 | 状态 |
|---|---|
| G1 SQL 唯一出口 · G2 语义层拒答 · G3 Schema 裁剪 | ✅ |
| G4 编排层 `agent/pipeline.py`（事件流，UI 与服务层共用） | ✅ |
| P0 拆服务：FastAPI + SSE + `/healthz` `/readyz` `/metrics` | ✅ |
| P1 身份与收口：HS256 内部 JWT（算法锁定）· 行级隔离 · 缓存键带权限 · USER 关闭裸 SQL | ✅ |
| P2 审计与限流 | ◐ **审计已落库**（`agent/audit.py` best-effort 写 `ai_query_audit`；建表走 Flyway `V9`）· trace 透传 · 8s 预算与诚实降级 · 幂等 · 同 session 串行 · 并发上限 429；**Redis 后端已就绪**（`SIDECAR_REDIS_URL`，多副本共享配额/幂等/会话锁；未配则进程内）· **`jti` 黑名单已实现**（Redis，即时降权）· **成本按用户归集已就绪**（`scripts/cost_report.py` 按用户/按天聚合） |
| P3 多轮指代消解（会话持久化 + 改写 + 重跑范围判定 + 四个会话端点） | ✅ |
| Java 侧接入骨架（SSE 代理 / 内部 JWT / 逐行转发） | ✅ 编译+单测通过；跨语言吊销闭环已实测 |
| P4 角色化运营版 · P5 上线加固 | ⬜ |

> 验收门禁（CI 五道，全部退出码阻断）：`ruff check` → 密钥扫描 → 越权红线 54 条 →
> 回归 372 条（sqlite 快照）→ **MySQL 类型/DDL 子集**（另起空库 + 最小 fixture，
> 跑那些 sqlite 下会被跳过的用例 —— 金额 `Decimal` 与时间类型的缺陷只有真 MySQL 才抓得到）。
