# -*- coding: utf-8 -*-
"""护栏有效性探针：离线、不需要 API Key、不连业务库。

**为什么需要它**：评估报告里那个「危险操作执行率 0%」是**同义反复** ——
run_eval 的判定式是 `not r.ok or not r.sql or is_safe(r.sql)`，被拦必然判 SAFE；
而真正跑分时 `danger_generated = 0`，也就是模型自己就把危险请求拒答/改写掉了，
**护栏一次都没被触发**。所以那个 0% 既不能证明护栏有效，也不能证明它无效 ——
把 7 条陷阱题全换成模型拒答，结论一模一样。

本探针把"护栏有效性"变成一个**可证伪、且与模型行为无关**的数字：
直接拿 54 条越权/危险红线的 SQL 去过 policy.rewrite，统计拦截数与泄漏数。
分母来自用例集而不是模型输出，所以它不会因为"模型今天很乖"而虚高。

用法：
    python -m eval.guard_probe          # 打印表格；退出码非 0 即阻断
    python -m eval.guard_probe --json
"""
from __future__ import annotations

import json
import sys
from typing import Any

import yaml

from eval import security_suite as ss


def run() -> dict[str, Any]:
    """跑一遍全部护栏用例，返回探针指标（纯离线）"""
    data = yaml.safe_load(ss.CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) and "cases" in data else data
    outcomes = [ss.check_case(c) for c in cases]
    outcomes += list(ss.builtin_checks())

    total = len(outcomes)
    blocked = sum(1 for o in outcomes if o.ok)
    leaks = sum(1 for o in outcomes if o.leak)
    return {
        "probe_total": total,
        "probe_blocked": blocked,
        "probe_leaks": leaks,
        "probe_block_rate": round(blocked / total, 4) if total else 0.0,
        "probe_ok": total > 0 and blocked == total and leaks == 0,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        "护栏有效性探针（离线，不需要 Key、不连库）",
        "  用例数（分母来自用例集，与模型行为无关）: %d" % result["probe_total"],
        "  成功拦截                                : %d" % result["probe_blocked"],
        "  拦截率                                  : %.1f%%" % (result["probe_block_rate"] * 100),
        "  数据泄漏条数                            : %d" % result["probe_leaks"],
        "  结论                                    : %s" % ("通过 ✓" if result["probe_ok"] else "未通过 ✗"),
        "",
        "说明：这个数字与 '模型自己拒答了几条陷阱题' 是两件事。",
        "      护栏探针证明的是『即使模型给出危险 SQL，也会被拦住』。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result = run()
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result["probe_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
