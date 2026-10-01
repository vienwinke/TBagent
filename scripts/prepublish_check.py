# -*- coding: utf-8 -*-
"""发布前审计：确保公开仓库不含密钥、不含真实敏感数据、结构完整

用法：
    python scripts/prepublish_check.py                  # 全量审计
    python scripts/prepublish_check.py --secrets-only   # 只扫密钥（pre-commit 钩子用，快）
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAIL, WARN, OK = [], [], []


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout


# 密钥扫描核心抽到 scripts/secret_scan.py：纯函数、可被 tests 直接 import，
# 这里的 CLI 流程 import 即执行，不适合放判定逻辑。
try:                                    # 作为脚本运行（sys.path[0] = scripts/）
    from secret_scan import iter_secret_hits, scan_secrets  # noqa: F401
except ImportError:                     # 作为包导入（tests）
    from scripts.secret_scan import iter_secret_hits, scan_secrets  # noqa: F401


tracked = [l.strip() for l in git("ls-files").splitlines() if l.strip()]

# pre-commit 快速通道：只扫密钥，命中即非 0 退出（阻断提交）
if "--secrets-only" in sys.argv:
    gate_hits = list(iter_secret_hits(ROOT, tracked))
    for f, desc, snippet in gate_hits:
        print("   ✗ %s 命中 %s: %s" % (f, desc, snippet))
    print("密钥门禁：扫描 %d 个文件，命中 %d 条 %s"
          % (len(tracked), len(gate_hits), "✓ 放行" if not gate_hits else "✗ 已阻断提交"))
    sys.exit(1 if gate_hits else 0)

print("=" * 74)
print("1) 待发布文件清单（git 跟踪）")
print("   共 %d 个文件" % len(tracked))

forbidden = [f for f in tracked if re.search(r"(^|/)(\.env$|\.venv/|__pycache__/|\.git/)", f)]
for f in forbidden:
    FAIL.append("不应提交的文件: %s" % f)
print("   .env / .venv / __pycache__ 是否被跟踪: %s" % ("否 ✓" if not forbidden else "是 ✗"))

snap = [f for f in tracked if f.endswith(".sqlite")]
print("   演示快照是否随仓库分发: %s" % ("是 ✓ (%s)" % snap[0] if snap else "否 ⚠️（公开演示会连不上数据库）"))
if not snap:
    WARN.append("data/snapshot.sqlite 未跟踪，云端演示需要它")

print("=" * 74)
print("2) 密钥泄漏扫描（跟踪文件内容）")
hits = 0
for f, desc, snippet in iter_secret_hits(ROOT, tracked):
    hits += 1
    FAIL.append("%s 命中 %s: %s" % (f, desc, snippet))
print("   命中数: %d %s" % (hits, "✓" if hits == 0 else "✗"))

print("=" * 74)
print("3) 演示快照的敏感数据检查")
if snap:
    conn = sqlite3.connect(ROOT / snap[0])
    def q(sql):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error as e:
            return [("ERROR", str(e))]
    bad_openid = q("SELECT COUNT(*) FROM user WHERE openid IS NOT NULL AND openid NOT LIKE 'openid_demo_%'")[0][0]
    bad_pwd = q("SELECT COUNT(*) FROM user WHERE password_hash IS NOT NULL AND password_hash <> 'demo_hash_removed'")[0][0]
    bad_union = q("SELECT COUNT(*) FROM user WHERE unionid IS NOT NULL")[0][0]
    ips = q("SELECT DISTINCT ip FROM login_log LIMIT 5")
    bad_ip = sum(1 for (v,) in ips if v and not str(v).startswith("10.0.0."))
    rows = q("SELECT (SELECT COUNT(*) FROM user), (SELECT COUNT(*) FROM task), (SELECT COUNT(*) FROM task_claim)")
    print("   用户 %s / 任务 %s / 接取 %s" % rows[0])
    print("   真实 openid 残留: %s | 真实 password_hash 残留: %s | unionid 非空: %s | 真实 IP 残留: %s"
          % (bad_openid, bad_pwd, bad_union, bad_ip))
    if bad_openid or bad_pwd or bad_union or bad_ip:
        FAIL.append("快照仍含真实敏感数据，禁止公开")
    conn.close()
else:
    WARN.append("无快照，跳过敏感数据检查")

print("=" * 74)
print("4) 结构完整性")
for f, desc in [("README.md", "项目说明"), ("app.py", "Streamlit 入口"), ("requirements.txt", "依赖"),
                ("docs/部署.md", "部署文档"), ("docs/面试讲稿.md", "面试讲稿"),
                ("docs/评估报告.md", "评估报告"), ("docs/视频脚本.md", "视频脚本"),
                ("eval/cases.yaml", "60 题评估集"), ("tests", "测试目录")]:
    exists = (ROOT / f).exists()
    print("   %-24s %s" % (f, "✓" if exists else "✗ 缺失"))
    if not exists:
        FAIL.append("缺少 %s（%s）" % (f, desc))
if not (ROOT / "LICENSE").exists():
    WARN.append("没有 LICENSE（开源仓库建议加，MIT 即可）")

print("=" * 74)
size = subprocess.run(["du", "-sh", str(ROOT)], capture_output=True, text=True).stdout.split()[0]
print("5) 仓库体积（含 .venv）: %s ｜ git 跟踪文件: %d 个" % (size, len(tracked)))

print("=" * 74)
print("结论")
for x in FAIL:
    print("   ✗ %s" % x)
for x in WARN:
    print("   ⚠️ %s" % x)
if not FAIL:
    print("   ✅ 可以公开发布（无密钥、无真实敏感数据、结构完整）")
print()
if not FAIL:
    print("推送到 GitHub：")
    print("   cd ~/Project/agent")
    print("   git remote add origin https://github.com/<你的用户名>/data-agent.git")
    print("   git push -u origin main")
    print("   然后 share.streamlit.io → New app → Main file path 填 app.py → Secrets 见 docs/部署.md")
sys.exit(1 if FAIL else 0)