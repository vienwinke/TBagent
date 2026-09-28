# -*- coding: utf-8 -*-
"""知识库问答（RAG）：检索 → 约束式提示词 → 带引用的回答

关键约束（防幻觉）：
1. 只依据检索到的资料回答；资料不足必须**如实说明**（insufficient=true）
2. 返回 JSON：answer / used_sources（资料编号）/ insufficient —— 便于自动评估"引用是否落地""是否正确拒答"
3. 引用来源随回答一起返回，界面可展开核查
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import llm as llm_mod
from agent.kb import Hit, KnowledgeBase

RAG_SYSTEM = """你是 treatbord 任务接取平台的业务助手。
请**仅根据提供的资料**回答问题，遵守：
- 资料足够：给出准确、简洁的中文回答（可分点），不要编造资料里没有的规则或数字；
- 资料不足：如实说明"资料中没有相关内容"，并把 insufficient 设为 true；
- 引用资料编号：在 used_sources 里列出你实际依据的资料编号（如 [1,3]）。
只输出 JSON：{"answer": "...", "used_sources": [1,2], "insufficient": false}"""

LlmFn = Callable[[list[dict[str, str]]], dict[str, Any]]


@dataclass
class RagResult:
    question: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    insufficient: bool = False
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer)

    def summary(self) -> dict[str, Any]:
        return {"question": self.question, "answer": self.answer, "citations": self.citations,
                "insufficient": self.insufficient, "hits": self.hits, "error": self.error}


def _default_llm_fn(messages: list[dict[str, str]]) -> dict[str, Any]:
    return llm_mod.chat_json(messages, temperature=0, max_tokens=800, tag="rag")


def answer(question: str, *, top_k: int | None = None, llm_fn: LlmFn | None = None,
           kb: KnowledgeBase | None = None) -> RagResult:
    call = llm_fn or _default_llm_fn
    res = RagResult(question=question)
    usage_before = llm_mod.USAGE.summary()

    try:
        kb = kb or KnowledgeBase()
        hits: list[Hit] = kb.retrieve(question, top_k or 4)
    except Exception as exc:  # noqa: BLE001
        res.error = "知识库不可用: %s" % exc
        return res

    res.hits = [{"doc": h.chunk.doc, "heading": h.chunk.heading, "score": h.score,
                 "source": h.source, "text": h.chunk.text[:160]} for h in hits]
    if not hits:
        res.answer = "资料中没有与这个问题相关的内容。"
        res.insufficient = True
        return res

    context = kb.context(hits) if kb else ""
    payload = "%s\n\n【问题】%s" % (context, question)
    try:
        raw = call([{"role": "system", "content": RAG_SYSTEM},
                    {"role": "user", "content": payload}])
        if isinstance(raw, dict):
            res.answer = str(raw.get("answer", "")).strip()
            res.insufficient = bool(raw.get("insufficient", False))
            used = raw.get("used_sources") or []
            idxs = [int(x) for x in used if str(x).isdigit() or isinstance(x, int)]
            res.citations = [hits[i - 1].chunk.cite() for i in idxs if 1 <= i <= len(hits)] or \
                            [h.cite() for h in hits[:2]]
        else:
            res.answer = str(raw).strip()
            res.citations = [h.cite() for h in hits[:2]]
    except Exception as exc:  # noqa: BLE001
        res.error = "生成答案失败: %s: %s" % (type(exc).__name__, str(exc)[:120])

    after = llm_mod.USAGE.summary()
    res.usage = {k: after[k] - usage_before.get(k, 0)
                 for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")}
    return res