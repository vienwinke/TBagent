# -*- coding: utf-8 -*-
"""内部 JWT 校验（契约 §2.2）：`sidecar/auth.py`。

安全用例的重点不是"合法 token 能过"，而是**非法形态必须全被挡住**：
算法混淆（alg=none / RS256→HS256）、签名伪造、过期、aud 不符、sub 非正整数。
以及边车的错误映射：缺/坏/过期 → 401；密钥没配 → 503（fail-closed）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.pipeline import Deps  # noqa: E402
from sidecar import app as sidecar  # noqa: E402
from sidecar import auth  # noqa: E402

SECRET = "unit-test-secret"
SQL = "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0"
DEPS = Deps(nl2sql_llm=lambda messages: {"sql": SQL, "reason": "t", "tables": ["task"]},
            summary_llm=lambda messages: "共 8 个任务。")
BODY = {"session_id": "s_1", "question": "待接取的任务有几个？", "client_msg_id": "c_1"}


def make_token(*, secret: str = SECRET, sub: str = "7", role: str = "USER",
               aud: str = "ai-sidecar", ttl: float = 120, iat: float | None = None,
               now: float | None = None, **extra) -> str:
    now = time.time() if now is None else now
    iat = now if iat is None else iat
    payload = {"sub": sub, "role": role, "jti": "j-1", "iat": iat, "exp": iat + ttl, "aud": aud}
    payload.update(extra)
    return auth.sign(payload, secret)


def forge(header: dict, payload: dict, *, signature: bytes = b"") -> str:
    """手工拼一个 token（用于 alg=none / 算法混淆这类攻击形态）"""
    enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    head = enc(json.dumps(header, separators=(",", ":")).encode())
    body = enc(json.dumps(payload, separators=(",", ":")).encode())
    return "%s.%s.%s" % (head, body, enc(signature))


# ------------------------------------------------------------------ 合法路径
def test_valid_token_yields_principal():
    p = auth.verify(make_token(), secret=SECRET)
    assert p.user_id == 7 and p.role == "USER"


@pytest.mark.parametrize("role", ["USER", "OPERATOR", "ADMIN"])
def test_all_three_roles_accepted(role):
    assert auth.verify(make_token(role=role), secret=SECRET).role == role


# ------------------------------------------------------------------ 算法混淆
def test_alg_none_is_rejected():
    payload = {"sub": "7", "role": "ADMIN", "aud": "ai-sidecar",
               "iat": time.time(), "exp": time.time() + 60}
    with pytest.raises(auth.AuthError, match="算法不被接受"):
        auth.verify(forge({"alg": "none", "typ": "JWT"}, payload), secret=SECRET)


def test_algorithm_confusion_rs256_header_with_hmac_signature():
    """用 HMAC 签的 token 却声称 RS256：必须按 header 拒绝，而不是按它自称的算法去校验"""
    payload = {"sub": "7", "role": "USER", "aud": "ai-sidecar",
               "iat": time.time(), "exp": time.time() + 60}
    sig = hmac.new(SECRET.encode(), b"whatever", hashlib.sha256).digest()
    with pytest.raises(auth.AuthError, match="算法不被接受"):
        auth.verify(forge({"alg": "RS256", "typ": "JWT"}, payload, signature=sig), secret=SECRET)


# ------------------------------------------------------------------ 签名与期限
def test_forged_signature_rejected():
    with pytest.raises(auth.AuthError, match="签名校验失败"):
        auth.verify(make_token(secret="attacker-secret"), secret=SECRET)


def test_expired_token_rejected():
    now = time.time()
    with pytest.raises(auth.AuthError, match="已过期"):
        auth.verify(make_token(iat=now - 600, ttl=60, now=now), secret=SECRET)


def test_lifetime_over_five_minutes_rejected():
    """契约：exp ≤ 5 分钟 —— 长命 token 会让"降权后 5 分钟内仍有效"的窗口失控"""
    with pytest.raises(auth.AuthError, match="有效期超过"):
        auth.verify(make_token(ttl=3600), secret=SECRET)


def test_missing_exp_rejected():
    payload = {"sub": "7", "role": "USER", "aud": "ai-sidecar"}
    token = auth.sign(payload, SECRET)
    with pytest.raises(auth.AuthError, match="缺少 exp"):
        auth.verify(token, secret=SECRET)


def test_iat_in_the_future_rejected():
    now = time.time()
    with pytest.raises(auth.AuthError, match="iat 在未来"):
        auth.verify(make_token(iat=now + 600, ttl=60, now=now), secret=SECRET)


# ------------------------------------------------------------------ 载荷合法性
def test_wrong_audience_rejected():
    with pytest.raises(auth.AuthError, match="aud 不符"):
        auth.verify(make_token(aud="other-service"), secret=SECRET)


@pytest.mark.parametrize("sub", ["abc", "0", "-1", "7.0", ""])
def test_sub_must_be_positive_integer_string(sub):
    with pytest.raises(auth.AuthError):
        auth.verify(make_token(sub=sub), secret=SECRET)


def test_unknown_role_rejected():
    with pytest.raises(auth.AuthError, match="身份不合法"):
        auth.verify(make_token(role="SUPERUSER"), secret=SECRET)


def test_malformed_token_rejected():
    for bad in ("", "abc", "a.b", "a.b.c.d"):
        with pytest.raises(auth.AuthError):
            auth.verify(bad, secret=SECRET)


def test_missing_secret_is_not_configured():
    with pytest.raises(auth.AuthError) as exc:
        auth.verify(make_token(), secret="")
    assert exc.value.code == "AUTH_NOT_CONFIGURED"


# ------------------------------------------------------------------ 边车端点映射
@pytest.fixture
def client(monkeypatch):
    sidecar.STATS.reset()
    sidecar.LIMITER.reset()
    monkeypatch.delenv("SIDECAR_DEV_PRINCIPAL", raising=False)
    monkeypatch.setenv("SIDECAR_JWT_SECRET", SECRET)
    monkeypatch.setattr(sidecar, "DEPS", DEPS)
    return TestClient(sidecar.app)


def test_valid_jwt_serves_sse(client):
    r = client.post("/v1/ai/chat", json=BODY, headers={"Authorization": "Bearer " + make_token()})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in r.text and "event: done" in r.text


def test_forged_jwt_is_401(client):
    r = client.post("/v1/ai/chat", json=BODY,
                    headers={"Authorization": "Bearer " + make_token(secret="attacker")})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHENTICATED" and r.json()["retryable"] is False


def test_expired_jwt_is_401(client):
    now = time.time()
    token = make_token(iat=now - 600, ttl=60, now=now)
    r = client.post("/v1/ai/chat", json=BODY, headers={"Authorization": "Bearer " + token})
    assert r.status_code == 401


def test_missing_authorization_is_401(client):
    r = client.post("/v1/ai/chat", json=BODY)
    assert r.status_code == 401 and "Bearer" in r.json()["message"]


def test_admin_jwt_gets_sql_text(client):
    token = make_token(sub="1", role="ADMIN")
    r = client.post("/v1/ai/chat", json=BODY, headers={"Authorization": "Bearer " + token})
    assert '"sql"' in r.text and "LIMIT" in r.text


def test_dev_principal_is_ignored_when_jwt_secret_configured(client, monkeypatch):
    """fail-safe：配了密钥就只认 JWT —— 生产环境残留的开发变量不能变成后门"""
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "1:ADMIN")
    r = client.post("/v1/ai/chat", json=BODY)
    assert r.status_code == 401, "有密钥时开发身份必须失效"


def test_not_configured_still_503(monkeypatch):
    sidecar.STATS.reset()
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.delenv("SIDECAR_DEV_PRINCIPAL", raising=False)
    r = TestClient(sidecar.app).post("/v1/ai/chat", json=BODY)
    assert r.status_code == 503 and r.json()["code"] == "AUTH_NOT_CONFIGURED"


def test_readyz_reports_auth_configured(monkeypatch):
    monkeypatch.setenv("SIDECAR_JWT_SECRET", SECRET)
    body = TestClient(sidecar.app).get("/readyz").json()
    assert body["auth_configured"] is True


# ------------------------------------------------------------------ jti 黑名单（即时降权）
def test_revoked_jti_is_rejected():
    """契约 §2.2：降权/登出后旧 token 要**即时**失效，不能等 5 分钟过期"""
    token = make_token()
    assert auth.verify(token, secret=SECRET).user_id == 7                  # 未吊销：可用
    revoked = {"j-1"}          # make_token() 里的 jti 就是 "j-1"
    with pytest.raises(auth.AuthError, match="已吊销"):
        auth.verify(token, secret=SECRET, is_revoked=lambda jti: jti in revoked)


def test_unknown_jti_passes():
    assert auth.verify(make_token(), secret=SECRET,
                       is_revoked=lambda jti: False).user_id == 7


def test_missing_jti_never_revoked():
    """没有 jti 的 token 不该因为"查不到"被拒（黑名单只对明确的 jti 生效）"""
    payload = {"sub": "7", "role": "USER", "aud": "ai-sidecar",
               "iat": time.time(), "exp": time.time() + 60}
    token = auth.sign(payload, SECRET)
    assert auth.verify(token, secret=SECRET, is_revoked=lambda jti: jti is not None).user_id == 7


# ------------------------------------------------------------------ 黑名单后端
def test_memory_denylist_ttl():
    from sidecar.denylist import MemoryDenylist

    dl = MemoryDenylist()
    assert dl.is_revoked("j1") is False
    dl.revoke("j1", ttl_sec=60)
    assert dl.is_revoked("j1") is True
    dl.revoke("j2", ttl_sec=1)
    dl._revoked["j2"] = time.monotonic() - 1        # 手动过期
    assert dl.is_revoked("j2") is False
    dl.reset()
    assert dl.is_revoked("j1") is False


def test_make_denylist_falls_back_without_redis(monkeypatch):
    from sidecar import denylist as dl_mod

    monkeypatch.delenv("SIDECAR_REDIS_URL", raising=False)
    assert dl_mod.make_denylist().backend() == "memory"
    monkeypatch.setenv("SIDECAR_REDIS_URL", "redis://127.0.0.1:6399/0")
    assert dl_mod.make_denylist().backend() == "memory", "连不上必须降级而不是崩"
