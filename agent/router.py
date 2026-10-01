# -*- coding: utf-8 -*-
"""意图路由：闲聊直答 / 知识库问答（RAG）/ 数据查询（NL2SQL）

三级策略（成本从低到高，避免每个问题都多花一次 LLM 调用）：
1) 强数据线索（聚合词/时间词/表名业务词）→ data
2) 强知识线索（状态/规则/流程/怎么/为什么/是什么…）且无聚合词 → knowledge
3) 都不明确或冲突 → 交给 LLM 分类（可注入 classify_fn；失败则默认 data）
"""
from __future__ import annotations

import re
from typing import Callable

CHAT = "chat"
KNOWLEDGE = "knowledge"
DATA = "data"

CHAT_PATTERNS = [
    r"^(你好|您好|hi|hello|hey|在吗|嗨)[!！。~\s]*$",
    r"(你是谁|你叫什么|介绍一下你|你能做什么|你会什么|怎么用)",
    r"^(谢谢|感谢|多谢|thanks|thank you)",
    r"^(再见|拜拜|bye)",
]

# 聚合/统计诉求
AGG_HINTS = [
    r"(多少|几个|几条|几笔|排名|统计|总数|平均|最大|最小|占比|分布|趋势|明细|列表)",
    r"(查询|列出|看看|给我|展示).{0,6}(数据|明细|列表)",
]
# 时间线索（单独不足以判定为数据问题："今天天气" 不该查库）
TIME_HINTS = [r"(近\s*\d+\s*天|今天|昨天|本周|本月|上周|上月|最近)"]
# 直接输入 SQL 的情况
SQL_HINTS = [r"\b(select|count|sum|avg|group by|from)\b"]
DATA_HINTS = AGG_HINTS + TIME_HINTS + SQL_HINTS

# 需要"讲规则/流程/定义"的线索（强）
KNOWLEDGE_STRONG = [
    r"(状态机|状态有哪些|有哪些状态|流转)",
    r"(规则|口径|政策|流程|条件|限制|门槛|机制|原理)",
    r"(怎么|如何|为什么|是什么|什么是|什么意思|能否|可以吗|支持吗|需要什么)",
    r"(多久|什么时候|几天|多长时间).{0,6}(审核|结算|到账|处理)",
    r"(支持|是否支持|能否|可以吗|需要什么|怎么算|收费|费用|价格|规则是什么)",
]

# 业务名词：既能进数据问题也能进知识问题，单靠它不足以判定
BIZ_NOUNS = r"(任务|用户|接取|结算|举报|通知|评价|赏金|信用分|文件|审核|登录|审计|配置|权限|隐私)"


def route(question: str, *, classify_fn: Callable[[str], str] | None = None) -> str:
    q = (question or "").strip()
    if not q:
        return CHAT

    has_agg = any(re.search(p, q, re.I) for p in AGG_HINTS)
    has_time = any(re.search(p, q, re.I) for p in TIME_HINTS)
    has_sql = any(re.search(p, q, re.I) for p in SQL_HINTS)
    has_data = has_agg or has_time or has_sql
    has_biz = re.search(BIZ_NOUNS, q) is not None
    has_kb = any(re.search(p, q, re.I) for p in KNOWLEDGE_STRONG)

    # 1) 纯打招呼 / 自我介绍
    for p in CHAT_PATTERNS:
        if re.search(p, q, re.I) and not has_biz:
            return CHAT

    # 2) 直接输入 SQL → 数据
    if has_sql:
        return DATA

    # 3) 强知识线索且没有聚合诉求 → 知识库
    if has_kb and not has_agg:
        return KNOWLEDGE

    # 4) 数据分支的硬门槛：**必须出现 treatbord 业务名词**
    #    （否则"公司年假有多少天"这类库外问题会被误送进 SQL 生成；实测踩到）
    if has_biz:
        if has_agg or has_time:
            return DATA
        if classify_fn:
            try:
                got = classify_fn(q)
                if got in (CHAT, KNOWLEDGE, DATA):
                    return got
            except Exception:  # noqa: BLE001
                pass
        return KNOWLEDGE

    # 5) 无业务名词但带聚合/时间词（如"今天天气怎么样"）→ 交给分类器，失败则走知识库（会如实拒答）
    if has_agg or has_time:
        if classify_fn:
            try:
                got = classify_fn(q)
                if got in (CHAT, KNOWLEDGE, DATA):
                    return got
            except Exception:  # noqa: BLE001
                pass
        return KNOWLEDGE

    # 6) 兜底：先问分类器（短句也问，避免"你们支持信用卡支付吗"被当成闲聊）；失败默认 data
    if classify_fn:
        try:
            got = classify_fn(q)
            if got in (CHAT, KNOWLEDGE, DATA):
                return got
        except Exception:  # noqa: BLE001
            pass
    return CHAT if len(q) <= 12 else DATA


ROUTE_SYSTEM = """判断用户问题的类型，只回复一个词：
- data：需要查询数据库才能回答（统计数字、明细列表、排名、趋势）
- knowledge：询问业务规则/流程/定义/操作方式（不需要查数）
- chat：问候、闲聊、自我介绍
只回复 data / knowledge / chat 三者之一。"""


def llm_classify(question: str) -> str:
    """可选的 LLM 分类器（仅在规则无法判定时调用，省 token）

    走**便宜档**：三选一的分类任务、输出上限 8 token，没有理由用主档模型。
    """
    import llm as llm_mod
    from config import LLM

    text = llm_mod.chat([{"role": "system", "content": ROUTE_SYSTEM},
                         {"role": "user", "content": question}], temperature=0, max_tokens=8,
                        tag="route", model=LLM.model_cheap).strip().lower()
    for k in (DATA, KNOWLEDGE, CHAT):
        if k in text:
            return k
    return DATA