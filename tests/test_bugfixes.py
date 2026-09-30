# -*- coding: utf-8 -*-
"""既有缺陷的回归用例（嵌入改造过程中发现，见 docs/treatbord嵌入-技术栈重设计.md）

每一条都对应一个真实存在过的缺陷，作用是防止它们被"顺手改回来"。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import policy  # noqa: E402
from agent.policy import Principal  # noqa: E402

USER = Principal(user_id=7)
ADMIN = Principal(user_id=1, role="ADMIN")


def test_config_defines_secrets_loader_once():
    """config.py 里 _load_streamlit_secrets 曾被重复定义 5 次（且每次定义后都调用一次）"""
    import config as config_mod

    src = inspect.getsource(config_mod)
    assert src.count("def _load_streamlit_secrets()") == 1
    assert src.count("\n_load_streamlit_secrets()") == 1


def test_router_has_no_unreachable_code_after_return():
    """router.route() 末尾曾有一段 return 之后的死代码（重复了一遍分类器逻辑）"""
    from agent import router

    tail = inspect.getsource(router.route).rsplit("return", 1)[-1]
    assert "classify_fn(" not in tail, "route() 末尾又出现不可达代码"


def test_domain_rules_has_no_double_percent():
    """DATE_FORMAT(create_time,'%%Y-%%m') 的 %% 会被模型照抄；
    MySQL 里 %% 是字面 %，结果是返回 '%Y-%m' 字符串而不是年月"""
    from agent.prompts import DOMAIN_RULES

    assert "%%" not in DOMAIN_RULES


def test_cache_isolated_between_roles(tmp_path):
    """缓存键必须带权限维度：否则用户 A 缓存的 SQL 会被用户 B 直接复用（越权）"""
    from agent.cache import SqlCache

    c = SqlCache(path=tmp_path / "c.json", ttl_sec=600, enabled=True)
    q = "我有几个任务"
    c.put(q, "SELECT 1", top_k=5, scope=policy.cache_scope(USER))

    assert c.get(q, 5, scope=policy.cache_scope(USER)) is not None
    assert c.get(q, 5, scope=policy.cache_scope(ADMIN)) is None      # 跨角色不串号
    assert c.get(q, 5) is None                                       # 未传身份也不得命中


def test_cache_scope_ignores_user_id_but_tracks_role_and_version():
    """同角色共享缓存是安全的（值是重写前 SQL）；跨角色必须隔离"""
    assert policy.cache_scope(Principal(user_id=7)) == policy.cache_scope(Principal(user_id=8))
    assert policy.cache_scope(Principal(user_id=7)) != policy.cache_scope(ADMIN)
    assert policy.POLICY_VERSION in policy.cache_scope(USER)


def test_nl2sql_answer_accepts_principal():
    """answer() 必须能从外部接收身份，否则服务化后无法隔离缓存"""
    from agent import nl2sql

    params = inspect.signature(nl2sql.answer).parameters
    assert "principal" in params, "answer() 缺少 principal 参数"
    assert params["principal"].default is None
