# -*- coding: utf-8 -*-
"""提示词版本灰度（ai_prompt_version.name / version / content / enabled / traffic_pct）。

这张表从 V9 起就在 DDL 里，但**代码零引用** —— 也就是"灰度发布"写进了表结构、
却从未实现。本模块把它接上：

    · 按 key（当前用 user_id）做**确定性**分桶 → 同一用户永远命中同一版本；
    · 启用行按 (name, version) 排序后按 traffic_pct **累积分桶**：
      例如 v2=10、v3=20，则桶 0-9 走 v2、10-29 走 v3、其余回落内置版本；
    · 任何异常都回落内置 PROMPT_VERSION —— 灰度配置不该成为问答链路的单点。

三个刻意的取舍：
    1. 分桶**必须确定性**（不能用随机数）：否则同一用户的 A/B 组会在会话之间跳，
       指标就不可归因。
    2. 读表是 best-effort：拿不到就用内置版本 + 记日志，绝不抛。
    3. 缓存 30s：避免"问一句查一次库"（Java 侧 AppConfigService 是同样的做法）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger

from agent import prompts_user

CACHE_TTL_SEC = 30
_cache: tuple[float, list[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class ActivePrompt:
    """当前请求命中的提示词版本。source=builtin 表示回落内置版本。"""

    version: str
    content: str | None = None
    source: str = "builtin"


def bucket_of(key: str) -> int:
    """把任意 key 稳定映射到 0..99 的桶。同一 key 永远同一结果。"""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) % 100


def select_version(rows: Iterable[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """按 traffic_pct 累积分桶选版本；没有命中就返回 None（调用方回落内置）。

    纯函数：不碰数据库、不看时间，便于单测与复现。
    """
    bucket = bucket_of(key)
    cursor = 0
    for row in sorted(rows, key=lambda r: (str(r.get("name")), str(r.get("version")))):
        # 缺失 enabled 视作启用（调用方通常已按 enabled=1 过滤过）
        if "enabled" in row and not row["enabled"]:
            continue
        try:
            pct = int(row.get("traffic_pct") or 0)
        except (TypeError, ValueError):
            continue
        pct = max(0, min(100, pct))
        if bucket < cursor + pct:
            return row
        cursor += pct
        if cursor >= 100:
            break
    return None


def load_versions(*, force: bool = False) -> list[dict[str, Any]]:
    """读启用的提示词版本（best-effort，失败返回空列表）。

    走 executor.connect() 而不是另开一条连接：全仓的连接工厂只该有一个。
    """
    global _cache
    now = time.time()
    if not force and _cache is not None and _cache[0] > now:
        return _cache[1]
    rows: list[dict[str, Any]] = []
    try:
        from contextlib import closing

        from agent import executor

        sql = ("SELECT name, version, content, enabled, traffic_pct FROM ai_prompt_version "
               "WHERE enabled = 1")
        with closing(executor.connect()) as conn:
            with executor._CursorCtx(conn) as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    row = dict(zip(cols, r))
                    row["enabled"] = bool(row.get("enabled"))
                    rows.append(row)
    except Exception as e:                      # noqa: BLE001
        # 灰度配置拿不到不该影响问答：回落内置版本并留一条日志
        logger.debug("[prompt-registry] 读取 ai_prompt_version 失败，回落内置版本：{}",
                     str(e)[:120])
        rows = []
    _cache = (now + CACHE_TTL_SEC, rows)
    return rows


def resolve(key: str, *, rows: list[dict[str, Any]] | None = None) -> ActivePrompt:
    """解析该 key 命中的提示词版本；未命中/无配置 → 内置版本。"""
    if rows is None:
        rows = load_versions()
    chosen = select_version(rows, key)
    if chosen is None:
        return ActivePrompt(version=prompts_user.PROMPT_VERSION, source="builtin")
    content = (chosen.get("content") or "").strip() or None
    return ActivePrompt(version="%s/%s" % (chosen.get("name"), chosen.get("version")),
                        content=content, source="db")


def invalidate() -> None:
    """清缓存（改完灰度配置想让改动立即生效时调用）"""
    global _cache
    _cache = None
