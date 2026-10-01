# -*- coding: utf-8 -*-
"""内部 JWT 校验（契约 §2.2）—— 只用标准库，不引新依赖。

为什么自己写而不是装 PyJWT：HS256 的校验逻辑就是"base64url + HMAC-SHA256 + 常量时间比较"，
标准库足够；少一个依赖就少一处供应链面。反过来，**算法字段必须锁定**：
用 `alg` 决定校验方式是被反复利用的经典漏洞（`alg=none` 直接放行、RS256→HS256 混淆用公钥当密钥），
所以这里只接受 `HS256`，绝不按 token 自称的算法去分发校验器。

契约 §2.2 的硬约束，逐条落在这里：
    sub   必须正整数（字符串形式，Principal 再校验一次类型与范围）
    role  只允许 USER / OPERATOR / ADMIN
    exp   必须存在且未过期；签发时长 ≤ 5 分钟（管理员降权后旧 token 最长 5 分钟内仍有效）
    aud   必须包含 "ai-sidecar"
    iat   不能在未来（允许少量时钟容差）
    ——不接受任何客户端自带的 user_id / X-User-Id（请求 schema 里根本没有该字段）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from agent.policy import ROLES, Principal

ALGORITHM = "HS256"
AUDIENCE = "ai-sidecar"
MAX_LIFETIME_SEC = 300        # 契约：exp ≤ 5 分钟
DEFAULT_LEEWAY_SEC = 5        # 时钟容差（同机部署，给一点点就够）


class AuthError(RuntimeError):
    """认证失败：code 直接对应契约 §2.4 的错误码"""

    def __init__(self, message: str, *, code: str = "UNAUTHENTICATED") -> None:
        super().__init__(message)
        self.code = code


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + pad)
    except Exception as exc:  # noqa: BLE001
        raise AuthError("token 片段不是合法 base64url") from exc


def sign(payload: dict[str, Any], secret: str) -> str:
    """签发一个 HS256 token（treatbord 侧的等价物在 Java；这里供联调与测试使用）"""
    header = {"alg": ALGORITHM, "typ": "JWT"}
    head = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode("utf-8"), ("%s.%s" % (head, body)).encode("ascii"),
                   hashlib.sha256).digest()
    return "%s.%s.%s" % (head, body, _b64url_encode(mac))


def verify(token: str, *, secret: str, audience: str = AUDIENCE,
           max_lifetime: int = MAX_LIFETIME_SEC, leeway: int = DEFAULT_LEEWAY_SEC,
           now: float | None = None) -> Principal:
    """校验内部 JWT 并返回 Principal；任何不合规都抛 AuthError。"""
    if not secret:
        raise AuthError("边车未配置 JWT 密钥", code="AUTH_NOT_CONFIGURED")
    if not token or token.count(".") != 2:
        raise AuthError("缺少或格式错误的 Authorization Bearer token")

    head_seg, body_seg, sig_seg = token.split(".")

    # 1) 算法锁定：只认 HS256，不看 token 自称什么就信什么
    try:
        header = json.loads(_b64url_decode(head_seg))
    except (ValueError, AuthError) as exc:
        raise AuthError("token header 不是合法 JSON") from exc
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        raise AuthError("算法不被接受：只允许 %s（拒绝 alg=none / 算法混淆）" % ALGORITHM)

    # 2) 签名：常量时间比较，避免时序侧信道
    expected = hmac.new(secret.encode("utf-8"), ("%s.%s" % (head_seg, body_seg)).encode("ascii"),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(sig_seg)):
        raise AuthError("签名校验失败")

    # 3) 载荷
    try:
        payload = json.loads(_b64url_decode(body_seg))
    except (ValueError, AuthError) as exc:
        raise AuthError("token payload 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise AuthError("token payload 必须是 JSON 对象")

    now = time.time() if now is None else now

    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in audiences:
        raise AuthError("aud 不符：期望 %s" % audience)

    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, (int, float)):
        raise AuthError("token 缺少 exp")
    if now > float(exp) + leeway:
        raise AuthError("token 已过期")
    if isinstance(iat, (int, float)):
        if float(iat) > now + leeway:
            raise AuthError("iat 在未来（时钟或伪造）")
        if float(exp) - float(iat) > max_lifetime + leeway:
            raise AuthError("token 有效期超过 %d 秒（契约要求 exp ≤ 5 分钟）" % max_lifetime)

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        # 契约：sub 必须是正整数的字符串形式；字符串/0/负数一律拒绝
        raise AuthError("sub 必须是正整数的字符串形式")
    try:
        return Principal(user_id=int(sub), role=str(payload.get("role", "")).upper())
    except ValueError as exc:
        raise AuthError("身份不合法：%s" % exc) from exc


def principal_from_bearer(authorization: str | None, *, secret: str, **kwargs: Any) -> Principal:
    """从 Authorization 头解析并校验；头缺失/前缀不对也抛 AuthError。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("缺少 Authorization: Bearer <内部JWT>")
    return verify(authorization.split(" ", 1)[1].strip(), secret=secret, **kwargs)


__all__ = ["AuthError", "ALGORITHM", "AUDIENCE", "MAX_LIFETIME_SEC", "sign", "verify",
           "principal_from_bearer", "ROLES"]
