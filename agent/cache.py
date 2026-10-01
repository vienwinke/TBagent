# -*- coding: utf-8 -*-
"""问题 → SQL 缓存（Cache-Aside）

设计取舍：**缓存 SQL，而不是缓存结果** —— 命中后仍然重新执行 SQL，
所以数据永远是新鲜的（执行只要几毫秒），省掉的是最贵的那一次 LLM 生成。

键 = sha1(归一化问题 + top_k + **权限维度** + 数据库后端 + schema 指纹)，TTL 可配（默认 600s）。
落盘 data/cache/sql_cache.json（已 gitignore），原子写 + 命中计数，便于算命中率。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import CACHE_ENABLED, CACHE_TTL_SEC, ROOT, SCHEMA_PATH, SQL_DIALECT


def _norm(question: str) -> str:
    """问题归一化：去空白/标点差异、全角转半角，提高同类问题的命中率"""
    q = (question or "").strip().lower()
    q = q.replace("？", "?").replace("，", ",").replace("　", " ")
    q = re.sub(r"\s+", "", q)
    return q


def _schema_fingerprint() -> str:
    try:
        st = SCHEMA_PATH.stat()
        return "%s:%d" % (int(st.st_mtime), st.st_size)
    except OSError:
        return "0"


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    puts: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


class SqlCache:
    def __init__(self, path: Path | None = None, ttl_sec: int | None = None,
                 enabled: bool | None = None) -> None:
        self.path = path or (ROOT / "data" / "cache" / "sql_cache.json")
        self.ttl = CACHE_TTL_SEC if ttl_sec is None else ttl_sec
        self.enabled = CACHE_ENABLED if enabled is None else enabled
        self.stats = CacheStats()
        self._data: dict[str, dict[str, Any]] = {}
        if self.enabled:
            self._load()

    # ---------- 存取 ----------
    def _key(self, question: str, top_k: int | None, scope: str = "") -> str:
        # ★ scope = 权限隔离维度（角色 + 策略版本，见 policy.cache_scope）。
        #   缺了它，用户 A 缓存的 SQL 会被用户 B 直接复用 —— 多租户下的越权漏洞。
        raw = "%s|k=%s|%s|%s|%s" % (_norm(question), top_k, scope, SQL_DIALECT, _schema_fingerprint())
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, question: str, top_k: int | None = None, *, scope: str = "") -> dict[str, Any] | None:
        if not self.enabled:
            return None
        item = self._data.get(self._key(question, top_k, scope))
        if not item:
            self.stats.misses += 1
            return None
        if self.ttl > 0 and time.time() - item.get("ts", 0) > self.ttl:
            self.stats.misses += 1
            return None
        item["used"] = item.get("used", 0) + 1
        self.stats.hits += 1
        logger.debug("[cache] 命中 SQL 缓存（省一次生成）：{}", item.get("sql", "")[:60])
        return item

    def put(self, question: str, sql: str, *, tables: list[str] | None = None,
            top_k: int | None = None, scope: str = "") -> None:
        if not self.enabled or not sql:
            return
        self._data[self._key(question, top_k, scope)] = {
            "q": question.strip(), "sql": sql, "tables": tables or [],
            "ts": time.time(), "used": 0,
        }
        self.stats.puts += 1
        self._save()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cache] 读取失败，从空缓存开始：{}", exc)
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)          # 原子替换，避免半个文件
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cache] 写入失败（不影响主流程）：{}", exc)

    def clear(self) -> int:
        n = len(self._data)
        self._data = {}
        self._save()
        return n

    def summary(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "entries": len(self._data), "ttl_sec": self.ttl,
                "hits": self.stats.hits, "misses": self.stats.misses, "puts": self.stats.puts,
                "hit_rate": self.stats.hit_rate}


# 进程级单例（Streamlit 会话间共享）
_CACHE: SqlCache | None = None


def cache() -> SqlCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = SqlCache()
    return _CACHE
