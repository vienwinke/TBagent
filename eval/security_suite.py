# -*- coding: utf-8 -*-
"""越权红线用例运行器（**离线**：不连数据库、不需要 API Key）

为什么能离线跑：行级隔离发生在「SQL 重写」这一步 ——
本套用例直接检查 `policy.rewrite()` 的产物，所以可以在 CI 里当门禁用。

用法：
    python -m eval.security_suite          # 跑全部，打印分类表
    python -m eval.security_suite -q       # 只打印汇总
    python -m eval.security_suite --json   # 输出 JSON（给 CI 消费）

退出码：0 = 全部通过；1 = 有失败（CI 直接据此阻断）
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import policy  # noqa: E402
from agent.cache import SqlCache  # noqa: E402
from agent.policy import Principal, PolicyDenied  # noqa: E402

CASES_PATH = Path(__file__).with_name("security_cases.yaml")


@dataclass
class Outcome:
    id: str
    category: str
    ok: bool
    detail: str = ""
    leak: bool = False          # 是否属于"数据泄漏"（比普通失败更严重）


def _principal(spec: dict[str, Any]) -> Principal:
    return Principal(user_id=int(spec.get("user_id", 1)), role=str(spec.get("role", "USER")))


def check_case(case: dict[str, Any]) -> Outcome:
    cid = str(case.get("id", "?"))
    category = str(case.get("category", "未分类"))
    principal = _principal(case.get("principal") or {})
    sql = str(case.get("sql", ""))
    expect = str(case.get("expect", "filtered"))

    # ---- 期望被拒绝 ----
    if expect == "denied":
        want = str(case.get("reason", ""))
        try:
            policy.rewrite(sql, principal)
        except PolicyDenied as exc:
            if want and exc.reason != want:
                return Outcome(cid, category, False,
                               "拒绝原因不符：得到 %s，期望 %s" % (exc.reason, want))
            return Outcome(cid, category, True)
        return Outcome(cid, category, False, "应当被拒绝，但通过了", leak=True)

    # ---- 期望重写成功 ----
    try:
        rw = policy.rewrite(sql, principal)
    except PolicyDenied as exc:
        return Outcome(cid, category, False, "不应拒绝却被拒绝：[%s] %s" % (exc.reason, exc))
    except Exception as exc:  # noqa: BLE001
        return Outcome(cid, category, False, "重写抛异常：%s: %s" % (type(exc).__name__, exc))

    if expect == "allowed":
        if rw.rewritten_tables:
            return Outcome(cid, category, False,
                           "不应做行级重写，却重写了 %s" % ", ".join(rw.rewritten_tables))
        return Outcome(cid, category, True)

    # ---- expect == filtered ----
    missed = policy.unfiltered_refs(rw)
    if missed:
        return Outcome(cid, category, False,
                       "存在未被过滤的引用：%s" % ", ".join(missed), leak=True)

    against = list(case.get("against") or [])
    missing = [t for t in against if t not in rw.rewritten_tables]
    if missing:
        return Outcome(cid, category, False,
                       "预期被重写的表未出现：%s" % ", ".join(missing), leak=True)

    for needle in case.get("assert_contains") or []:
        if str(needle).lower() not in rw.sql.lower():
            return Outcome(cid, category, False, "重写产物缺少 %r" % needle)
    for needle in case.get("assert_absent") or []:
        if str(needle).lower() in rw.sql.lower():
            return Outcome(cid, category, False, "重写产物不应包含 %r" % needle, leak=True)

    return Outcome(cid, category, True)


def builtin_checks() -> list[Outcome]:
    """YAML 表达不了的检查（跨请求状态、类型约束）"""
    out: list[Outcome] = []
    user = Principal(user_id=7)
    admin = Principal(user_id=1, role="ADMIN")
    other_user = Principal(user_id=8)

    # 1) 缓存跨角色隔离（不同角色不得命中彼此条目）
    with tempfile.TemporaryDirectory() as td:
        c = SqlCache(path=Path(td) / "c.json", ttl_sec=600, enabled=True)
        q = "我有几个任务"
        c.put(q, "SELECT 1", scope=policy.cache_scope(user))
        same_user = c.get(q, None, scope=policy.cache_scope(other_user)) is not None
        cross_role = c.get(q, None, scope=policy.cache_scope(admin)) is not None
        unscoped = c.get(q, None) is not None
        out.append(Outcome(
            "builtin-cache-scope", "缓存串号",
            (not cross_role) and (not unscoped),
            "跨角色命中=%s / 未带身份命中=%s" % (cross_role, unscoped),
            leak=cross_role or unscoped,
        ))
        out.append(Outcome(
            "builtin-cache-same-role", "缓存串号", same_user,
            "同角色应共享缓存（缓存值是重写前 SQL，不含用户数据）",
        ))

    # 2) {{ME}} 占位符消解为当前用户
    rw = policy.rewrite("SELECT COUNT(*) FROM settlement WHERE user_id = {{ME}}", user)
    ok = ("{{ME}}" not in rw.sql) and (str(user.user_id) in rw.sql)
    out.append(Outcome("builtin-me-token", "占位符", ok,
                       "改写后仍含 {{ME}} 或缺少 user_id" if not ok else "", leak=not ok))

    # 3) Principal 拒绝非法身份（字符串 user_id 是最常见的越权入口）
    rejected = 0
    for kwargs in ({"user_id": "7"}, {"user_id": 0}, {"user_id": 7, "role": "ROOT"}):
        try:
            Principal(**kwargs)  # type: ignore[arg-type]
        except ValueError:
            rejected += 1
    out.append(Outcome("builtin-principal", "鉴权", rejected == 3,
                       "仅 %d/3 种非法身份被拒绝" % rejected, leak=rejected != 3))

    # 4) 缓存键含策略版本（策略语义变了，旧缓存必须失效）
    key_ok = policy.POLICY_VERSION in policy.cache_scope(user)
    out.append(Outcome("builtin-cache-version", "缓存串号", key_ok,
                       "cache_scope 未包含 POLICY_VERSION" if not key_ok else ""))

    return out


def run(cases_path: Path | None = None, *, include_builtin: bool = True) -> dict[str, Any]:
    data = yaml.safe_load((cases_path or CASES_PATH).read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) and "cases" in data else data

    pairs: list[tuple[dict[str, Any] | None, Outcome]] = [(c, check_case(c)) for c in cases]
    if include_builtin:
        pairs += [(None, o) for o in builtin_checks()]

    outcomes = [o for _, o in pairs]
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for o in outcomes:
        by_cat[o.category][0] += 1
        by_cat[o.category][1] += 1 if o.ok else 0

    deny_pairs = [(c, o) for c, o in pairs if c is not None and c.get("expect") == "denied"]
    deny_hit = sum(1 for _, o in deny_pairs if o.ok)

    return {
        "policy_version": policy.POLICY_VERSION,
        "prompt_version": _prompt_version(),
        "total": len(outcomes),
        "passed": sum(1 for o in outcomes if o.ok),
        "failed": sum(1 for o in outcomes if not o.ok),
        "leaks": sum(1 for o in outcomes if o.leak),
        "deny_total": len(deny_pairs),
        "deny_hit": deny_hit,
        "deny_rate": round(deny_hit / len(deny_pairs), 4) if deny_pairs else 1.0,
        "by_category": {k: {"total": v[0], "passed": v[1]} for k, v in sorted(by_cat.items())},
        "failures": [{"id": o.id, "category": o.category, "detail": o.detail}
                     for o in outcomes if not o.ok],
    }


def _prompt_version() -> str:
    try:
        from agent import prompts_user

        return prompts_user.PROMPT_VERSION
    except Exception:  # noqa: BLE001
        return "?"


def render(result: dict[str, Any], *, quiet: bool = False) -> str:
    lines = []
    if not quiet:
        lines.append("%-14s %6s %6s %6s" % ("类别", "用例", "通过", "失败"))
        for cat, v in result["by_category"].items():
            lines.append("%-14s %6d %6d %6d" % (cat, v["total"], v["passed"], v["total"] - v["passed"]))
        if result["failures"]:
            lines.append("")
            lines.append("失败明细：")
            for f in result["failures"]:
                lines.append("  ✗ %-18s [%s] %s" % (f["id"], f["category"], f["detail"]))
        lines.append("")
    lines.append("合计 %d 条：通过 %d / 失败 %d" % (result["total"], result["passed"], result["failed"]))
    lines.append("越权拦截率 %.1f%%（%d/%d） · 泄漏条数 %d · policy=%s prompt=%s"
                 % (result["deny_rate"] * 100, result["deny_hit"], result["deny_total"],
                    result["leaks"], result["policy_version"], result["prompt_version"]))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="越权红线用例（离线：不需要 DB / API Key）")
    ap.add_argument("--cases", type=Path, default=None, help="用例文件（默认 eval/security_cases.yaml）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    result = run(args.cases)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result, quiet=args.quiet))
    sys.exit(0 if result["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
