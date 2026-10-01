# -*- coding: utf-8 -*-
"""jti 黑名单：让"降权/登出"能**即时生效**（契约 §2.2）。

为什么需要：内部 JWT 的有效期是 5 分钟。管理员被降权或用户登出后，**旧 token 最多还能用 5 分钟** ——
契约里写明了这一点，并给出对策："要即时生效就把 `jti` 放 Redis 黑名单"。

分工（重要）：
· **写入方是 treatbord（Java）**：它在降权/登出/改密时把 `jti` 放进 Redis（`SET tb:ai:jti:<jti> 1 EX <ttl>`）；
· **本模块只负责校验**：边车每次校验 JWT 时问一句"这个 jti 被吊销了吗"。
这样边车不需要额外的管理接口，也不需要知道"为什么被吊销"。

可用性取舍：Redis 抖动时**放行**（fail-open）而不是拒绝所有请求 ——
黑名单是"缩短已知泄露窗口"的加固措施，不该变成整站不可用的新单点。
真要 fail-closed 的部署，把 `SIDECAR_DENYLIST_FAIL_CLOSED=true` 打开。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Protocol

PREFIX = "tb:ai:jti:"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


class Denylist(Protocol):
    def is_revoked(self, jti: str | None) -> bool: ...
    def revoke(self, jti: str, ttl_sec: int) -> None: ...          # 测试/未来管理接口用
    def backend(self) -> str: ...


class MemoryDenylist:
    """进程内实现（单副本；测试也用它）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[str, float] = {}

    def backend(self) -> str:
        return "memory"

    def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        with self._lock:
            expires = self._revoked.get(jti)
            if expires is None:
                return False
            if time.monotonic() > expires:
                self._revoked.pop(jti, None)
                return False
            return True

    def revoke(self, jti: str, ttl_sec: int) -> None:
        with self._lock:
            self._revoked[jti] = time.monotonic() + max(1, ttl_sec)

    def reset(self) -> None:
        with self._lock:
            self._revoked.clear()


class RedisDenylist:
    """Redis 实现（多副本共享；**写入方是 treatbord**）"""

    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True,
                                           socket_connect_timeout=2, socket_timeout=2)

    def backend(self) -> str:
        return "redis"

    def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        try:
            return bool(self._redis.exists(PREFIX + jti))
        except Exception as exc:  # noqa: BLE001
            from loguru import logger

            if _env("SIDECAR_DENYLIST_FAIL_CLOSED", "false").lower() in ("1", "true", "yes"):
                logger.error("[auth] 黑名单查询失败且已配置 fail-closed，拒绝该 token：{}", exc)
                return True
            logger.warning("[auth] 黑名单查询失败，放行（fail-open）：{}: {}", type(exc).__name__, exc)
            return False

    def revoke(self, jti: str, ttl_sec: int) -> None:
        self._redis.set(PREFIX + jti, 1, ex=max(1, ttl_sec))

    def reset(self) -> None:
        for key in self._redis.scan_iter(match=PREFIX + "*", count=200):
            self._redis.delete(key)


def make_denylist() -> Denylist:
    """按 `SIDECAR_REDIS_URL` 选择；Redis 不可用则退回进程内（并告警）"""
    url = _env("SIDECAR_REDIS_URL")
    if not url:
        return MemoryDenylist()
    try:
        denylist = RedisDenylist(url)
        denylist._redis.ping()
        return denylist
    except Exception as exc:  # noqa: BLE001
        from loguru import logger

        logger.warning("[auth] Redis 不可用（{}: {}），jti 黑名单退回进程内实现",
                       type(exc).__name__, str(exc)[:120])
        return MemoryDenylist()
