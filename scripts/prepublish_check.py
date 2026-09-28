# -*- coding: utf-8 -*-
"""发布前审计：确保公开仓库不含密钥、不含真实敏感数据、结构完整

用法：python scripts/prepublish_check.py
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


print("=" * 74)
print("1) 待发布文件清单（git 跟踪）")
tracked = [l.strip() for l in git("ls-files").splitlines() if l.strip()]
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
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI/DeepSeek 风格密钥"),
    # 注意用 [ \t]* 而非 \s*：否则 "LLM_API_KEY=" 会跨行匹配到下一行（实测误报）
    (r"(LLM_API_KEY|DEEPSEEK_API_KEY)[ \t]*=[ \t]*[\"']?[A-Za-z0-9_\-]{12,}", "环境变量里的真实密钥"),
    (r"password\s*=\s*[\"'][^\"']{8,}[\"']", "硬编码密码"),
]
hits = 0
for f in tracked:
    p = ROOT / f
    if not p.is_file() or p.stat().st_size > 2_000_000:
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    if f.endswith("prepublish_check.py"):
        continue          # 本脚本自身含模式串
    for pat, desc in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            hits += 1
            FAIL.append("%s 命中 %s: %s" % (f, desc, m.group(0)[:40]))
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