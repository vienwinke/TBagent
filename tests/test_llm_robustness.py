# -*- coding: utf-8 -*-
"""LLM 调用健壮性：空响应必须重试；截断 JSON 必须给可诊断的错误。

背景（实测）：60 题全量评估里出现 2 条"模型未返回合法 JSON"失败，而此前 11 次全量为 0 ——
其中一条的原始输出是**空字符串**（provider 偶发 HTTP 200 + 空内容），
被上层当成"模型输出不合法"烧掉了回环次数，最终整题失败。
空内容属瞬时故障，应在 chat() 内退避重试，而不是污染上层链路。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


def _fake_client(contents: list[str]):
    """按顺序返回预设内容；用完后重复最后一个。返回 (client, 调用计数盒)。"""
    box = {"i": 0}

    class _Completions:
        def create(self, **kwargs):
            i = min(box["i"], len(contents) - 1)
            box["i"] += 1
            return _Resp(contents[i])

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client(), box


class _StubLLM:
    """LLMConfig 是 frozen dataclass（改不了字段），这里整体替换成一个能通过自检的桩"""

    api_key = "test-key"
    base_url = "http://stub.invalid"
    model = "stub-model"
    timeout = 5.0
    max_retries = 2
    price_in = 0.0
    price_out = 0.0

    @property
    def configured(self) -> bool:
        return True

    def issues(self) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """不连网、不退避等待：把配置与客户端都换成桩"""
    monkeypatch.setattr(llm, "LLM", _StubLLM())
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)


def test_empty_response_is_retried_then_succeeds(monkeypatch):
    client, box = _fake_client(["", '{"sql":"SELECT 1"}'])
    monkeypatch.setattr(llm, "client", lambda: client)
    before = llm.USAGE.retries

    out = llm.chat_json([{"role": "user", "content": "hi"}])

    assert out == {"sql": "SELECT 1"}
    assert box["i"] == 2, "空响应后应当再试一次"
    assert llm.USAGE.retries > before, "重试次数应当被计入用量统计"


def test_persistent_empty_response_raises_clear_error(monkeypatch):
    client, box = _fake_client([""])
    monkeypatch.setattr(llm, "client", lambda: client)

    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])

    assert "空内容" in str(exc.value)
    assert box["i"] == llm.LLM.max_retries + 1, "应当把重试次数用满再放弃"


def test_truncated_json_is_reported_as_truncation(monkeypatch):
    client, _ = _fake_client(['{"sql":"SELECT DATE(create_time) AS d FROM task WHERE'])
    monkeypatch.setattr(llm, "client", lambda: client)

    with pytest.raises(llm.LLMError) as exc:
        llm.chat_json([{"role": "user", "content": "hi"}])

    assert "截断" in str(exc.value), "未闭合的 JSON 需要明确指出疑似截断，便于定位"
    assert "原始输出前 300 字" in str(exc.value)


def test_embedded_json_still_parses(monkeypatch):
    """模型夹带解释文字时，正则兜底解析仍然有效（既有能力不能退化）"""
    client, _ = _fake_client(['好的，结果是 {"sql":"SELECT 1","reason":"r"} 请查收'])
    monkeypatch.setattr(llm, "client", lambda: client)

    assert llm.chat_json([{"role": "user", "content": "hi"}])["sql"] == "SELECT 1"


def test_malformed_without_json_object_keeps_diagnostic(monkeypatch):
    client, _ = _fake_client(["抱歉，我无法完成"])
    monkeypatch.setattr(llm, "client", lambda: client)

    with pytest.raises(llm.LLMError) as exc:
        llm.chat_json([{"role": "user", "content": "hi"}])

    assert "未返回合法 JSON" in str(exc.value)
