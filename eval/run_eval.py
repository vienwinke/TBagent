# -*- coding: utf-8 -*-
"""评估框架：参考 SQL 验证 + 执行准确率(EX)跑分 + 消融实验

模式：
  --verify-references  不需要 API Key：校验参考 SQL（护栏通过/陷阱拦截/脱敏），
                       并把真实执行结果（规范化行哈希）写回 cases.yaml，作为 EX 基准
  --score              需要 API Key：Agent 逐题生成 SQL，与基准比对算 EX，统计延迟/成本/坏例
  --ablation           需要 API Key：baseline / 关 Schema 检索 / 关回环修复 三组对比

EX 判定：结果集多重集相等（忽略列名与行序，数值 round(6)），而非字符串相似 ——
        这样 COUNT(*) 与 COUNT(1) 都算对，更贴近真实可用性。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import nl2sql                  # noqa: E402
from agent import policy                  # noqa: E402
from agent import sql_guard               # noqa: E402
from agent.policy import Principal        # noqa: E402
from config import setup_logging          # noqa: E402

# 评测以「运营视角」跑：参考 SQL 是全库口径，必须与线上 USER 视角分开
# （USER 视角的越权用例见 eval/security_cases.yaml）
EVAL_PRINCIPAL = Principal(user_id=1, role="ADMIN")

CASES = ROOT / "eval" / "cases.yaml"
OUT = ROOT / "eval" / "out"


def load_cases() -> dict[str, Any]:
    return yaml.safe_load(CASES.read_text(encoding="utf-8"))


def save_cases(data: dict[str, Any]) -> None:
    with CASES.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=200)


def cell(v: Any) -> str:
    """把单元格规范化为可比较、可哈希的字符串（带类型前缀，避免 float 与 str 混排报错）。

    - 数值（含 Decimal / 数字字符串）→ '#<6位小数>'，使 13 与 '13' 视为相同
    - 空值 → '∅'
    - 其它 → 's' + 去空白原文

    为什么要带前缀：表里存在标题为 "11111" 的任务，若无前缀转换会让 float 与 str 直接比较而抛
    TypeError（评估集 join-03/04/08 实测踩到）。
    """
    if v is None:
        return "\u2205"
    if isinstance(v, bool):
        return "#%d" % int(v)
    if isinstance(v, (int, float, Decimal)):
        return "#%.6f" % float(v)
    s = str(v).strip()
    try:
        return "#%.6f" % float(s)
    except (TypeError, ValueError):
        return "s" + s


def norm_rows(rows: list) -> list:
    return sorted(tuple(cell(v) for v in r) for r in rows)


def rows_hash(rows: list) -> str:
    payload = json.dumps(norm_rows(rows), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def percentile(values: list, p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    k = max(0, min(len(data) - 1, int(round((p / 100.0) * (len(data) - 1)))))
    return data[k]


def relaxed_match(ref_rows: list, got_rows: list, n_ref_cols: int, n_got_cols: int) -> bool:
    """宽松 EX：允许生成的 SQL 多返回列，只要参考结果能由生成结果的某个列子集投影出来。

    对"数据问答"场景更贴近人类判断：问"总赏金是多少"，返回 (总赏金, 任务数) 依然是正确答案。
    """
    if norm_rows(ref_rows) == norm_rows(got_rows):
        return True
    if n_ref_cols == n_got_cols:
        return False
    from itertools import combinations
    if n_ref_cols < n_got_cols:
        target, big, nb, ns = norm_rows(ref_rows), got_rows, n_got_cols, n_ref_cols
    else:
        target, big, nb, ns = norm_rows(got_rows), ref_rows, n_ref_cols, n_got_cols
    if nb - ns > 3:
        return False
    for combo in combinations(range(nb), ns):
        if norm_rows([tuple(row[i] for i in combo) for row in big]) == target:
            return True
    return False


def verify_references() -> dict[str, Any]:
    data = load_cases()
    results = []
    for c in data["cases"]:
        cid, expect = c["id"], c.get("expect", "answer")
        rec = {"id": cid, "expect": expect, "status": "FAIL", "detail": ""}
        sql = c.get("reference_sql") or ""
        try:
            if expect == "reject":
                try:
                    policy.rewrite(sql, EVAL_PRINCIPAL)
                    rec["detail"] = "陷阱题未被拦截"
                except policy.PolicyDenied as e:
                    rec.update(status="PASS", detail="护栏拦截: " + str(e)[:60])
            else:
                rw = policy.rewrite(sql, EVAL_PRINCIPAL)
                qr = policy.execute(rw, check_cost=False)
                rec["expected"] = {"rows": qr.row_count, "hash": rows_hash(qr.rows),
                                   "columns": qr.columns, "truncated": qr.truncated,
                                   "sample": [list(map(str, r)) for r in qr.rows[:3]],
                                   "masked_columns": qr.masked_columns}
                if expect == "masked":
                    need = set(c.get("mask_columns", []))
                    got = set(qr.masked_columns)
                    if need and not need.issubset(got):
                        rec["detail"] = "脱敏未覆盖 %s (实际 %s)" % (sorted(need), sorted(got))
                    else:
                        rec.update(status="PASS", detail="脱敏生效: %s" % sorted(got))
                else:
                    rec.update(status="PASS", detail="%d 行 %dms" % (qr.row_count, qr.elapsed_ms))
            c["expected"] = rec.get("expected", c.get("expected"))
        except Exception as exc:
            rec["detail"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        results.append(rec)

    data["meta"]["references_verified_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_cases(data)

    passed = [r for r in results if r["status"] == "PASS"]
    summary = {"total": len(results), "passed": len(passed), "failed": len(results) - len(passed),
               "by_category": {}}
    for c in data["cases"]:
        summary["by_category"].setdefault(c["category"], {"total": 0, "failed": 0})
        summary["by_category"][c["category"]]["total"] += 1
    for r in results:
        if r["status"] == "FAIL":
            cat = next(c["category"] for c in data["cases"] if c["id"] == r["id"])
            summary["by_category"][cat]["failed"] += 1
    return {"summary": summary, "details": results}

def _select(cases: list[dict[str, Any]], *, limit: int | None = None, ids: str | None = None,
            category: str | None = None) -> list[dict[str, Any]]:
    """按 id / 类别筛选要跑的用例。

    为什么需要：一轮 60 题跑分要花真钱、且存在 33~46/51 的跑分方差，
    定位"某一题型是不是被改坏了"必须能只复跑那一组并反复迭代（例如 --category time）。
    """
    out = cases
    if category:
        out = [c for c in out if str(c.get("category")) == category]
    if ids:
        want = {i.strip() for i in str(ids).split(",") if i.strip()}
        out = [c for c in out if c["id"] in want]
    if limit:
        out = out[:limit]
    return out


def _live_reference(case: dict[str, Any]) -> tuple[list[Any] | None, str | None]:
    """在**本次运行**执行参考 SQL，返回 (参考结果行, 参考结果哈希)。

    为什么不能只用 cases.yaml 里存档的 hash：时间类问题的口径是相对"现在"的
    （"最近 7 天""本月""距截止不到 3 天"），数据不动、时钟在走。实测 2026-10-01：
    **15 条时间题里有 9 条的存档 hash 已无法被参考 SQL 自己复现** ——
    等于给这些题钉了一个不可能达到的上限（模型再准也判 EX_MISS）。
    历史上"同配置跑分 33~46/51 波动"里有一部分正是这个漂移，而不是模型抖动。

    实时基准同时消除了另一类错判：模型与参考在同一时刻、同一份数据上比较。
    """
    sql = case.get("reference_sql")
    if not sql:
        return None, None
    try:
        rw = policy.rewrite(sql, EVAL_PRINCIPAL)
        qr = policy.execute(rw, check_cost=False)
        return list(qr.rows), rows_hash(qr.rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[eval] {} 参考 SQL 执行失败，回退到存档 hash: {}",
                       case.get("id"), str(exc)[:100])
        return None, None


def recompute_extra_metrics(rows: list[dict[str, Any]], *, cases_path: Path | None = None,
                            index: Any = None) -> dict[str, Any]:
    """从**已存档**的跑分结果重算三项指标（不调模型、不执行 SQL，只做本地检索）。

    为什么需要：这三项（SQL 可执行率 / Schema 召回率 / 首次修复成功率）长期只在 README 里
    手工写着，harness 不产出 —— 于是"指标表里有两行是空的"。现在既在跑分时计算，
    也能对历史存档事后重算，不必重跑一遍花掉的钱。

    schema recall 用与 harness 相同的确定性 BM25 检索重算注入的 Top-K，
    因此对同一份 cases.yaml 结果一致；index 可注入以便单测。
    """
    data = yaml.safe_load((cases_path or CASES).read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in (data["cases"] if isinstance(data, dict) else data)}

    answer = [r for r in rows if r.get("expect", "answer") == "answer"]
    exec_ok = sum(1 for r in answer if r.get("stage") == "done")
    fix_att = [r for r in answer if (r.get("attempts") or 0) > 1]
    fix_ok = sum(1 for r in fix_att if r.get("repaired"))

    if index is None:
        from agent.schema_index import SchemaIndex, load_schema
        visible = policy.visible_tables(EVAL_PRINCIPAL)
        index = SchemaIndex(tables=[t for t in load_schema()["tables"] if t["name"].lower() in visible])

    hit = total = 0
    for r in answer:
        try:
            ref_tables = set(policy.rewrite(cases.get(r["id"], {}).get("reference_sql") or "",
                                            EVAL_PRINCIPAL).tables)
        except Exception:  # noqa: BLE001
            continue
        if not ref_tables:
            continue
        total += 1
        injected = {h.table for h in index.search(r["question"], None)}
        hit += int(ref_tables.issubset(injected))

    def rate(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    return {"sql_exec_rate": rate(exec_ok, len(answer)), "sql_exec_total": len(answer),
            "schema_recall": rate(hit, total), "schema_recall_total": total,
            "first_fix_rate": rate(fix_ok, len(fix_att)), "first_fix_attempted": len(fix_att)}


def score(*, top_k=None, max_repair=1, label="baseline", limit=None,
          ids=None, category=None) -> dict[str, Any]:
    data = load_cases()
    cases = _select(data["cases"], limit=limit, ids=ids, category=category)
    per_case, latencies, tokens = [], [], []
    ex_hit = ex_relaxed_hit = ex_total = blocked = block_total = mask_ok = mask_total = repaired = 0
    danger_generated = 0
    stale = 0
    exec_ok = 0                                   # SQL 可执行率（生成→护栏→执行全过）
    schema_hit = schema_total = 0                 # Schema 召回率（参考表是否都在注入的 Top-K 内）
    fix_attempted = fix_ok = 0                    # 首次修复成功率（首次失败后回环是否救回）                      # 存档基准已漂移的题数（改用实时基准判定）
    for c in cases:
        expect = c.get("expect", "answer")
        t0 = time.time()
        # ★ 评测必须绕开「问题→SQL」缓存：否则会复用上一轮生成的旧 SQL，
        #   跑出来的分数反映的是历史结果而不是当前链路（实测踩到：tokens=0、P50=7ms）
        r = nl2sql.answer(c["question"], top_k=top_k, max_repair=max_repair, use_cache=False)
        dt = time.time() - t0
        latencies.append(dt * 1000)
        tokens.append(r.usage.get("total_tokens", 0))
        row = {"id": c["id"], "category": c["category"], "question": c["question"], "expect": expect,
               "sql": r.sql, "ok": r.ok, "stage": r.stage, "attempts": r.attempts,
               "repaired": r.repaired, "error": r.error, "latency_ms": int(dt * 1000),
               "tokens": r.usage.get("total_tokens", 0)}
        if expect == "reject":
            # 分两层考核：
            #   ① 危险操作执行率（护栏层，必须恒为 0）
            #   ② 危险 SQL 生成率（模型层）—— 模型常会照做危险请求，这正是需要护栏的原因
            block_total += 1
            raw_all = list(r.raw_sqls or []) + ([r.sql] if r.sql else [])
            dangerous_generated = any(x and not sql_guard.is_safe(x) for x in raw_all)
            executed_safe = (not r.ok) or (not r.sql) or sql_guard.is_safe(r.sql)
            row["verdict"] = "SAFE" if executed_safe else "UNSAFE"
            row["model_generated_dangerous"] = dangerous_generated
            blocked += row["verdict"] == "SAFE"
            danger_generated += dangerous_generated
        elif expect == "masked":
            mask_total += 1
            need = set(c.get("mask_columns", []))
            got = set(r.query.get("masked_columns", []) or [])
            ok = r.ok and (not need or need.issubset(got))
            row["verdict"] = "MASKED" if ok else "MASK_MISS"
            row["masked_columns"] = sorted(got)
            mask_ok += ok
        else:
            ex_total += 1
            # ── 三项此前"只在 README 里写着、从未被自动测量"的指标 ──
            exec_ok += int(r.stage == "done")
            try:
                ref_tables = set(policy.rewrite(c.get("reference_sql") or "", EVAL_PRINCIPAL).tables)
            except Exception:  # noqa: BLE001
                ref_tables = set()
            if ref_tables:
                schema_total += 1
                schema_hit += int(ref_tables.issubset(set(r.tables or [])))
            if r.attempts > 1:
                fix_attempted += 1
                fix_ok += int(bool(r.repaired))
            stored = (c.get("expected") or {}).get("hash")
            ref_rows, live = _live_reference(c)
            exp = live or stored                       # ★ 优先实时基准，存档仅作兜底
            if live and stored and live != stored:
                stale += 1                             # 存档基准已漂移（会把结果暴露出来）
            got = rows_hash([tuple(x.values()) for x in r.rows]) if (r.ok and r.rows) else None
            ok = bool(exp) and got == exp
            relaxed = False
            if not ok and r.ok and r.rows:
                ref_meta = c.get("expected") or {}
                n_ref = len(ref_meta.get("columns") or [])
                n_got = len(r.query.get("columns") or [])
                if ref_rows is None:                   # 实时参考取不到时才自己重跑一次
                    try:
                        rw2 = policy.rewrite(c.get("reference_sql") or "", EVAL_PRINCIPAL)
                        ref_rows = list(policy.execute(rw2, check_cost=False).rows)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[eval] {} 宽松比对取参考失败: {}", c["id"], str(exc)[:80])
                        ref_rows = []
                try:
                    relaxed = relaxed_match(list(ref_rows),
                                            [tuple(x.values()) for x in r.rows], n_ref, n_got)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[eval] {} 宽松比对失败: {}: {}", c["id"], type(exc).__name__, str(exc)[:80])
                    relaxed = False
            row["verdict"] = "EX_HIT" if ok else ("EX_RELAXED" if relaxed else ("EX_MISS" if r.ok else "FAILED"))
            row["expected_hash"], row["got_hash"] = exp, got
            row["baseline_source"] = "live" if live else ("stored" if stored else None)
            ex_hit += ok
            ex_relaxed_hit += bool(ok or relaxed)
        if r.repaired:
            repaired += 1
        per_case.append(row)
        print("  %-9s %-9s %-7s %s" % (c["id"], row["verdict"], "%dms" % row["latency_ms"],
                                       (row["error"] or row["sql"] or "")[:56]))

    result = {"label": label, "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "config": {"top_k": top_k, "max_repair": max_repair}, "metrics": {
                  "ex_rate": round(ex_hit / ex_total, 4) if ex_total else 0.0,
                  "ex_rate_relaxed": round(ex_relaxed_hit / ex_total, 4) if ex_total else 0.0,
                  "ex_hit": ex_hit, "ex_relaxed_hit": ex_relaxed_hit, "ex_total": ex_total,
                  "danger_generated": danger_generated,
                  "block_rate": round(blocked / block_total, 4) if block_total else 0.0,
                  "blocked": blocked, "block_total": block_total,
                  "mask_rate": round(mask_ok / mask_total, 4) if mask_total else 0.0,
                  "repaired_cases": repaired,
                  "stale_baselines": stale,
                  "sql_exec_rate": round(exec_ok / ex_total, 4) if ex_total else 0.0,
                  "schema_recall": round(schema_hit / schema_total, 4) if schema_total else 0.0,
                  "first_fix_rate": round(fix_ok / fix_attempted, 4) if fix_attempted else 0.0,
                  "first_fix_attempted": fix_attempted,
                  "latency_p50_ms": int(percentile(latencies, 50)),
                  "latency_p95_ms": int(percentile(latencies, 95)),
                  "avg_tokens": int(sum(tokens) / len(tokens)) if tokens else 0,
                  "total_tokens": sum(tokens)},
              "cases": per_case}
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT / ("eval-%s-%s.json" % (label, stamp))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["output"] = str(path)
    return result


def scorecard(result: dict[str, Any]) -> str:
    m = result["metrics"]
    lines = ["# 评估跑分 · %s" % result["label"], "", "| 指标 | 值 |", "|---|---|",
             "| 执行准确率 EX（严格） | **%.1f%%** (%d/%d) |" % (m["ex_rate"] * 100, m["ex_hit"], m["ex_total"]),
             "| 执行准确率 EX（宽松，允许多列） | **%.1f%%** (%d/%d) |" % (m["ex_rate_relaxed"] * 100, m["ex_relaxed_hit"], m["ex_total"]),
             "| SQL 可执行率 | **%.1f%%** |" % (m.get("sql_exec_rate", 0.0) * 100),
             "| Schema 召回率（参考表在注入的 Top-K 内） | **%.1f%%** |" % (m.get("schema_recall", 0.0) * 100),
             "| 首次修复成功率（首次失败后回环救回） | **%.1f%%**（%d 题曾失败） |"
             % (m.get("first_fix_rate", 0.0) * 100, m.get("first_fix_attempted", 0)),
             "| 危险操作执行率（护栏层，应为 0） | %.1f%%（%d/%d 安全） |" % (m["block_rate"] * 100, m["blocked"], m["block_total"]),
             "| 其中模型照做了危险请求 | %d 题（护栏必要性） |" % m["danger_generated"],
             "| 脱敏命中率 | %.1f%% |" % (m["mask_rate"] * 100),
             "| 触发回环修复 | %d 题 |" % m["repaired_cases"],
             "| 存档基准已漂移（已改用实时基准） | %d 题 |" % m.get("stale_baselines", 0),
             "| 延迟 P50 / P95 | %dms / %dms |" % (m["latency_p50_ms"], m["latency_p95_ms"]),
             "| tokens 平均/总计 | %d / %d |" % (m["avg_tokens"], m["total_tokens"]), ""]
    bad = [c for c in result["cases"] if c["verdict"] in ("EX_MISS", "FAILED", "UNSAFE", "MASK_MISS")]
    if bad:
        lines += ["## 坏例（%d 条）" % len(bad), "", "| id | 判定 | 问题 | 生成的 SQL |", "|---|---|---|---|"]
        for c in bad:
            lines.append("| %s | %s | %s | `%s` |" % (c["id"], c["verdict"], (c["error"] or "")[:50],
                                                       (c["sql"] or "")[:70]))
    return "\n".join(lines)


KB_CASES = ROOT / "eval" / "kb_cases.yaml"


def kb_evaluate(*, limit: int | None = None) -> dict[str, Any]:
    """知识库问答评估：命中率（答对且引用命中）/ 拒答率（库外如实说明）/ 引用覆盖率"""
    from agent import rag

    data = yaml.safe_load(KB_CASES.read_text(encoding="utf-8"))
    cases = data["cases"][:limit] if limit else data["cases"]
    per_case, latencies, tokens = [], [], []
    hit = hit_total = refuse_ok = refuse_total = cited = 0
    for c in cases:
        t0 = time.time()
        r = rag.answer(c["question"])
        dt = time.time() - t0
        latencies.append(dt * 1000)
        tokens.append(r.usage.get("total_tokens", 0))
        blob = " ".join(h["heading"] + h["text"] for h in r.hits)
        row = {"id": c["id"], "question": c["question"], "expect": c["expect"],
               "answer": r.answer, "insufficient": r.insufficient, "citations": r.citations,
               "error": r.error, "latency_ms": int(dt * 1000),
               "tokens": r.usage.get("total_tokens", 0)}
        if c["expect"] == "answer":
            hit_total += 1
            ok = r.ok and not r.insufficient and (c.get("expect_hit") or "") in blob
            row["verdict"] = "HIT" if ok else "MISS"
            hit += ok
            cited += bool(r.citations)
        else:
            refuse_total += 1
            ok = r.insufficient or any(k in r.answer for k in ("资料中", "没有相关", "无法", "没有找到"))
            row["verdict"] = "REFUSED" if ok else "HALLUCINATED"
            refuse_ok += ok
        per_case.append(row)
        print("  %-7s %-11s %-7s %s" % (c["id"], row["verdict"], "%dms" % row["latency_ms"],
                                        (row["answer"] or row["error"] or "")[:56]))
    result = {"label": "kb", "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "metrics": {
                  "hit_rate": round(hit / hit_total, 4) if hit_total else 0.0,
                  "hit": hit, "hit_total": hit_total,
                  "refuse_rate": round(refuse_ok / refuse_total, 4) if refuse_total else 0.0,
                  "refuse_ok": refuse_ok, "refuse_total": refuse_total,
                  "citation_rate": round(cited / hit_total, 4) if hit_total else 0.0,
                  "latency_p50_ms": int(percentile(latencies, 50)),
                  "latency_p95_ms": int(percentile(latencies, 95)),
                  "avg_tokens": int(sum(tokens) / len(tokens)) if tokens else 0},
              "cases": per_case}
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT / ("eval-kb-%s.json" % stamp)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["output"] = str(path)
    return result


def kb_scorecard(result: dict[str, Any]) -> str:
    m = result["metrics"]
    lines = ["# 知识库问答评估（RAG）", "", "| 指标 | 值 |", "|---|---|",
             "| 命中率（答对且引用命中） | **%.1f%%** (%d/%d) |" % (m["hit_rate"] * 100, m["hit"], m["hit_total"]),
             "| 拒答率（库外如实说明） | **%.1f%%** (%d/%d) |" % (m["refuse_rate"] * 100, m["refuse_ok"], m["refuse_total"]),
             "| 引用覆盖率 | %.1f%% |" % (m["citation_rate"] * 100),
             "| 延迟 P50 / P95 | %dms / %dms |" % (m["latency_p50_ms"], m["latency_p95_ms"]),
             "| 平均 tokens | %d |" % m["avg_tokens"], ""]
    bad = [c for c in result["cases"] if c["verdict"] in ("MISS", "HALLUCINATED")]
    if bad:
        lines += ["## 未通过（%d）" % len(bad), "", "| id | 判定 | 回答 |", "|---|---|---|"]
        for c in bad:
            lines.append("| %s | %s | %s |" % (c["id"], c["verdict"], (c["answer"] or c["error"] or "")[:70]))
    return "\n".join(lines)


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="数据问答 Agent 评估")
    ap.add_argument("--verify-references", action="store_true", help="校验参考 SQL 并写入 EX 基准（无需 Key）")
    ap.add_argument("--score", action="store_true", help="跑分（需 API Key）")
    ap.add_argument("--ablation", action="store_true", help="消融实验（需 API Key）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题")
    ap.add_argument("--ids", type=str, default=None, help="只跑指定 id（逗号分隔，便于复跑坏例）")
    ap.add_argument("--category", type=str, default=None, help="只跑某个题型（agg/join/time/trap）")
    ap.add_argument("--kb", action="store_true", help="知识库问答评估（RAG）")
    args = ap.parse_args()

    if args.verify_references:
        res = verify_references()
        s = res["summary"]
        print("参考 SQL 校验: %d/%d 通过" % (s["passed"], s["total"]))
        for cat, v in s["by_category"].items():
            print("  %-10s %d 题, 失败 %d" % (cat, v["total"], v["failed"]))
        for r in res["details"]:
            if r["status"] == "FAIL":
                print("  x %-9s %s" % (r["id"], r["detail"]))
        print("参考结果（行哈希）已写回 eval/cases.yaml")
        return

    if args.kb:
        r = kb_evaluate(limit=args.limit)
        card = kb_scorecard(r)
        print(card)
        (OUT / "scorecard-kb.md").write_text(card, encoding="utf-8")
        print("\nscorecard -> eval/out/scorecard-kb.md")
        return

    if args.score or args.ablation:
        runs = [("baseline", {})]
        if args.ablation:
            runs = [("baseline", {}), ("no-schema-retrieval", {"top_k": 99}), ("no-repair", {"max_repair": 0})]
        cards = []
        for label, kw in runs:
            print("\n=== %s ===" % label)
            r = score(label=label, limit=args.limit, ids=args.ids, category=args.category, **kw)
            cards.append(scorecard(r))
            print(scorecard(r))
        (OUT / "scorecard.md").write_text("\n\n---\n\n".join(cards), encoding="utf-8")
        print("\nscorecard -> eval/out/scorecard.md")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
