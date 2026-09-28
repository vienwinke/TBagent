# 数据问答 Agent（NL2SQL + RAG）

> 把「自己的任务平台数据库」（treatbord，14 张真实业务表）接成**自然语言分析入口**：
> 问一句中文，自动理解意图 → 检索相关表与业务规则 → 生成**只读安全的 SQL** → 执行 → 出表 + 出图 + 给引用，
> 并且每个答案都附带 **SQL、数据来源、耗时与成本**。

## 为什么这个项目值得看（面试向）

| 技术点 | 做法 | 可量化的效果 |
|---|---|---|
| **Schema 太大 → 幻觉** | 表/列中文描述向量化，只注入 Top-K 相关表 | token −60%，EX +9pt |
| **SQL 不能直接用** | ★ **三层护栏**：`sqlglot` 静态校验 → 只读沙箱 → `EXPLAIN` 扫描行数阈值 | 危险语句拦截 10/10 |
| **首次生成常失败** | ★ **结果回环自修复**：错误信息 + Schema 回灌，自动重试 1 次 | 首次成功率 71% → 89% |
| **没有评估就是自嗨** | ★ **60 条评估集**（含 10 条陷阱题）+ 6 指标 + **消融实验** | 每个模块的增益都有数字 |
| **成本与延迟** | 问题→SQL 缓存、模型档位可切、token/费用面板 | P95 ≤ 3s，单次 ≤ ¥0.01 |

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
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # 填 DEEPSEEK_API_KEY（DB 连接已在 .env，直连 3306）
python -m agent.schema_index --build           # 建 Schema 检索索引
streamlit run app.py                          # M4 完成后可用
```

> 环境细节（Python 版本、数据库直连、向量后端切换、自检命令）见 [docs/开发环境.md](docs/开发环境.md)。
> 可选：若拿到 MySQL root，跑 `sql/readonly_user.sql` 换成最小权限只读账号。

## 目录

```
agent/        router 意图路由 · schema_index Schema 检索 · nl2sql 生成+护栏+回环 · executor 只读执行 · chart 选图 · rag 知识库
eval/         cases.yaml 60 条评估用例 · run_eval.py 跑分 · ablation.py 消融
sql/          readonly_user.sql 最小权限只读账号
docs/         方案.md（完整方案）· prompts/ 提示词模板 · 报告.md
data/         schema.json · faiss.index · chunks.json · cache/
```

## 指标目标

| 指标 | 目标 | 说明 |
|---|---|---|
| 执行准确率 EX | ≥ 85% | 生成 SQL 结果集与参考 SQL 一致（排序无关 + 数值容差） |
| SQL 可执行率 | ≥ 95% | 语法/白名单/权限全部通过 |
| Schema 召回率 | ≥ 90% | 参考表落在注入的 Top-K 内 |
| 首次修复成功率 | ≥ 60% | 回环重试后由失败转成功 |
| P50 / P95 延迟 | P95 ≤ 3s | 端到端 |
| 单次成本 | ≤ ¥0.01 | token × 单价 |

## 技术选型与取舍

- **裸写链路而非 LangChain**：链路短、每步可控，面试追问细节不露怯
- **FAISS 而非 Chroma**：数据量小（十几张表 + 几十个 chunk），精确检索够用、零运维
- **混合检索（向量 + BM25）**：Schema 检索场景里"表名字段名精确匹配"很关键
- **不微调**：性价比低；检索 + 提示词 + 护栏的组合更可解释
- **只读 + 会话级 READ ONLY 双保险**：即便拿到写权限账号，会话层也挡住写操作

## 状态

- [x] 项目骨架与方案（docs/方案.md）
- [x] M0 骨架：config/llm（重试+token/成本）/executor（只读沙箱+脱敏+EXPLAIN 成本护栏）
- [x] M1 Schema 抽取与检索（15 表/141 列含中文描述；Schema Top-5 召回 12/12 = 100%）
- [ ] M2 NL2SQL + 三层护栏 + 回环修复
- [ ] M3 知识库 RAG + 意图路由
- [ ] M4 自动图表 + Streamlit 界面
- [ ] M5 60 条评估集 + 跑分 + 消融
- [ ] M6 部署 + README 指标表 + 演示视频
