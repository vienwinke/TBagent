# -*- coding: utf-8 -*-
"""知识库：切分 + 混合检索（BM25 + 可选向量）+ RRF 融合

设计：
- 语料是 Markdown（业务规则/数据字典/常见问题），按「标题 → 段落 → 句子」递归切分，
  chunk 约 300 字符、重叠 50 字符（重叠保证跨块语义不被割裂）
- 检索默认 BM25（零依赖）；若 .env 设 EMBED_BACKEND=local 则叠加 BGE 向量检索，
  两路结果用 **RRF（Reciprocal Rank Fusion）** 融合 —— 关键词精确匹配与语义召回互补
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config import GUARD, ROOT

KB_DIR = ROOT / "data" / "knowledge"
BGE_MODEL = "BAAI/bge-small-zh-v1.5"


@dataclass
class Chunk:
    id: int
    doc: str          # 来源文件名
    heading: str      # 所属标题
    text: str

    def cite(self) -> str:
        return "%s › %s" % (self.doc, self.heading) if self.heading else self.doc


@dataclass
class Hit:
    chunk: Chunk
    score: float
    source: str = "bm25"      # bm25 / vector / rrf


def split_markdown(text: str, chunk_size: int = 300, overlap: int = 50) -> list[tuple[str, str]]:
    """把 Markdown 切成 (heading, chunk) 列表；先按标题分段，再按长度递归切"""
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
                buf = []
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))

    chunks: list[tuple[str, str]] = []
    for h, body in sections:
        if not body:
            continue
        if len(body) <= chunk_size:
            chunks.append((h, body))
            continue
        # 段落优先，段太长再按句号切
        units = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        cur = ""
        for u in units:
            if len(cur) + len(u) + 1 <= chunk_size:
                cur = (cur + "\n" + u).strip()
            else:
                if cur:
                    chunks.append((h, cur))
                if len(u) <= chunk_size:
                    cur = u
                else:
                    sentences = re.split(r"(?<=[。！？；])", u)
                    cur = ""
                    for s in sentences:
                        if len(cur) + len(s) <= chunk_size:
                            cur += s
                        else:
                            if cur:
                                chunks.append((h, cur))
                            cur = s
        if cur:
            chunks.append((h, cur))
    # 加重叠：把上一块尾部拼到下一块开头
    if overlap > 0:
        out = []
        for i, (h, c) in enumerate(chunks):
            if i and chunks[i - 1][1][-overlap:]:
                out.append((h, (chunks[i - 1][1][-overlap:] + "\n" + c).strip()))
            else:
                out.append((h, c))
        chunks = out
    return chunks


def tokenize(text: str) -> list[str]:
    """中文 bigram + 英文标识符（与 Schema 检索保持一致的轻量分词）"""
    text = text.lower()
    tokens = re.findall(r"[a-z_][a-z0-9_]{1,}", text)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens += [seg[i:i + 2] for i in range(len(seg) - 1)]
        if len(seg) == 1:
            tokens.append(seg)
    return tokens


def rrf_fuse(rankings: Iterable[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """RRF：把多路检索的排名融合成统一分数（对分数量纲不敏感，无需调权重）"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


class KnowledgeBase:
    def __init__(self, kb_dir: Path | None = None, chunk_size: int = 300, overlap: int = 50) -> None:
        self.dir = kb_dir or KB_DIR
        self.chunks: list[Chunk] = []
        for path in sorted(self.dir.glob("*.md")):
            for i, (heading, text) in enumerate(split_markdown(path.read_text(encoding="utf-8"),
                                                              chunk_size, overlap)):
                self.chunks.append(Chunk(len(self.chunks), path.name, heading, text))
        self._bm25 = None
        self._embedder = None
        self._vectors = None
        if not self.chunks:
            raise FileNotFoundError("知识库为空：先运行 python scripts/build_knowledge.py 生成语料")

    # ---------- 检索 ----------
    def _bm25_rank(self, query: str) -> list[int]:
        from rank_bm25 import BM25Okapi

        if self._bm25 is None:
            # 标题是强信号（"任务状态机"必须能命中"状态"类提问），因此重复 3 次加权；
            # 文件名也做轻量加权（"常见问题"类提问）
            self._bm25 = BM25Okapi([tokenize(" ".join([c.heading] * 5 + [c.doc, c.text]))
                                    for c in self.chunks])
        scores = self._bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        return [i for i in order if scores[i] > 0]

    def _vector_rank(self, query: str) -> list[int]:
        try:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(BGE_MODEL)
                self._vectors = self._embedder.encode([c.text for c in self.chunks],
                                                      normalize_embeddings=True)
            import numpy as np

            qv = self._embedder.encode([query], normalize_embeddings=True)
            sims = (self._vectors @ qv.T).ravel()
            return list(np.argsort(-sims))
        except Exception:  # noqa: BLE001  未装 torch/模型时静默退化为纯 BM25
            return []

    def retrieve(self, query: str, top_k: int = 4) -> list[Hit]:
        rankings, sources = [self._bm25_rank(query)], ["bm25"]
        if GUARD.embed_backend == "local":
            vec = self._vector_rank(query)
            if vec:
                rankings.append(vec)
                sources.append("vector")
        fused = rrf_fuse(rankings)
        tag = "rrf" if len(rankings) > 1 else rankings and sources[0] or "bm25"
        return [Hit(self.chunks[i], round(s, 6), tag) for i, s in fused[:top_k]]

    # ---------- 提示词上下文 ----------
    def context(self, hits: list[Hit], max_chars: int = 2400) -> str:
        parts, used = [], 0
        for h in hits:
            block = "【资料 %s】\n%s" % (h.chunk.cite(), h.chunk.text)
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)


def stats() -> dict[str, Any]:
    kb = KnowledgeBase()
    docs = sorted({c.doc for c in kb.chunks})
    return {"chunks": len(kb.chunks), "docs": docs,
            "avg_chunk_len": int(sum(len(c.text) for c in kb.chunks) / len(kb.chunks))}