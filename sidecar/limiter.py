# -*- coding: utf-8 -*-
"""并发闸门 / 同 session 串行 / client_msg_id 幂等 —— 两种后端。

为什么要有两种后端（契约 §0 的"并发/幂等"条款）：
· **进程内**（`MemoryLimiter`，默认）：单副本部署语义正确、零依赖；
· **Redis**（`RedisLimiter`，`SIDECAR_REDIS_URL` 打开）：多副本部署下配额、幂等、会话锁
  必须**跨进程共享** —— 进程内实现会让每个副本各自放宽一份配额（等于配额 × 副本数），
  幂等也会各写各的（重发落到另一个副本就重复计费）。

实现约束（都是 Redis 上做锁/计数时最容易踩的坑）：
· 计数类键一律带 TTL：进程被 kill 时不会留下永远减不掉的"幽灵占用"；
· 会话锁的释放用 **Lua 比对 token**，避免"超时释放了别人的锁"；
· Redis 不可用时**降级为进程内实现并告警**，而不是让整个边车不可用（可用性优先）。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Protocol


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


class Limiter(Protocol):
    """两种后端共同的接口（app.py 只依赖这个形状）"""

    def try_acquire(self) -> bool: ...
    def release(self) -> None: ...
    def acquire_session(self, session_id: str) -> str: ...
    def release_session(self, session_id: str, token: str) -> None: ...
    def replay(self, key: str) -> str | None: ...
    def remember(self, key: str, frames: str) -> None: ...
    def reset(self) -> None: ...
    def backend(self) -> str: ...


class MemoryLimiter:
    """进程内实现（单副本）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sem: threading.BoundedSemaphore | None = None
        self._sem_size = 0
        self._session_locks: dict[str, threading.Lock] = {}
        self._seen: dict[str, tuple[float, str]] = {}

    def backend(self) -> str:
        return "memory"

    def _slots(self) -> threading.BoundedSemaphore:
        size = max(1, _env_int("SIDECAR_MAX_CONCURRENCY", 4))
        with self._lock:
            if self._sem is None or self._sem_size != size:
                self._sem = threading.BoundedSemaphore(size)
                self._sem_size = size
            return self._sem

    def try_acquire(self) -> bool:
        """非阻塞占坑：满了就让调用方直接 429，而不是把请求堆在队列里"""
        return self._slots().acquire(blocking=False)

    def release(self) -> None:
        try:
            self._slots().release()
        except ValueError:
            pass

    def acquire_session(self, session_id: str) -> str:
        """契约 §0：同 session 串行（进程内 = 一把互斥锁，带等待上限）

        拿不到就抛 TimeoutError —— 由端点转成 **429**，而不是让请求无限排队。
        """
        with self._lock:
            lock = self._session_locks.setdefault(session_id, threading.Lock())
        wait_ms = max(0, _env_int("SIDECAR_SESSION_LOCK_WAIT_MS", 5000))
        if not lock.acquire(timeout=wait_ms / 1000.0):
            raise TimeoutError("同 session 串行等待超时：%s" % session_id)
        return session_id

    def release_session(self, session_id: str, token: str) -> None:
        with self._lock:
            lock = self._session_locks.get(session_id)
        if lock is not None and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    def replay(self, key: str) -> str | None:
        if not key:
            return None
        with self._lock:
            item = self._seen.get(key)
            if not item:
                return None
            expires_at, frames = item
            if time.monotonic() > expires_at:
                self._seen.pop(key, None)
                return None
            return frames

    def remember(self, key: str, frames: str) -> None:
        if not key:
            return
        ttl = max(1, _env_int("SIDECAR_IDEMPOTENCY_TTL_SEC", 600))
        with self._lock:
            if len(self._seen) > 1000:          # 粗略上限，避免无界增长
                self._seen.clear()
            self._seen[key] = (time.monotonic() + ttl, frames)

    def reset(self) -> None:
        with self._lock:
            self._session_locks.clear()
            self._seen.clear()
            self._sem = None
            self._sem_size = 0


class RedisLimiter:
    """Redis 实现（多副本共享）"""

    _UNLOCK_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                   "return redis.call('del', KEYS[1]) else return 0 end")

    def __init__(self, url: str, *, prefix: str = "tb:ai:") -> None:
        import redis  # 延迟导入：不用 Redis 的部署不必装它

        self._redis = redis.Redis.from_url(url, decode_responses=True,
                                           socket_connect_timeout=2, socket_timeout=2)
        self._prefix = prefix
        self._inflight = self._prefix + "inflight"
        self._lock_prefix = self._prefix + "session:"
        self._idem_prefix = self._prefix + "idem:"

    def backend(self) -> str:
        return "redis"

    # ---------------------------------------------------------------- 并发闸门
    def try_acquire(self) -> bool:
        limit = max(1, _env_int("SIDECAR_MAX_CONCURRENCY", 4))
        current = self._redis.incr(self._inflight)
        self._redis.expire(self._inflight, 60)      # 兜底 TTL：进程被 kill 不留幽灵占用
        if current > limit:
            self._redis.decr(self._inflight)
            return False
        return True

    def release(self) -> None:
        try:
            if self._redis.decr(self._inflight) < 0:
                self._redis.set(self._inflight, 0)
        except Exception:  # noqa: BLE001  Redis 抖动不该把请求搞崩
            pass

    # ---------------------------------------------------------------- 会话串行
    def _lock_key(self, session_id: str) -> str:
        return self._lock_prefix + session_id

    def acquire_session(self, session_id: str) -> str:
        """Redis 会话锁：SETNX + token；拿不到等一会儿再抛 TimeoutError（端点转 429）"""
        token = uuid.uuid4().hex
        key = self._lock_key(session_id)
        wait_ms = max(0, _env_int("SIDECAR_SESSION_LOCK_WAIT_MS", 5000))
        deadline = time.monotonic() + wait_ms / 1000.0
        while True:
            if self._redis.set(key, token, nx=True, px=max(wait_ms + 2000, 5000)):
                return token
            if time.monotonic() >= deadline:
                raise TimeoutError("同 session 串行等待超时：%s" % session_id)
            time.sleep(0.02)

    def release_session(self, session_id: str, token: str) -> None:
        """Lua 比对 token 再删：避免超时后释放掉了别人的锁"""
        try:
            self._redis.eval(self._UNLOCK_LUA, 1, self._lock_key(session_id), token)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 幂等
    def _idem_key(self, key: str) -> str:
        return self._idem_prefix + key

    def replay(self, key: str) -> str | None:
        if not key:
            return None
        return self._redis.get(self._idem_key(key))

    def remember(self, key: str, frames: str) -> None:
        if not key:
            return
        ttl = max(1, _env_int("SIDECAR_IDEMPOTENCY_TTL_SEC", 600))
        self._redis.set(self._idem_key(key), frames, ex=ttl)

    def reset(self) -> None:
        for pattern in (self._prefix + "*",):
            for k in self._redis.scan_iter(match=pattern, count=200):
                self._redis.delete(k)


def make_limiter() -> Limiter:
    """按 `SIDECAR_REDIS_URL` 选择后端；Redis 不可用时降级为进程内并告警。"""
    url = _env("SIDECAR_REDIS_URL")
    if not url:
        return MemoryLimiter()
    try:
        limiter = RedisLimiter(url)
        limiter._redis.ping()
        return limiter
    except Exception as exc:  # noqa: BLE001
        from loguru import logger

        logger.warning("[sidecar] Redis 不可用（{}: {}），降级为进程内限流；"
                       "多副本部署下配额与幂等将不共享", type(exc).__name__, str(exc)[:120])
        return MemoryLimiter()
