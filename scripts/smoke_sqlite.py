# -*- coding: utf-8 -*-
import os, sys
os.environ["DB_BACKEND"] = "sqlite"
sys.path.insert(0, '/home/KeYa/Project/agent')
from config import IS_SQLITE, SQL_DIALECT, setup_logging
from agent import executor as ex, nl2sql, router, chart
setup_logging()
print("后端 =", "sqlite" if IS_SQLITE else "mysql", "| 方言 =", SQL_DIALECT)
h = ex.health()
print("健康检查:", h)
r = ex.execute_readonly("SELECT COUNT(*) FROM task")
print("只读查询 task 计数 =", r.rows[0][0])
for sql in ["UPDATE task SET title='x'", "DELETE FROM user", "SELECT COUNT(*) FROM task; DROP TABLE task"]:
    try:
        ex.execute_readonly(sql); print("FAIL 未拦截:", sql)
    except ex.SqlError as e:
        print("已拒绝:", sql[:26], "->", str(e)[:72])
r2 = ex.execute_readonly("SELECT id, openid, password_hash FROM user LIMIT 2")
print("脱敏列 =", r2.masked_columns, "| 样例 =", r2.rows[0])
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