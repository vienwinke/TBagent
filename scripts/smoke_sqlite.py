# -*- coding: utf-8 -*-
"""sqlite 后端冒烟：只读护栏 + 脱敏 + 两条真实问题（不调模型的那条链路）

注意：所有 SQL 都必须经 `policy.rewrite()` —— `executor.execute_readonly` 现在
**只接受 `policy.RewrittenSql`**（契约 §6 决策 B：把"唯一出口"从约定变成类型约束）。
危险语句会在 rewrite 阶段就被拒（PolicyDenied），根本走不到执行。
"""
import os, sys
os.environ["DB_BACKEND"] = "sqlite"
sys.path.insert(0, '/home/KeYa/Project/agent')
from config import IS_SQLITE, SQL_DIALECT, setup_logging
from agent import executor as ex, nl2sql, policy, router, chart
from agent.policy import ROLE_ADMIN, Principal

setup_logging()
ADMIN = Principal(user_id=1, role=ROLE_ADMIN)


def run(sql: str):
    """经唯一出口执行：rewrite（护栏 + 行级隔离）→ execute"""
    return policy.execute(policy.rewrite(sql, ADMIN), check_cost=False)


print("后端 =", "sqlite" if IS_SQLITE else "mysql", "| 方言 =", SQL_DIALECT)
print("健康检查:", ex.health())
r = run("SELECT COUNT(*) FROM task")
print("只读查询 task 计数 =", r.rows[0][0])

for sql in ["UPDATE task SET title='x'", "DELETE FROM user",
            "SELECT COUNT(*) FROM task; DROP TABLE task"]:
    try:
        policy.rewrite(sql, ADMIN)
        print("FAIL 未拦截:", sql)
    except (policy.PolicyDenied, ex.SqlError) as e:
        print("已拒绝:", sql[:26], "->", str(e)[:72])

r2 = run("SELECT id, openid, password_hash FROM user LIMIT 2")
print("脱敏列 =", r2.masked_columns, "| 样例 =", r2.rows[0])
print()

# 类型约束自检：裸字符串必须被拒（这是"唯一出口"的硬保证）
try:
    ex.execute_readonly("SELECT 1")
    print("FAIL 裸 SQL 竟然执行了")
except TypeError as e:
    print("已拒绝裸 SQL:", str(e)[:80])

print()
for q in ["待接取的任务有几个？", "每个发布者发布了多少个任务？"]:
    rr = nl2sql.answer(q)
    print("👤", q)
    if rr.ok:
        cols = rr.query["columns"]; rows = [tuple(x.values()) for x in rr.rows]
        print("   SQL:", rr.sql[:100])
        print("   结果:", len(rows), "行 | 选图:", chart.choose_spec(cols, rows).kind)
    else:
        print("   失败:", (rr.error or "")[:110])
