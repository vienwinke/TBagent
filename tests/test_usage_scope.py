# -*- coding: utf-8 -*-
"""用量/成本必须按请求隔离。

为什么需要：服务化后一个进程同时处理多个请求，而 `llm.USAGE` 是模块级单例 ——
A 请求的 done 事件会把 B 请求的 token/成本算进自己的增量里，成本面板与审计金额全错。
单机 Streamlit 没有并发，作用域不存在时自动落到全局单例，行为不变。
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from agent import pipeline  # noqa: E402
from agent.policy import Principal  # noqa: E402

USER = Principal(user_id=7)


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion
        self.prompt_tokens_details = None


class _Resp:
    def __init__(self, content: str, prompt: int = 100, completion: int = 10) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage(prompt, completion)


class _Completions:
    def create(self, **kwargs):
        return _Resp('{"sql":"SELECT 1","reason":"stub"}')


class _Client:
    class chat:  # noqa: N801
        completions = _Completions()


def test_without_scope_usage_is_the_global_singleton():
    assert llm.usage() is llm.USAGE


def test_nested_scope_reuses_the_active_one():
    """可重入：服务层再包一层时不能把用量吞进内层"""
    with llm.isolated_usage() as outer:
        with llm.isolated_usage() as inner:
            assert inner is outer
        assert llm.usage() is outer


def test_concurrent_requests_do_not_share_usage(monkeypatch):
    """两个线程各自开作用域：用量互不污染，且都不落到全局单例"""
    monkeypatch.setattr(llm, "client", lambda: _Client())
    global_calls_before = llm.USAGE.calls

    def worker(tag: str) -> dict:
        with llm.isolated_usage() as scope:
            llm.chat([{"role": "user", "content": tag}], tag=tag)
            return scope.summary()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(worker, ["req-A", "req-B"]))

    for one in (a, b):
        assert one["calls"] == 1, "每个请求只应看到自己的调用"
        assert one["prompt_tokens"] == 100 and one["completion_tokens"] == 10
    assert llm.USAGE.calls == global_calls_before, "隔离后不应再写全局单例"


def test_pipeline_reports_only_its_own_usage(monkeypatch):
    """编排层自带作用域：done 事件的 tokens 是本请求的，且不污染全局"""
    monkeypatch.setattr(llm, "client", lambda: _Client())
    global_before = llm.USAGE.summary()

    events = list(pipeline.answer_stream("待接取的任务有几个？", USER, use_cache=False))
    done = [d for e, d in events if e == "done"][0]

    assert done["tokens"] == 110, "done 应报告本请求的用量（100 prompt + 10 completion）"
    assert llm.USAGE.calls == global_before["calls"], "请求用量不得写进全局单例"


def test_usage_scope_is_released_after_request(monkeypatch):
    """作用域用完即释放，避免串到下一个请求"""
    monkeypatch.setattr(llm, "client", lambda: _Client())
    list(pipeline.answer_stream("待接取的任务有几个？", USER, use_cache=False))

    assert llm.usage() is llm.USAGE, "请求结束后必须回到全局作用域"


def test_scope_is_thread_local():
    """不同线程各自的作用域互不可见（contextvars 语义）"""
    seen = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with llm.isolated_usage() as scope:
            scope.calls = 42
            barrier.wait()
            seen[name] = llm.usage().calls

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == {"A": 42, "B": 42}, "各自的调用数必须只属于自己的作用域"
