# -*- coding: utf-8 -*-
"""Schema 检索（M1）：把 15 张表做成可检索索引，只把 Top-K 相关表注入提示词

为什么需要：14 张业务表全塞进 prompt 既贵又容易让模型选错表。
做法：把「表名 + 表描述 + 列名 + 列描述」拼成一篇文档，做检索（默认 BM25）。

后端可切换（.env 的 EMBED_BACKEND）：
  bm25  —— 默认，零额外依赖（rank-bm25），对"表名/字段名精确匹配"友好
  local —— sentence-transformers + BAAI/bge-small-zh-v1.5（语义检索，需 torch）

用法：
  python -m agent.schema_index --build
  python -m agent.schema_index --query "被封禁的用户有多少"
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any

from config import GUARD, INDEX_PATH, SCHEMA_PATH, SYSTEM_TABLES, setup_logging


# 领域别名：把业务口语映射到表（BM25 对"表名/字段名精确匹配"友好，但口语需要额外线索）
TABLE_ALIASES: dict[str, str] = {
    "user": "用户 账号 会员 被封禁 封号 信用分 昵称 注册 微信登录 有多少人 人数",
    "task": "任务 悬赏 发布 赏金 报酬 名额 招人 需求 截止 进行中",
    "task_claim": "接取 领取 报名 参与 接单 订单 接取记录 接取数 新增接取 谁接了 接取量 每天新增",
    "task_submission": "凭证 提交 交付 成果 截图 上传 提交内容",
    "file": "文件 图片 凭证图 头像 大小 存储 上传",
    "notification": "通知 消息 站内信 未读 提醒",
    "review": "评价 互评 评分 打分 好评 差评 星级",
    "report": "举报 投诉 工单 处理 违规 驳回 待处理",
    "settlement": "结算 打款 账目 金额 结清",
    "task_status_log": "任务状态变更 流转 操作留痕 状态历史",
    "claim_status_log": "接取状态变更 流转 操作留痕 状态历史",
    "audit_log": "审计 操作日志 关键操作 风控记录",
    "login_log": "登录记录 登录日志 登录失败 来源IP 风控",
    "app_config": "配置 参数 阈值 开关 设置项",
}

def load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError("缺少 %s：先运行 python -m agent.schema_index --build 前请先抽取 Schema"
                                % SCHEMA_PATH)
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def tokenize(text: str) -> list[str]:
    """中文 bigram + 英文标识符：无第三方分词依赖，对表名/列名精确匹配友好"""
    text = text.lower()
    tokens = re.findall(r"[a-z_][a-z0-9_]{1,}", text)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens += [seg[i:i + 2] for i in range(len(seg) - 1)]
        if len(seg) == 1:
            tokens.append(seg)
    return tokens


def table_document(t: dict[str, Any]) -> str:
    """表名与表描述加权（×3/×2）——检索时"选对表"比"选对列"更关键；再叠加领域别名"""
    name = t["name"]
    parts = [name] * 3 + [t["desc"]] * 2 + [TABLE_ALIASES.get(name, "")] * 2
    for c in t["columns"]:
        parts.append(c["name"])
        parts.append(c.get("desc", ""))
    return " ".join(p for p in parts if p)


@dataclass
class Hit:
    table: str
    score: float


class SchemaIndex:
    def __init__(self, tables: list[dict[str, Any]] | None = None) -> None:
        schema = load_schema()
        self.tables = [t for t in (tables or schema["tables"]) if t["name"] not in SYSTEM_TABLES]
        self._by_name = {t["name"]: t for t in self.tables}
        self._bm25 = None
        self._embedder = None
        self._vectors = None

    # ---------- 构建 ----------
    def build(self) -> dict[str, Any]:
        corpus = [tokenize(table_document(t)) for t in self.tables]
        backend = GUARD.embed_backend
        meta: dict[str, Any] = {"backend": backend, "tables": [t["name"] for t in self.tables]}
        if backend == "local":
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415

                self._embedder = SentenceTransformer("BAAI/bge-small-zh-v1.5")
                docs = [table_document(t) for t in self.tables]
                vecs = self._embedder.encode(docs, normalize_embeddings=True)
                self._vectors = vecs
                INDEX_PATH.write_text(json.dumps({**meta, "docs": docs}, ensure_ascii=False), encoding="utf-8")
                meta["vectors"] = list(map(len, vecs))
            except Exception as exc:  # noqa: BLE001
                meta["fallback"] = "local 后端不可用（%s），已回退 bm25" % type(exc).__name__
                backend = "bm25"
        if backend == "bm25":

            INDEX_PATH.write_text(json.dumps({**meta, "corpus": corpus}, ensure_ascii=False), encoding="utf-8")
        return meta

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int | None = None) -> list[Hit]:
        k = top_k or GUARD.schema_top_k
        if self._embedder is not None and self._vectors is not None:
            import numpy as np  # noqa: PLC0415

            qv = self._embedder.encode([query], normalize_embeddings=True)
            sims = (self._vectors @ qv.T).ravel()
            order = np.argsort(-sims)[:k]
            return [Hit(self.tables[i]["name"], float(sims[i])) for i in order]
        from rank_bm25 import BM25Okapi  # noqa: PLC0415

        corpus = [tokenize(table_document(t)) for t in self.tables]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [Hit(self.tables[i]["name"], round(float(scores[i]), 4)) for i in order]

    # ---------- 提示词片段 ----------
    def describe(self, names: list[str], with_columns: bool = True, *,
                 max_columns: int | None = None, desc_max: int | None = None) -> str:
        """生成给提示词用的 Schema 片段。

        成本优化：列数上限（默认 SCHEMA_MAX_COLUMNS）+ 列描述截断（默认 SCHEMA_DESC_MAX），
        既省 prefill token，又保留"有哪些列"这一关键信息（模型仍知道列存在，只是描述更短）。
        """
        from config import SCHEMA_DESC_MAX, SCHEMA_MAX_COLUMNS

        max_cols = max_columns if max_columns is not None else SCHEMA_MAX_COLUMNS
        dmax = desc_max if desc_max is not None else SCHEMA_DESC_MAX
        blocks = []
        for name in names:
            t = self._by_name.get(name)
            if not t:
                continue
            cols = t["columns"]
            lines = ["表 %s（%s，约 %d 行）" % (t["name"], t["desc"], t["rows"])]
            if with_columns:
                shown = cols if max_cols <= 0 or len(cols) <= max_cols else cols[:max_cols]
                for c in shown:
                    desc = (c.get("desc") or "")
                    if dmax > 0 and len(desc) > dmax:
                        desc = desc[:dmax] + "…"
                    lines.append("  %-22s %-26s %s" % (c["name"], c["type"], desc))
                if len(shown) < len(cols):
                    rest = ", ".join(c["name"] for c in cols[len(shown):])
                    lines.append("  （其余列：%s）" % rest)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Schema 检索索引")
    parser.add_argument("--build", action="store_true", help="构建索引")
    parser.add_argument("--query", type=str, help="检索相关表")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--describe", type=str, help="输出指定表（逗号分隔）的提示词片段")
    args = parser.parse_args()

    idx = SchemaIndex()
    if args.build:
        print("构建完成:", json.dumps(idx.build(), ensure_ascii=False))
    if args.query:
        hits = idx.search(args.query, args.top_k)
        print("查询: %s" % args.query)
        for h in hits:
            print("  %-20s score=%.4f  %s" % (h.table, h.score, idx._by_name[h.table]["desc"][:40]))
        if not hits:
            print("  （无命中）")
    if args.describe:
        print(idx.describe([s.strip() for s in args.describe.split(",")]))


if __name__ == "__main__":
    main()
