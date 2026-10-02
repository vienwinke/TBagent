# -*- coding: utf-8 -*-
"""用量隔离在 **SSE 消费路径**下必须依然成立（回归测试）。

背景（真实踩到的 bug）：Starlette 会把同步生成器放到线程池里**分步**推进，
每一步都在"调用上下文的一份拷贝"里执行。于是 `pipeline` 第一步用
`isolated_usage()` 设的 ContextVar，到第二步就看不见了 → `llm.usage()` 回落到
**进程级全局 USAGE**。现象：一个 8ms、根本没调模型的"你好"请求，done 里却报出
全局累计的 3015 tokens / ¥0.0089；并发时各请求的数字互相污染。

修法：消费端用 `contextvars.copy_context()` 固定一个 Context，每次 `next()` 都在同一作用域里推进。
这条测试就是把"账目必须是本请求的"钉死 —— 只要有人把 `for ... in` 改回去，它就会红。
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import llm as llm_mod  # noqa: E402
from agent.pipeline import Deps  # noqa: E402
from sidecar import app as sidecar  # noqa: E402

SQL = "SELECT COUNT(*) AS c FROM task WHERE deleted = 0"


def _done_payload(text: str) -> dict:
    """从 SSE 文本里取出 done 帧的 data"""
    match = re.search(r"event: done\s*\ndata: (.*)", text)
    assert match, "响应里没有 done 帧：\n" + text[:400]
    return json.loads(match.group(1))


_seq = itertools.count()


def _accounting_llm(messages):        # 模拟真实模型调用：计入当前用量作用域
    scope = llm_mod.usage()
    scope.calls += 1
    scope.prompt_tokens += 1000
    scope.completion_tokens += 50
    # 每次生成**不同的 SQL**：否则第二次会命中问题→SQL 缓存、压根不调模型，
    # 测出来的就不是"账目是否正确"而是"缓存是否生效"（第一版就踩了这个坑）
    return {"sql": "%s /* %d */" % (SQL, next(_seq)), "reason": "统计", "tables": ["task"]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "2:USER")
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=_accounting_llm,
                                              summary_llm=lambda m: "共 8 个任务。"))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()
    return TestClient(sidecar.app)


def test_free_request_reports_zero_tokens(client):
    """不花钱的请求（闲聊）必须报 0 —— 而不是全局累计值"""
    # 故意把全局单例"弄脏"：模拟此前已有别的请求烧过 token
    llm_mod.USAGE.prompt_tokens += 9999
    llm_mod.USAGE.completion_tokens += 111
    try:
        res = client.post("/v1/ai/chat", json={"session_id": "s-iso-1", "question": "你好",
                                               "client_msg_id": "iso-1"})
        done = _done_payload(res.text)
        assert done["route"] == "chat"
        assert done["tokens"] == 0, "闲聊不该带上任何人的 token：%s" % done["tokens"]
        assert done["cost_yuan"] == 0.0
    finally:
        llm_mod.USAGE.prompt_tokens -= 9999
        llm_mod.USAGE.completion_tokens -= 111


def test_paid_request_reports_only_its_own_usage(client):
    """花钱的请求只报**自己**的用量（1050），不含全局历史"""
    llm_mod.USAGE.prompt_tokens += 5000
    llm_mod.USAGE.completion_tokens += 500
    try:
        res = client.post("/v1/ai/chat", json={"session_id": "s-iso-2",
                                               "question": "待接取的任务有几个？",
                                               "client_msg_id": "iso-2"})
        done = _done_payload(res.text)
        assert done["route"] == "data"
        assert done["tokens"] == 1050, "只应统计本请求的用量，实际 %s" % done["tokens"]
    finally:
        llm_mod.USAGE.prompt_tokens -= 5000
        llm_mod.USAGE.completion_tokens -= 500


def test_two_requests_in_a_row_do_not_accumulate(client):
    """连续两次**不同**问题：各自只报自己的 1050，不能把上一条算进来。

    注意不能用同一个问题测 —— 那第二次会命中问题→SQL 缓存、压根不调模型（报 0 是对的，
    但那测的是缓存而不是账目）。
    """
    def run(key: str, question: str) -> int:
        res = client.post("/v1/ai/chat", json={"session_id": "s-iso-3", "question": question,
                                               "client_msg_id": key})
        return _done_payload(res.text)["tokens"]

    first = run("iso-3a", "待接取的任务有几个？")
    second = run("iso-3b", "已完成的任务有几个？")
    assert first == 1050 and second == 1050, (first, second)


def test_repeat_question_is_cached_and_free(client):
    """同一问题重复问：命中缓存 → 不该再产生模型用量（省钱的正确行为）"""
    body = {"session_id": "s-iso-4", "question": "待接取的任务有几个？"}
    first = _done_payload(client.post("/v1/ai/chat", json=dict(body, client_msg_id="iso-4a")).text)
    second = _done_payload(client.post("/v1/ai/chat", json=dict(body, client_msg_id="iso-4b")).text)
    assert first["tokens"] == 1050
    assert second["tokens"] == 0, "命中缓存还报用量，说明账目算错了"
