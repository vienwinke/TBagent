# -*- coding: utf-8 -*-
"""生成知识库语料：业务规则 / 数据字典 / 常见问题

为什么自己写语料：RAG 的效果上限由语料决定。这里把 treatbord 的**业务语义**（状态机、名额规则、
信用分、审核、结算、举报闭环）写成可检索文档 —— 与数据分支共用同一套业务口径，
保证"查数据"和"查规则"给出**一致**的答案。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "data" / "knowledge"

BUSINESS = """# 业务规则

## 任务状态机
任务（task）的状态流转：
- OPEN（待接取）：发布后初始状态，可被接取
- IN_PROGRESS（进行中）：有人接取后进入
- REVIEWING（待确认）：接取者已提交凭证，等待发布者审核
- SETTLED（已完成）：全部审核通过并结算
- EXPIRED（已关闭-过期）：到接取截止仍无人接取，或到期未完成
- CANCELLED（已关闭-取消）：发布者主动取消

## 接取状态机
接取记录（task_claim）的状态：
- CLAIMED（已接取）→ SUBMITTED（已提交凭证）→ APPROVED（审核通过）
- SUBMITTED → REJECTED（审核驳回，可重新提交）
- CLAIMED → CANCELLED（接取者主动取消，或超时未提交被自动取消）
- 提交后发布者超过 48 小时未审核，系统自动通过（AUTO_APPROVED）

## 名额规则
- 每个任务有 quota（名额上限），claimed_count 是已占用的名额计数
- 接取通过原子 SQL 扣减：`claimed_count = claimed_count + 1 WHERE claimed_count < quota`
- **取消接取会释放名额**（claimed_count 减 1）：因此判断"某任务是否被接取过"，
  应以 task.claimed_count 为准；若查 task_claim 明细，必须排除 status='CANCELLED'
- 名额已满（claimed_count >= quota）的任务不能再被接取

## 信用分规则
- 新用户初始信用分 100（user.credit_score）
- 接取后超时未提交：扣分（默认 5 分，见 app_config.credit.penalty.overdue）
- 审核被驳回：扣分（默认 3 分，见 app_config.credit.penalty.reject）
- 信用分低于门槛（默认 60，见 app_config.credit.claim.threshold）不能接取任务

## 审核与结算
- 接取者提交凭证（task_submission，含文本与图片 file_ids）后进入待确认
- 发布者审核：通过（APPROVED）或驳回（REJECTED，需填 review_note）
- 审核通过后生成结算记录（settlement），金额为接取时快照的 task_claim.reward
- 本项目 MVP 只跑结算状态位（settlement.status: 0=未结算 / 1=已结算），不做真实打款

## 举报与内容安全
- 举报（report）：举报人、目标类型（task/claim/user）、目标 id、原因
- 处理状态 report.status：0=待处理，1=已处理；处理人记录在 handler_id
- 内容安全：文本过 msgSecCheck，图片过 mediaCheckAsync；
  文件检测状态 file.sec_status：0=待检测 / 1=通过 / 2=不通过（不通过的文件不下发）

## 用户与权限
- 用户状态 user.status：0=正常，1=封禁（封禁后 JWT 加入黑名单，立即失效）
- 角色 user.role：0=普通用户，1=管理员（管理员可处理举报、封禁用户、查审计日志）

## 安全与隐私
- 敏感字段：openid、unionid、password_hash、ip —— 查询结果中一律脱敏为 [已脱敏]
- 数据库访问为只读：只允许 SELECT，禁止 UPDATE/DELETE/DDL，禁止访问系统库
- 单次查询结果行数上限 200，执行超时 3 秒，EXPLAIN 预估扫描行数超过 10 万行会被拒绝
"""

FAQ = """# 常见问题

## 怎么发布任务？
进入「发布」页，填写任务标题（不超过 50 字）、任务描述（不超过 2000 字）、赏金金额（reward）、
接取名额（quota）、接取截止时间（claim_deadline）与完成截止时间（deadline）。
提交后任务为 OPEN 状态，出现在任务大厅可被接取。发布者不能接取自己发布的任务。

## 怎么接取任务？
在任务大厅选择任务进入详情，点「接取」。系统会校验：任务必须为 OPEN 且名额未满、
不能是自己的任务、信用分不低于门槛（默认 60）。接取成功后占用一个名额。

## 接取后可以取消吗？
可以，在接取截止前可主动取消，取消后名额被释放。若超过提交时限仍未提交凭证，
系统会定时扫描并自动取消，同时扣除信用分。

## 提交凭证后多久能结算？
发布者应在 48 小时内审核；若超时未处理，系统自动通过（AUTO_APPROVED）并生成结算记录。
MVP 阶段结算只更新状态位，不进行真实打款。

## 被封禁了怎么办？
封禁后所有 token 立即失效、无法登录与操作。可联系管理员申诉；管理员在管理后台解除封禁后恢复。

## 怎么设置账号密码？
「我的」→「设置」→「账号密码」，设置后可用「账号密码登录」。设置账号密码要求
用户名 4~32 位字母数字下划线，密码 6~64 位。

## 怎么注销账号？
「我的」→「设置」→「注销账号」。注销后个人数据被匿名化处理且不可恢复。

## 数据问答能查哪些内容？
可以查 treatbord 数据库的 14 张业务表（用户、任务、接取、凭证、文件、通知、互评、举报、
结算、状态日志、审计日志、登录日志、系统配置）的统计数据与明细；
**不能**执行写操作、不能访问其它数据库、敏感字段会被自动脱敏。
"""


def build_data_dictionary() -> str:
    """由 schema.json 生成数据字典（与数据分支使用同一份 Schema 描述，保证口径一致）"""
    schema = json.loads((ROOT / "data" / "schema.json").read_text(encoding="utf-8"))
    lines = ["# 数据字典", "", "数据库共 %d 张业务表。以下为表与关键字段说明。" % schema["table_count"], ""]
    for t in schema["tables"]:
        lines.append("## %s（%s）" % (t["name"], t["desc"]))
        lines.append("约 %d 行数据。字段：" % t["rows"])
        for c in t["columns"]:
            flag = "（敏感，查询结果会脱敏）" if c.get("sensitive") else ""
            lines.append("- %s %s：%s%s" % (c["name"], c["type"], c.get("desc") or "—", flag))
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    KB.mkdir(parents=True, exist_ok=True)
    files = {
        "01-业务规则.md": BUSINESS,
        "02-数据字典.md": build_data_dictionary(),
        "03-常见问题.md": FAQ,
    }
    for name, text in files.items():
        (KB / name).write_text(text, encoding="utf-8")
    total = sum(len(t) for t in files.values())
    print("✅ 知识库语料生成: %s（%d 个文档 / %d 字符）" % (KB, len(files), total))
    for name, text in files.items():
        print("   %-18s %5d 字符  %d 行" % (name, len(text), len(text.splitlines())))


if __name__ == "__main__":
    main()