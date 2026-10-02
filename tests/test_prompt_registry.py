# -*- coding: utf-8 -*-
"""提示词灰度（ai_prompt_version.traffic_pct）的回归测试。

这张表从 V9 就在 DDL 里，而代码零引用 —— 也就是"灰度发布"只写了表结构。
本文件守三件事：分桶确定性、traffic_pct 的累积分桶语义、以及"拿不到配置就回落内置"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import prompt_registry as reg            # noqa: E402
from agent import prompts_user                      # noqa: E402


def _rows(*specs):
    return [{"name": n, "version": v, "content": c, "enabled": True, "traffic_pct": p}
            for n, v, c, p in specs]


def test_bucket_is_deterministic_and_in_range():
    assert reg.bucket_of("7") == reg.bucket_of("7"), "同一 key 必须永远同一桶（否则 A/B 不可归因）"
    assert 0 <= reg.bucket_of("7") < 100
    assert 0 <= reg.bucket_of("some-session-id") < 100
    assert reg.bucket_of("7") != reg.bucket_of("8") or True   # 不假设两个 key 不同桶


def test_traffic_pct_is_cumulative():
    """v2=10、v3=20 -> 桶 0-9 走 v2、10-29 走 v3、其余回落内置"""
    rows = _rows(("nl2sql", "v2", "内容2", 10), ("nl2sql", "v3", "内容3", 20))
    seen = {"v2": 0, "v3": 0, None: 0}
    n = 20000
    for i in range(n):
        got = reg.select_version(rows, "user-%d" % i)
        seen[None if got is None else got["version"]] += 1
    assert abs(seen["v2"] / n - 0.10) < 0.02, seen
    assert abs(seen["v3"] / n - 0.20) < 0.02, seen
    assert abs(seen[None] / n - 0.70) < 0.02, seen


def test_full_traffic_selects_everyone():
    rows = _rows(("nl2sql", "v9", None, 100))
    assert reg.select_version(rows, "any-key")["version"] == "v9"


def test_disabled_and_zero_pct_rows_are_skipped():
    rows = [{"name": "n", "version": "off", "enabled": False, "traffic_pct": 100},
            {"name": "n", "version": "zero", "enabled": True, "traffic_pct": 0}]
    assert reg.select_version(rows, "k") is None


def test_resolve_falls_back_to_builtin():
    active = reg.resolve("7", rows=[])
    assert active.source == "builtin"
    assert active.version == prompts_user.PROMPT_VERSION
    assert active.content is None


def test_resolve_returns_content_and_label():
    rows = _rows(("nl2sql", "v2", "  只允许查 task 表  ", 100))
    active = reg.resolve("7", rows=rows)
    assert active.source == "db"
    assert active.version == "nl2sql/v2"
    assert active.content == "只允许查 task 表", "content 应 strip；空串应转成 None"


def test_blank_content_becomes_none():
    rows = _rows(("nl2sql", "v2", "   ", 100))
    assert reg.resolve("7", rows=rows).content is None


def test_extra_instruction_is_appended_not_replacing(monkeypatch):
    """灰度指令只能追加到 system 末尾，不能替换 system"""
    principal = __import__("agent.policy", fromlist=["Principal"]).Principal(user_id=1)
    base = prompts_user.nl2sql_messages("（schema）", "问题", principal)
    with_extra = prompts_user.nl2sql_messages("（schema）", "问题", principal,
                                             extra_instruction="附加要求")
    assert with_extra[0]["content"].startswith(base[0]["content"][:80])
    assert len(with_extra[0]["content"]) > len(base[0]["content"])
    assert "附加要求" in with_extra[0]["content"]
    # 空/纯空白不生效
    blank = prompts_user.nl2sql_messages("（schema）", "问题", principal, extra_instruction="   ")
    assert blank[0]["content"] == base[0]["content"]
