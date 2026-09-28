# -*- coding: utf-8 -*-
"""意图路由：决定一个问题走「闲聊直答」还是「数据查询（NL2SQL）」

M4 阶段用**规则**实现（快、零成本、可解释）；M3 会升级为规则 + LLM 分类 + 知识库分支。
规则优先的好处：常见寒暄与越界问题不消耗 token，也不会被误送进 SQL 生成。
"""
from __future__ import annotations

import re

CHAT = "chat"
DATA = "data"

CHAT_PATTERNS = [
    r"^(你好|您好|hi|hello|hey|在吗|嗨)[!！。~\s]*$",
    r"(你是谁|你叫什么|介绍一下你|你能做什么|你会什么|怎么用)",
    r"^(谢谢|感谢|多谢|thanks|thank you)",
    r"^(再见|拜拜|bye)",
]

# 出现这些词基本可判定为数据类问题（提高召回，避免把业务问题误判成闲聊）
DATA_HINTS = [
    r"(多少|几个|几条|几笔|哪些|哪个|排名|统计|总数|平均|最大|最小|占比|分布|趋势|同比|环比)",
    r"(任务|用户|接取|结算|举报|通知|评价|赏金|信用分|文件|举报|审核|登录|审计|配置)",
    r"(近\s*\d+\s*天|今天|昨天|本周|本月|上周|上月|最近)",
    r"(select|count|sum|avg|from)\b",
]


def route(question: str) -> str:
    """返回 CHAT 或 DATA"""
    q = (question or "").strip()
    if not q:
        return CHAT
    for p in DATA_HINTS:
        if re.search(p, q, re.I):
            return DATA
    for p in CHAT_PATTERNS:
        if re.search(p, q, re.I):
            return CHAT
    # 兜底：短且没有数据线索 → 闲聊；否则按数据问题处理（宁可多查一次，也不要答非所问）
    return CHAT if len(q) <= 12 else DATA