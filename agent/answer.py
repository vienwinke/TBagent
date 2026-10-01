# -*- coding: utf-8 -*-
"""把查询结果转述成一句中文答案（可选步骤，单独成模块以便单独评估）

原则：只依据结果数据，不许编造；结果为空要如实说"没有查到"。
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

import llm as llm_mod

SUMMARY_SYSTEM = """你是数据分析助手。根据【问题】和【查询结果】用**一句中文**回答用户。
要求：
- 只依据给定数据，不要推测或补充数据里没有的信息；
- 结果为空就说"没有查到符合条件的数据"；
- 数字要带单位/口径（如"共 8 个任务"）；
- 不要输出 SQL、不要 Markdown、不要多余解释。"""


def summarize(question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
              *, llm_fn: Callable[[list[dict[str, str]]], str] | None = None,
              max_rows: int = 20, template_first: bool | None = None) -> str:
    """生成一句话答案（llm_fn 可注入，便于单测）

    成本优化：**简单结果直接用确定性模板**，不调 LLM ——
    实测这类结果占多数，省下一次调用的输入 token（约 400~500）与 1~3 秒延迟。
    复杂结果（多行多列）仍走 LLM 转述。
    """
    from config import SUMMARY_TEMPLATE_FIRST

    use_template = SUMMARY_TEMPLATE_FIRST if template_first is None else template_first
    if use_template and _is_simple(columns, rows):
        return _fallback(len(rows), columns, rows)
    call = llm_fn or (lambda messages: llm_mod.chat(messages, temperature=0, max_tokens=256, tag="summary"))
    preview = [dict(zip(columns, r)) for r in list(rows)[:max_rows]]
    payload = "列：%s\n行数：%d\n数据：%s" % (", ".join(columns), len(rows), preview)
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": "【问题】%s\n【查询结果】%s" % (question, payload)},
    ]
    try:
        text = (call(messages) or "").strip()
        if text:
            return text
        return _fallback(len(rows), columns, rows)
    except Exception as exc:  # noqa: BLE001
        return "（结果转述失败：%s）" % type(exc).__name__


def _is_simple(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bool:
    """简单结果判定：单行（任意列数）或单列（任意行数）→ 模板化足以表达"""
    if len(rows) <= 1:
        return True
    return len(columns) == 1


def _fallback(n_rows: int, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """模型没给出文字时的确定性兜底（实测推理模型偶发 content 为空）"""
    if n_rows == 0:
        return "没有查到符合条件的数据。"
    if n_rows == 1 and len(columns) == 1:
        return "查询结果：%s = %s。" % (columns[0], rows[0][0])
    if n_rows == 1:
        pairs = "，".join("%s=%s" % (c, v) for c, v in zip(columns, rows[0]))
        return "查询结果：%s。" % pairs
    return "共返回 %d 行结果（列：%s）。" % (n_rows, "、".join(map(str, columns)))