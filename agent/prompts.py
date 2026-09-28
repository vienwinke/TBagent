# -*- coding: utf-8 -*-
"""提示词集中管理（与代码分离，便于 A/B 与版本化）

设计要点：
1. system 里灌入「业务规则」（状态枚举、逻辑删除、敏感列、时区），
   这是 Text2SQL 准确率的关键——模型不知道 `status='OPEN'` 还是 `status=1`。
2. 只注入检索到的 Top-K 表的 Schema（省 token、少选错表）。
3. 强制 JSON 输出（sql + reason），便于程序消费与审计。
"""
from __future__ import annotations

# 业务规则：从 schema.json / AGENTS.md 提炼，避免模型瞎猜枚举值
DOMAIN_RULES = """【业务规则（必须遵守）】
数据库：MySQL 8，库名 treatbord（任务接取平台），14 张业务表。
1. 逻辑删除：`user/task/task_claim/task_submission/file/notification/review/report/settlement` 有 `deleted` 字段，
   查询务必加 `deleted = 0`。日志类表（`audit_log`/`login_log`/`task_status_log`/`claim_status_log`/`app_config`）**没有** `deleted`。
2. 状态枚举（注意大小写与类型）：
   - task.status VARCHAR: OPEN(待接取) / IN_PROGRESS(进行中) / REVIEWING(待确认) / SETTLED(已完成) / EXPIRED(已关闭-过期) / CANCELLED(已关闭-取消)
   - task_claim.status VARCHAR: CLAIMED(已接取) / SUBMITTED(已提交) / APPROVED(已通过) / REJECTED(已驳回) / CANCELLED(已取消)
   - user.status TINYINT: 0=正常 1=封禁；user.role TINYINT: 0=普通用户 1=管理员
   - notification.is_read TINYINT: 0=未读 1=已读
   - report.status TINYINT: 0=待处理 1=已处理
   - settlement.status TINYINT: 0=未结算 1=已结算
   - file.sec_status TINYINT: 0=待检测 1=通过 2=不通过
   - login_log.success TINYINT: 1=成功 0=失败，失败原因在 fail_reason（如 LOGIN_LOCKED）
3. 金额字段 `task.reward`/`task_claim.reward`/`settlement.amount` 为 DECIMAL，聚合用 SUM()/AVG()。
4. 时间字段为 DATETIME（Asia/Shanghai）。"最近 7 天"= `create_time >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)`；
   "今天"= `DATE(create_time) = CURDATE()`；按月用 `DATE_FORMAT(create_time, '%%Y-%%m')`。
5. 表关系：task.publisher_id→user.id；task_claim.task_id→task.id；task_claim.user_id→user.id；
   task_submission.claim_id→task_claim.id；review.claim_id→task_claim.id；review.from_user_id/to_user_id→user.id；
   settlement.claim_id→task_claim.id；report.reporter_id/handler_id→user.id；report.target_type/target_id 指向被举报对象；
   notification.biz_id 为业务 id。
6. 敏感列（`openid`/`unionid`/`password_hash`/`ip`）在结果中会被系统自动脱敏，**不要试图绕过或拼接**。
7. 只允许 SELECT（可用 WITH/JOIN/GROUP BY）；禁止 UPDATE/DELETE/DDL、禁止访问其它库与 information_schema。"""

NL2SQL_SYSTEM = """你是一位严谨的 MySQL 数据分析工程师。
把用户的自然语言问题翻译成 **一条可直接执行的只读 SELECT 查询**。

硬性要求：
- 只输出 SELECT（允许 WITH/CTE、JOIN、GROUP BY、聚合），不要写任何写操作。
- 只使用下面提供的表和列，列名必须完全一致，不要臆造字段。
- 必须带 LIMIT（不明显超过 200 行）。
- 中文别名便于阅读（如 `COUNT(*) AS 接取数`）。
- 输出严格 JSON：{"sql": "…", "reason": "一句话说明思路", "tables": ["用到的表"]}
  不要输出多余文字、不要用 Markdown 代码块包裹。"""


def nl2sql_messages(schema_text: str, question: str, *, error: str | None = None,
                    prev_sql: str | None = None) -> list[dict[str, str]]:
    """构造 NL2SQL 的对话消息；error/prev_sql 用于回环修复"""
    user = f"""【可用表结构（已按相关度检索）】
{schema_text}

{DOMAIN_RULES}

【用户问题】
{question}"""
    if error or prev_sql:
        user += f"""

【上一次尝试失败，请修正】
上一次 SQL：
{prev_sql or "(未产出)"}
失败原因：{error or "查询返回 0 行，可能条件/枚举值不对"}
请分析原因后给出修正后的 SQL（仍然只输出要求的 JSON）。"""
    return [{"role": "system", "content": NL2SQL_SYSTEM}, {"role": "user", "content": user}]
