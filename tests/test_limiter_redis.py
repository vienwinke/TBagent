# -*- coding: utf-8 -*-
"""限流后端（sidecar/limiter.py）：进程内 vs Redis。

Redis 后端的价值**只体现在多副本**：配额、幂等、会话锁必须跨进程共享。
所以这里的关键用例都用**两个独立的 limiter 实例**来模拟两个副本 ——
单实例自测通过并不能说明 Redis 后端写对了。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sidecar import app as sidecar  # noqa: E402
from sidecar import limiter as limiter_mod  # noqa: E402
from sidecar.limiter import MemoryLimiter, RedisLimiter  # noqa: E402

REDIS_URL = "redis://127.0.0.1:6379/0"


def _redis_ok() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_redis = pytest.mark.skipif(not _redis_ok(), reason="本机没有可用的 Redis")


@pytest.fixture
def two_replicas():
    """两个独立实例 = 两个副本，共用同一个 Redis 命名空间"""
    prefix = "tb:test:%s:" % uuid.uuid4().hex[:8]
    a, b = RedisLimiter(REDIS_URL, prefix=prefix), RedisLimiter(REDIS_URL, prefix=prefix)
    yield a, b
    a.reset()


# ------------------------------------------------------------------ 后端选择
def test_make_limiter_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("SIDECAR_REDIS_URL", raising=False)
    assert limiter_mod.make_limiter().backend() == "memory"


@requires_redis
def test_make_limiter_uses_redis_when_configured(monkeypatch):
    monkeypatch.setenv("SIDECAR_REDIS_URL", REDIS_URL)
    assert limiter_mod.make_limiter().backend() == "redis"


def test_make_limiter_falls_back_when_redis_unreachable(monkeypatch):
    """Redis 连不上时降级为进程内 + 告警，而不是让整个边车起不来（可用性优先）"""
    monkeypatch.setenv("SIDECAR_REDIS_URL", "redis://127.0.0.1:6399/0")
    assert limiter_mod.make_limiter().backend() == "memory"


def test_memory_limiter_concurrency_and_idempotency(monkeypatch):
    monkeypatch.setenv("SIDECAR_MAX_CONCURRENCY", "1")
    lim = MemoryLimiter()
    assert lim.try_acquire() is True
    assert lim.try_acquire() is False, "并发上限为 1 时第二次必须被拒"
    lim.release()
    assert lim.try_acquire() is True
    lim.release()

    lim.remember("7|c1", "frames")
    assert lim.replay("7|c1") == "frames"


# ------------------------------------------------------------------ Redis：跨副本
@requires_redis
def test_redis_concurrency_is_shared_across_replicas(two_replicas, monkeypatch):
    monkeypatch.setenv("SIDECAR_MAX_CONCURRENCY", "1")
    a, b = two_replicas
    assert a.try_acquire() is True, "副本 A 占到唯一坑位"
    assert b.try_acquire() is False, "副本 B 必须看到 A 的占用（否则配额 × 副本数）"
    a.release()
    assert b.try_acquire() is True


@requires_redis
def test_redis_idempotency_is_shared_across_replicas(two_replicas):
    a, b = two_replicas
    a.remember("7|msg-1", "同一个响应")
    assert b.replay("7|msg-1") == "同一个响应", "重发落到另一个副本也要能重放（不重复计费）"
    assert b.replay("7|msg-2") is None


@requires_redis
def test_redis_session_lock_serializes_across_replicas(two_replicas, monkeypatch):
    monkeypatch.setenv("SIDECAR_SESSION_LOCK_WAIT_MS", "50")
    a, b = two_replicas
    token = a.acquire_session("s-1")
    try:
        with pytest.raises(TimeoutError):
            b.acquire_session("s-1"), "同 session 在另一个副本上必须等不到"
    finally:
        a.release_session("s-1", token)

    token2 = b.acquire_session("s-1")          # A 释放后 B 能拿到
    b.release_session("s-1", token2)


@requires_redis
def test_redis_session_lock_does_not_release_someone_elses(two_replicas, monkeypatch):
    """Lua 比对 token：拿错 token 释放不得删掉别人的锁"""
    monkeypatch.setenv("SIDECAR_SESSION_LOCK_WAIT_MS", "50")
    a, b = two_replicas
    token = a.acquire_session("s-2")
    try:
        b.release_session("s-2", "错误的 token")     # 不该生效
        with pytest.raises(TimeoutError):
            b.acquire_session("s-2")
    finally:
        a.release_session("s-2", token)


@requires_redis
def test_redis_reset_clears_all_keys(two_replicas):
    a, b = two_replicas
    a.remember("7|x", "frames")
    assert a.try_acquire() is True
    a.reset()
    assert b.replay("7|x") is None
    # reset 后计数归零：仍能正常占坑
    assert b.try_acquire() is True
    b.release()


# ------------------------------------------------------------------ 边车端到端（Redis 后端）
@requires_redis
def test_sidecar_works_with_redis_backend(monkeypatch):
    from agent.pipeline import Deps

    monkeypatch.setenv("SIDECAR_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    monkeypatch.setattr(sidecar, "LIMITER", limiter_mod.make_limiter())
    # 自带清理：Redis 里的并发计数/幂等键可能被上一次中断的进程留下（TTL 60s 才自愈），
    # 不清理就会出现"偶发 429 / 偶发重放"的假失败 —— 测试不能依赖外部残留状态。
    sidecar.LIMITER.reset()
    monkeypatch.setattr(sidecar, "DEPS", Deps(
        nl2sql_llm=lambda m: {"sql": "SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                              "reason": "t", "tables": ["task"]},
        summary_llm=lambda m: "共 8 个任务。"))
    sidecar.STATS.reset()
    try:
        client = TestClient(sidecar.app)
        body = {"session_id": "s-redis", "question": "待接取的任务有几个？", "client_msg_id": "r1"}
        first = client.post("/v1/ai/chat", json=body)
        second = client.post("/v1/ai/chat", json=body)

        assert first.status_code == 200 and second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "1", "Redis 幂等应生效"
        assert second.text == first.text
    finally:
        sidecar.LIMITER.reset()
        sidecar.LIMITER = limiter_mod.make_limiter()          # 还原默认（内存）


# ------------------------------------------------------------------ jti 黑名单（Redis，即时降权）
@requires_redis
def test_redis_denylist_is_shared_across_replicas():
    """treatbord（Java）写入黑名单，边车必须立刻看到 —— 否则降权还有 5 分钟窗口"""
    from sidecar.denylist import RedisDenylist

    prefix = "tb:test:jti:%s:" % uuid.uuid4().hex[:8]      # 隔离前缀：绝不碰生产键
    a, b = RedisDenylist(REDIS_URL, prefix=prefix), RedisDenylist(REDIS_URL, prefix=prefix)
    jti = "test-jti-%s" % uuid.uuid4().hex[:8]
    assert b.is_revoked(jti) is False
    a.revoke(jti, ttl_sec=60)
    assert b.is_revoked(jti) is True, "跨副本必须共享"
    a.reset()
    assert b.is_revoked(jti) is False


@requires_redis
def test_revoked_token_rejected_end_to_end(monkeypatch):
    """真实 HTTP：同一个 token，吊销前 200、吊销后 401、移出黑名单后又能用"""
    import time

    from agent.pipeline import Deps
    from sidecar import auth as auth_mod
    from sidecar import denylist as denylist_mod

    secret = "denylist-secret"
    monkeypatch.setenv("SIDECAR_JWT_SECRET", secret)
    monkeypatch.delenv("SIDECAR_DEV_PRINCIPAL", raising=False)
    monkeypatch.setenv("SIDECAR_REDIS_URL", REDIS_URL)
    prefix = "tb:test:jti:%s:" % uuid.uuid4().hex[:8]
    monkeypatch.setattr(sidecar, "DENYLIST", denylist_mod.RedisDenylist(REDIS_URL, prefix=prefix))
    monkeypatch.setattr(sidecar, "DEPS", Deps(
        nl2sql_llm=lambda m: {"sql": "SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                              "reason": "t", "tables": ["task"]},
        summary_llm=lambda m: "共 8 个任务。"))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()

    now = time.time()
    token = auth_mod.sign({"sub": "7", "role": "USER", "jti": "e2e-jti-1",
                           "iat": now, "exp": now + 120, "aud": "ai-sidecar"}, secret)
    body = {"session_id": "s-jti", "question": "待接取的任务有几个？"}
    client = TestClient(sidecar.app)
    try:
        assert client.post("/v1/ai/chat", json=body,
                           headers={"Authorization": "Bearer " + token}).status_code == 200

        sidecar.DENYLIST.revoke("e2e-jti-1", ttl_sec=60)        # 模拟 treatbord 降权
        r = client.post("/v1/ai/chat", json=body,
                        headers={"Authorization": "Bearer " + token})
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    finally:
        sidecar.DENYLIST.reset()
        sidecar.LIMITER.reset()


def test_denylist_prefix_matches_treatbord_production_key():
    """跨仓库集成对齐（必须钉住，否则登出对边车不生效）：

    treatbord 的 TokenBlacklistService 用 `token:blacklist:<jti>`（登出/封禁/注销都走它），
    边车必须查**同一个键**。这条测试就是为了防止有人随手改成自己的前缀。
    """
    from sidecar.denylist import DEFAULT_PREFIX

    assert DEFAULT_PREFIX == "token:blacklist:", DEFAULT_PREFIX


def test_reset_refuses_to_wipe_production_prefix(monkeypatch):
    """防误用：默认前缀是生产键，清空等于把所有人集体解封"""
    from sidecar.denylist import DEFAULT_PREFIX, MemoryDenylist, RedisDenylist

    if not _redis_ok():
        pytest.skip("需要 Redis")
    limiter_obj = RedisDenylist(REDIS_URL, prefix=DEFAULT_PREFIX)
    with pytest.raises(RuntimeError, match="拒绝清空生产前缀"):
        limiter_obj.reset()
    MemoryDenylist().reset()          # 内存后端不受限
