# -*- coding: utf-8 -*-
"""语义层范围判定（P1）：在**生成 SQL 之前**判定"这个问题该不该答"。

为什么必须有它 —— 行级隔离只解决了一半问题：
  policy 的派生表替换保证普通用户"问不到别人"，但它会把
  「平台上个月总成交额」静默改写成「我的成交额」：**数字含义被改写**，
  而用户会拿这个数字当结论 —— 错答比拒答更危险。
  表白名单则只拦"引用了不可见的表"，拦不住"问题本身越权、而生成的 SQL 恰好只用可见表"：
  「平台一共有多少条审计记录」只要模型不写 audit_log，就能生成一条看似正常的 SQL。

三级策略（与 docs/treatbord嵌入-技术栈重设计.md §5.4 一致）：
  1) 规则层（默认，确定性）：先判最严格的 SENSITIVE / 注入，再判 OTHER_USER / PLATFORM，
     然后 MARKET / SELF；命中即返回，**绝不调用模型**
  2) LLM 兜底（可选）：只有调用方显式传入 scope_llm_fn、且规则一个都没命中时才用
  3) 角色闸门：SENSITIVE 对所有角色拒答；PLATFORM / OTHER_USER 只对非特权角色拒答

确定性是硬要求：实测过同一个平台级问题两次运行结果不同（一次拒答、一次错答），
根因就是判定依赖了模型采样。规则命中路径不碰模型，因此同一问题必然同一结论。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from agent.policy import Principal

# ---------------------------------------------------------------- 范围取值
SELF = "SELF"
MARKET = "MARKET"
PLATFORM = "PLATFORM"
OTHER_USER = "OTHER_USER"
SENSITIVE = "SENSITIVE"
NON_BUSINESS = "NON_BUSINESS"
ALL_SCOPES = (SELF, MARKET, PLATFORM, OTHER_USER, SENSITIVE, NON_BUSINESS)

# 拒答原因：前三个与 policy.DENY_* 同值（同一套话术，前端只认字符串）；
# DENY_INJECTION / DENY_MODEL_REFUSE 是语义层独有的，key 必须存在于 prompts_user.DENY_TEMPLATES。
DENY_PLATFORM = "DENY_PLATFORM"
DENY_OTHER_USER = "DENY_OTHER_USER"
DENY_SENSITIVE = "DENY_SENSITIVE"
DENY_INJECTION = "DENY_INJECTION"
DENY_MODEL_REFUSE = "DENY_MODEL_REFUSE"

# ---------------------------------------------------------------- 规则表
# 顺序 = 严格度。先命中先返回，所以"平台有多少在招任务"不会被 平台 误判成 PLATFORM。
INJECTION_RULES = (
    r"忽略.{0,8}(规则|指令|限制|设定)",
    r"绕过.{0,8}(规则|限制|权限|校验|护栏)",
    r"你现在是|扮演.{0,6}(管理员|admin|root)|系统提示词|你的提示词|prompt",
    r"(直接|立刻|马上)?(给我|执行|跑|运行).{0,8}(sql|语句|查询)",
)

# 「导出/外发」意图：与凭据类名词同时出现时，对所有角色都拒（管理员也不能导凭据）
EXPORT_RULES = (
    r"导出|下载全部|全量下载|导库|\bdump\b",
    r"把.{0,12}(数据|记录|列表|账号|凭据).{0,8}(给我|发我)",
)

# 「凭据/身份字段」名词本身。注意：**仅提到字段名不等于索要取值** ——
# 「设置了账号密码的用户有几个」是合法的聚合问题（COUNT 不可能泄漏原值），
# 所以要与下面的统计语义词共同判定，否则会把正常业务问题也拒掉（实测踩到 agg-13）。
CREDENTIAL_RULES = (
    r"password_hash|passwd|\bpwd\b|密码|口令",
    r"openid|unionid|access[_\s]?token|\btoken\b|凭据|私钥|密钥|api[_\-\s]?key",
    r"手机号|身份证|银行卡|实名信息",
)

# 统计语义词：问数量/占比/平均时，不构成"索要凭据"
AGGREGATE_HINTS = (
    r"有多少|几个|多少个|多少条|多少人|数量|个数|统计|占比|比例|平均|\bcount\b",
)

# 探测库结构/系统表：普通用户拒答；运营以上交给静态护栏（白名单本来就拦）
STRUCTURE_RULES = (
    r"information_schema|sqlite_master|show\s+tables|数据库结构|表结构|系统库",
    r"所有表的名字|库里(的|有)哪些表|有哪些表",
)

OTHER_USER_RULES = (
    r"用户\s*[#＃]?\s*\d{2,}",                 # 「用户 12345」
    r"别人|他人|其他人|其他用户|别的用户",
    r"那个人|某用户|某个用户|指定用户",
    r"谁的(手机|密码|结算|接取|收入)",
)

# 硬平台级：语义明确，不可能是"公开任务行情"
PLATFORM_HARD_RULES = (
    r"全站|全平台|整个平台|平台整体|平台总|全库",
    r"所有用户|全部用户|全体用户|每个用户",
    r"日活|月活|\bDAU\b|\bMAU\b",
    r"成交额|总流水|平台流水|风控",
    r"平台.{0,4}(有多少|一共|总共).{0,8}(用户|人)",
)

# 公开任务行情：允许（仅 task 表的公开信息），必须在"软平台级"之前判
MARKET_RULES = (
    r"在招|招募中|任务列表|任务行情",
    r"有多少(个)?任务|多少个任务|任务数|任务总数",
    r"赏金|悬赏",
)

# 软平台级：只出现「平台」二字。若同时命中市场行情，则按 MARKET 放行。
PLATFORM_SOFT_RULES = (r"平台",)

SELF_RULES = (r"我|我的|本人|自己",)


def _first_match(question: str, rules: tuple[str, ...]) -> str | None:
    for pat in rules:
        if re.search(pat, question, re.I):
            return pat
    return None


@dataclass(frozen=True)
class ScopeDecision:
    scope: str
    allowed: bool
    reason: str
    source: str                      # rule | llm | default
    deny_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "allowed": self.allowed, "reason": self.reason,
                "source": self.source, "deny_reason": self.deny_reason}


def _verdict(scope: str, principal: Principal, reason: str, source: str,
             *, injection: bool = False, exfil: bool = False) -> ScopeDecision:
    """角色闸门：把"范围"翻译成"能不能答"（这是唯一的放行/拒答判定点）

    敏感字段分两种强度，别混为一谈：
      · exfil=True  —— 索要/导出凭据本身：**所有角色**都拒（管理员也不行，
                       话术原文就是"账号凭据和身份字段我不能查询或导出"）
      · exfil=False —— 只是提到敏感字段：普通用户"先拒后脱敏"；运营/管理员放行，
                       由 executor 的列脱敏兜底（这正是 eval 里 expect=masked 的用例：
                       管理员查密码列必须能跑通并返回 masked_columns，而不是被拒）
    """
    if injection:
        return ScopeDecision(SENSITIVE, False, reason, source, DENY_INJECTION)
    if scope == SENSITIVE:
        if exfil or not principal.is_privileged:
            return ScopeDecision(scope, False, reason, source, DENY_SENSITIVE)
        return ScopeDecision(scope, True, reason + "（运营及以上走脱敏兜底）", source)
    if scope == PLATFORM and not principal.is_privileged:
        return ScopeDecision(scope, False, reason, source, DENY_PLATFORM)
    if scope == OTHER_USER and not principal.is_privileged:
        return ScopeDecision(scope, False, reason, source, DENY_OTHER_USER)
    return ScopeDecision(scope, True, reason, source)


def judge_by_rules(question: str, principal: Principal) -> ScopeDecision | None:
    """纯规则判定（确定性，不调模型）；没有任何规则命中时返回 None。"""
    q = (question or "").strip()
    if not q:
        return ScopeDecision(NON_BUSINESS, True, "空问题", "rule")

    pat = _first_match(q, INJECTION_RULES)
    if pat:
        return _verdict(SENSITIVE, principal, "疑似提示词注入（命中 %s）" % pat, "rule",
                        injection=True)

    # 导出 + 凭据 = 外泄意图 → 所有角色都拒
    export_pat = _first_match(q, EXPORT_RULES)
    cred_pat = _first_match(q, CREDENTIAL_RULES)
    if export_pat and cred_pat:
        return _verdict(SENSITIVE, principal, "要求导出凭据（命中 %s + %s）" % (export_pat, cred_pat),
                        "rule", exfil=True)

    # 只提到敏感字段：统计类提问（问数量）不构成索要凭据，放行给执行侧脱敏
    if cred_pat and not _first_match(q, AGGREGATE_HINTS):
        return _verdict(SENSITIVE, principal, "涉及敏感字段（命中 %s）" % cred_pat, "rule")

    pat = _first_match(q, STRUCTURE_RULES)
    if pat:
        return _verdict(SENSITIVE, principal, "探测库结构/系统表（命中 %s）" % pat, "rule")

    pat = _first_match(q, OTHER_USER_RULES)
    if pat:
        return _verdict(OTHER_USER, principal, "指向其他用户（命中 %s）" % pat, "rule")

    pat = _first_match(q, PLATFORM_HARD_RULES)
    if pat:
        return _verdict(PLATFORM, principal, "全平台口径（命中 %s）" % pat, "rule")

    # 市场行情优先于"软平台级"：否则「平台现在有多少任务在招」会被 平台 二字误拒
    pat = _first_match(q, MARKET_RULES)
    if pat:
        return _verdict(MARKET, principal, "公开任务行情（命中 %s）" % pat, "rule")

    pat = _first_match(q, PLATFORM_SOFT_RULES)
    if pat:
        return _verdict(PLATFORM, principal, "提到平台口径，未限定为公开行情（命中 %s）" % pat,
                        "rule")

    pat = _first_match(q, SELF_RULES)
    if pat:
        return _verdict(SELF, principal, "本人数据（命中 %s）" % pat, "rule")

    return None


def _judge_by_llm(question: str, principal: Principal, history: str,
                  llm_fn: Callable[[list[dict[str, str]]], dict[str, Any]]) -> ScopeDecision | None:
    """LLM 兜底：只在规则完全没命中时调用。模型不可用/输出不可解析 → 返回 None（交给默认放行）。"""
    from agent import prompts_user

    try:
        raw = llm_fn(prompts_user.scope_guard_messages(question, history=history))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scope] 范围判定调用失败，退回默认放行：{}", str(exc)[:120])
        return None
    if not isinstance(raw, dict):
        return None
    scope = str(raw.get("scope", "")).upper().strip()
    if scope not in ALL_SCOPES:
        return None
    reason = str(raw.get("reason", "") or "模型判定")
    injection = bool(raw.get("suspected_injection"))
    return _verdict(scope, principal, "模型判定：%s" % reason, "llm", injection=injection)


def judge(question: str, principal: Principal, *, history: str = "",
          llm_fn: Callable[[list[dict[str, str]]], dict[str, Any]] | None = None) -> ScopeDecision:
    """范围判定入口：规则优先（确定性），LLM 仅在显式传入且规则未命中时兜底。

    默认放行的兜底理由：行级隔离（policy 派生表替换）与列白名单仍是硬底线，
    语义层是**在它之上**的第二道网；模型不可用时宁可放行让它去接受隔离，也不要因误判全面拒答。
    """
    hit = judge_by_rules(question, principal)
    if hit is not None:
        return hit

    if llm_fn is not None:
        decision = _judge_by_llm(question, principal, history, llm_fn)
        if decision is not None:
            return decision

    return ScopeDecision(SELF, True, "规则未命中，按本人数据处理（行级隔离仍生效）", "default")
