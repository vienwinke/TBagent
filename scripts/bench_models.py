# -*- coding: utf-8 -*-
"""模型选型实测：用真实 NL2SQL 小任务比较 延迟 / 输出是否为空 / JSON 是否合规"""
import json, sys, time
sys.path.insert(0, '/home/KeYa/Project/agent')
from config import LLM
from openai import OpenAI

CANDIDATES = [
    "google/gemini-3.5-flash-lite",
    "google/gemini-3.5-flash",
    "z-ai/glm-5.3-flash",
    "Qwen/Qwen3.8-Flash",
    "gpt-5.4-mini",
    "deepseek/deepseek-v4.1-flash",
    "tencent/hy4-preview",
    "inclusionai/ling-3.0-flash-sante:free",
]
PROMPT = """你是 MySQL 分析工程师。只输出 JSON，形如 {"sql":"...","reason":"..."}，不要 Markdown。
表 task（任务主表）: id bigint, publisher_id bigint, title varchar, reward decimal, quota int, claimed_count int, claim_deadline datetime, deadline datetime, status varchar(OPEN/IN_PROGRESS/REVIEWING/SETTLED/EXPIRED/CANCELLED), deleted tinyint
表 user（用户表）: id bigint, nickname varchar, credit_score int, role tinyint, status tinyint(0正常/1封禁), deleted tinyint
问题：待接取的任务有几个？"""
client = OpenAI(api_key=LLM.api_key, base_url=LLM.base_url, timeout=120, max_retries=0)

print("%-38s %-8s %-6s %-6s %-9s %s" % ("模型", "延迟", "in", "out", "JSON", "结论"))
print("-" * 104)
rows = []
for m in CANDIDATES:
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=m, messages=[{"role": "user", "content": PROMPT}],
            temperature=0, max_tokens=2048, response_format={"type": "json_object"})
        dt = time.time() - t0
        ch = r.choices[0]
        content = (ch.message.content or "").strip()
        u = r.usage
        ok = False
        note = ""
        if content:
            try:
                d = json.loads(content); ok = bool(d.get("sql")); note = (d.get("sql") or "")[:40]
            except Exception as e:
                note = "JSON 解析失败: %s" % type(e).__name__
        else:
            note = "content 为空 (finish=%s)" % ch.finish_reason
        print("%-38s %-8s %-6s %-6s %-9s %s" % (m, "%.1fs" % dt, u.prompt_tokens if u else "-",
                                                u.completion_tokens if u else "-", "OK" if ok else "NO", note[:44]))
        rows.append({"model": m, "seconds": round(dt, 2), "json_ok": ok,
                     "out_tokens": u.completion_tokens if u else 0, "note": note})
    except Exception as e:
        dt = time.time() - t0
        print("%-38s %-8s %-6s %-6s %-9s %s" % (m, "%.1fs" % dt, "-", "-", "NO", "%s: %s" % (type(e).__name__, str(e)[:36])))
        rows.append({"model": m, "seconds": round(dt, 2), "json_ok": False, "error": type(e).__name__})

good = [r for r in rows if r.get("json_ok")]
good.sort(key=lambda r: r["seconds"])
print("-" * 104)
if good:
    print("★ 可用且相对最快: %s (%.1fs)" % (good[0]["model"], good[0]["seconds"]))
    print("  候选排序:", ", ".join("%s(%.1fs)" % (r["model"], r["seconds"]) for r in good[:5]))
else:
    print("✗ 没有模型通过 JSON 校验，需要调大 max_tokens 或换模型族")
json.dump(rows, open('/tmp/bench.json', 'w'), ensure_ascii=False, indent=2)