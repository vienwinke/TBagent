# -*- coding: utf-8 -*-
"""提示词功能回归：SCOPE_FEWSHOT 必须真的被用上；NL2SQL few-shot 必须真的可切换。

背景（修的两个"半成品"）：
1. `SCOPE_FEWSHOT`（7 条口径示例）一直定义在 prompts_user.py 里，**从未被引用**；
2. README 宣称 NL2SQL 有 few-shot，但 `nl2sql_messages` 里一条示例都没有。

第 2 条现在是**可开关**的（默认关闭）：开启会改变提示词口径，而 README 那批
已存档指标是在关闭状态下跑出来的 —— 不重跑评估就不能悄悄改口径。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import prompts_user                      # noqa: E402
from agent.policy import Principal                  # noqa: E402

PRINCIPAL = Principal(user_id=1)


def test_scope_prompt_actually_uses_fewshot_examples():
    msgs = prompts_user.scope_guard_messages("我上周接了几个任务？")
    system = msgs[0]["content"]

    assert "【判定示例" in system, "SCOPE_FEWSHOT 没有被接进 scope 提示词"
    for question, scope in prompts_user.SCOPE_FEWSHOT:
        assert question in system, "示例问题缺失：%s" % question
        assert scope in system, "示例口径缺失：%s" % scope

    # 示例必须待在 system 里，不能混进 user —— 否则会被当成"用户输入"的一部分
    assert "【判定示例" not in msgs[1]["content"]


def test_nl2sql_fewshot_is_switchable():
    off = prompts_user.nl2sql_messages("（schema）", "我接了几个任务？", PRINCIPAL, fewshot=False)
    on = prompts_user.nl2sql_messages("（schema）", "我接了几个任务？", PRINCIPAL, fewshot=True)

    assert "【示例" not in off[1]["content"], "关闭时不得出现示例"
    assert "【示例" in on[1]["content"], "开启时必须注入示例"
    # 示例要能示范 {{ME}} 与逻辑删除这两个最容易写错的约定
    assert "{{ME}}" in on[1]["content"]
    assert "deleted = 0" in on[1]["content"]
    # 无论开关，真正的问题都必须保留
    assert "我接了几个任务？" in on[1]["content"]
    assert "我接了几个任务？" in off[1]["content"]


def test_nl2sql_fewshot_default_follows_env(monkeypatch):
    monkeypatch.setenv("NL2SQL_FEWSHOT", "1")
    assert prompts_user.nl2sql_fewshot_enabled() is True
    monkeypatch.setenv("NL2SQL_FEWSHOT", "0")
    assert prompts_user.nl2sql_fewshot_enabled() is False
    monkeypatch.delenv("NL2SQL_FEWSHOT", raising=False)
    assert prompts_user.nl2sql_fewshot_enabled() is False, "默认必须是关闭（不动已存档口径）"


def test_fewshot_block_is_pure():
    """同样的输入必须得到同样的提示词（提示词里不能有随机/时间因素）"""
    a = prompts_user.nl2sql_fewshot_block()
    b = prompts_user.nl2sql_fewshot_block()
    assert a == b
    assert a.count("问：") == len(prompts_user.NL2SQL_FEWSHOT)
