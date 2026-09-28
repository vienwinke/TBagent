# -*- coding: utf-8 -*-
"""结果自动选图：根据结果集的"形状"决定可视化方式（只有规则，不依赖绘图库，便于单测）

为什么不用 matplotlib 直出 PNG：图表在 Streamlit 里由浏览器渲染（Altair/Vega-Lite），
中文由用户浏览器字体负责，**避开了服务端缺中文字体导致方块字**的问题。
本模块只产出 ChartSpec，渲染交给 app.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

KIND_METRIC = "metric"   # 单个数值（大数字卡片）
KIND_LINE = "line"       # 时间序列
KIND_BAR = "bar"         # 类别对比（竖柱）
KIND_BARH = "barh"       # 类别多/名字长（横条）
KIND_PIE = "pie"         # 占比（类别少且和为 100）
KIND_TABLE = "table"     # 不适合画图（多列或多行明细）

TIME_PATTERN = re.compile(
    r"(date|time|day|hour|month|year|日期|时间|天|小时|月份|年份|周)", re.I)


@dataclass
class ChartSpec:
    kind: str
    x: str | None = None
    y: str | None = None
    title: str = ""
    reason: str = ""          # 为什么这么选（界面上可展示，便于讲清逻辑）

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "x": self.x, "y": self.y, "title": self.title, "reason": self.reason}


def _is_number(v: Any) -> bool:
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return True
        except ValueError:
            return False
    return False


def _is_time_like(v: Any) -> bool:
    if isinstance(v, (datetime, date)):
        return True
    s = str(v)
    return bool(re.match(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:?\d{2})?", s)) or bool(re.match(r"^\d{1,2}$", s) and False)


def _column_kinds(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """判断每列是 'time' / 'num' / 'cat'"""
    kinds = []
    for i, _ in enumerate(columns):
        values = [r[i] for r in rows if r[i] is not None]
        if values and all(_is_number(v) for v in values):
            # 纯数字的"小时/月份"列按时间处理更合理（如 HOUR(create_time)）
            kinds.append("num")
        elif values and all(_is_time_like(v) for v in values):
            kinds.append("time")
        else:
            kinds.append("cat")
    # 列名暗示时间（小时/月份/日期）且是数值 → 修正为 time
    for i, name in enumerate(columns):
        if kinds[i] == "num" and TIME_PATTERN.search(name or ""):
            kinds[i] = "time"
    return kinds


def choose_spec(columns: Sequence[str], rows: Sequence[Sequence[Any]], *, max_bar: int = 12,
                masked_columns: Sequence[str] = (), max_cols: int = 4) -> ChartSpec:
    """按结果形状选图；无法可视化时返回 KIND_TABLE

    masked_columns：结果里含被脱敏的列时不画图（画图无意义，且避免把脱敏列当维度展示）
    max_cols：列数超过该值视为"明细导出"，直接展示表格（实测 SELECT * 13 列被误判为折线图）
    """
    cols = list(columns)
    n_rows, n_cols = len(rows), len(cols)

    # 单值：大数字卡片
    if n_rows == 1 and n_cols == 1:
        return ChartSpec(KIND_METRIC, title=str(cols[0]), reason="单行单列 → 大数字卡片")

    if masked_columns:
        return ChartSpec(KIND_TABLE, reason="结果含脱敏列 → 直接展示表格")
    if n_cols > max_cols:
        return ChartSpec(KIND_TABLE, reason="列数 %d 超过 %d，视为明细导出 → 展示表格" % (n_cols, max_cols))
    if n_rows < 2 or n_cols < 2:
        return ChartSpec(KIND_TABLE, reason="行或列不足两维 → 直接展示表格")

    kinds = _column_kinds(cols, rows)
    num_idx = [i for i, k in enumerate(kinds) if k == "num"]
    time_idx = [i for i, k in enumerate(kinds) if k == "time"]
    cat_idx = [i for i, k in enumerate(kinds) if k == "cat"]

    # 时间 + 数值 → 折线
    if time_idx and num_idx:
        x, y = cols[time_idx[0]], cols[num_idx[0]]
        return ChartSpec(KIND_LINE, x=x, y=y, title="%s 随 %s 变化" % (y, x),
                         reason="检测到时间列 + 数值列 → 折线图")

    # 类别 + 数值
    if cat_idx and num_idx:
        x, y = cols[cat_idx[0]], cols[num_idx[0]]
        # 占比：类别少且合计≈100
        if n_cols == 2 and 2 <= n_rows <= 6:
            try:
                total = sum(float(r[num_idx[0]]) for r in rows)
                if 95 <= total <= 105:
                    return ChartSpec(KIND_PIE, x=x, y=y, title=y,
                                     reason="类别≤6 且合计≈100 → 饼图（占比）")
            except (TypeError, ValueError):
                pass
        if n_rows > max_bar or max(len(str(v)) for v in (r[cat_idx[0]] for r in rows)) > 10:
            return ChartSpec(KIND_BARH, x=x, y=y, title="%s（按 %s）" % (y, x),
                             reason="类别较多或名称较长 → 横向条形图")
        return ChartSpec(KIND_BAR, x=x, y=y, title="%s（按 %s）" % (y, x),
                         reason="类别列 + 数值列 → 柱状图")

    return ChartSpec(KIND_TABLE, reason="没有合适的数值/类别组合 → 展示表格")