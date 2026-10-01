# -*- coding: utf-8 -*-
"""全局配置：环境变量 + 护栏参数 + 模型档位 + 单价表

所有可调项集中在 .env；代码里不出现魔法数字。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _load_streamlit_secrets() -> None:
    """Streamlit Cloud / HF Spaces 上密钥放在平台 Secrets（st.secrets），本地用 .env。

    统一注入 os.environ，让下游代码只认环境变量；用 setdefault 保证本地 .env 优先。
    """
    try:
        import streamlit as st  # noqa: PLC0415

        for key, value in dict(st.secrets).items():
            os.environ.setdefault(str(key), str(value))
    except Exception:  # noqa: BLE001  不在 Streamlit 运行时 / 无 secrets 时静默跳过
        pass


_load_streamlit_secrets()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class LLMConfig:
    """大模型接入（OpenAI 兼容）：换平台只改 .env 的 BASE_URL / MODEL"""

    # 通用命名：任何 OpenAI 兼容平台（DeepSeek / Command Code / 通义 / Kimi / GLM…）
    # 兼容旧名 DEEPSEEK_API_KEY
    api_key: str = _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY")
    base_url: str = _env("LLM_BASE_URL", "https://api.deepseek.com")
    model: str = _env("LLM_MODEL", "deepseek-chat")
    model_cheap: str = _env("LLM_MODEL_CHEAP", "deepseek-chat")
    timeout: float = float(_env("LLM_TIMEOUT", "60") or 60)
    max_retries: int = int(_env("LLM_MAX_RETRIES", "3") or 3)
    # 单价（元 / 百万 token）：用于成本统计，换模型时同步更新
    price_in: float = float(_env("LLM_PRICE_IN", "2.0") or 2.0)
    price_out: float = float(_env("LLM_PRICE_OUT", "9.0") or 9.0)
    # 便宜档单价：路由分类（输出仅 8 token）、结果转述、追问建议都走这一档；
    # 未配置则退回主档单价（估算偏保守，不会低估成本）
    price_in_cheap: float = float(_env("LLM_PRICE_IN_CHEAP", "") or price_in)
    price_out_cheap: float = float(_env("LLM_PRICE_OUT_CHEAP", "") or price_out)
    # 命中前缀缓存的输入单价：实测 provider 会缓存公共前缀（跨问题命中 75~95%），
    # 缓存读取通常远低于正常输入价。留空 = 按全价计（**偏保守的上界**，不会低估）
    price_in_cached: float = float(_env("LLM_PRICE_IN_CACHED", "") or price_in)

    def price_for(self, model: str | None) -> tuple[float, float]:
        """按模型返回 (输入单价, 输出单价)，单位：元/百万 token。

        为什么必须按模型区分：项目有主档/便宜档两个模型，而原先的成本统计用**单一单价**——
        便宜档的调用会被按主档计价，账目是错的（成本面板与审计金额都受影响）。
        """
        if model and model == self.model:
            return self.price_in, self.price_out
        if model and model == self.model_cheap:
            return self.price_in_cheap, self.price_out_cheap
        return self.price_in, self.price_out

    def cached_price_in(self, model: str | None) -> float:
        """命中前缀缓存的输入单价（元/百万 token）。未配置时等于正常输入价。"""
        return self.price_in_cached

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def issues(self) -> list[str]:
        """配置自检：返回人类可读的问题列表（云端部署最容易踩的坑都在这）"""
        problems: list[str] = []
        if not self.api_key:
            problems.append("LLM_API_KEY 为空：请在 Secrets/.env 里填入真实 API Key")
        else:
            if self.api_key.isascii() is False:
                bad = [c for c in self.api_key if ord(c) > 127][:6]
                problems.append("LLM_API_KEY 含非 ASCII 字符 %s：HTTP 头只能放 ASCII，"
                                "通常是**把占位符（如「你的 Command Code Key」）当成了密钥**，"
                                "请到平台后台复制真实 Key" % "".join(bad))
            low = self.api_key.lower()
            if any(k in low for k in ("your", "你的", "changeme", "placeholder", "xxxx", "todo")):
                problems.append("LLM_API_KEY 看起来是占位符，不是真实密钥")
            if len(self.api_key) < 20:
                problems.append("LLM_API_KEY 长度可疑（%d 字符）：真实密钥通常更长" % len(self.api_key))
        if not self.base_url.isascii():
            problems.append("LLM_BASE_URL 含非 ASCII 字符")
        if not self.model.isascii():
            problems.append("LLM_MODEL 含非 ASCII 字符（模型名必须与 /models 列表完全一致）")
        if not self.base_url.startswith(("http://", "https://")):
            problems.append("LLM_BASE_URL 必须以 http(s):// 开头")
        return problems


@dataclass(frozen=True)
class DBConfig:
    """只读数据库连接（拆分字段，避免密码含特殊字符时 URL 转义出错）"""

    host: str = _env("DB_HOST", "127.0.0.1")
    port: int = int(_env("DB_PORT", "3306") or 3306)
    user: str = _env("DB_USER", "app_user")
    password: str = _env("DB_PASSWORD")
    name: str = _env("DB_NAME", "treatbord")

    @property
    def url(self) -> str:
        if IS_SQLITE:
            return "sqlite:///%s" % SQLITE_PATH
        """SQLAlchemy URL（密码做 URL 编码，避免 @ : / 等字符破坏结构）"""
        from urllib.parse import quote_plus

        return "mysql+pymysql://%s:%s@%s:%d/%s?charset=utf8mb4" % (
            quote_plus(self.user), quote_plus(self.password), self.host, self.port, self.name
        )

    @property
    def sqlite_path(self) -> str:
        """sqlite URL 形如 sqlite:///abs/path.sqlite → 取出绝对路径"""
        return self.url.split("///", 1)[-1]

    @property
    def label(self) -> str:
        if IS_SQLITE:
            return "sqlite:%s" % self.sqlite_path
        return "%s@%s:%d/%s" % (self.user, self.host, self.port, self.name)


@dataclass(frozen=True)
class GuardConfig:
    """三层护栏参数（SQL 安全）"""

    max_rows: int = int(_env("SQL_MAX_ROWS", "200") or 200)
    timeout_ms: int = int(_env("SQL_TIMEOUT_MS", "3000") or 3000)
    explain_row_limit: int = int(_env("SQL_EXPLAIN_ROW_LIMIT", "100000") or 100000)
    schema_top_k: int = int(_env("SCHEMA_TOP_K", "5") or 5)
    embed_backend: str = _env("EMBED_BACKEND", "bm25")


# 敏感列（结果脱敏黑名单）：这些列的值永不出现在返回结果里
SENSITIVE_COLUMNS = frozenset({"openid", "unionid", "password_hash", "ip"})

# 系统表（不参与 Schema 检索与白名单）
SYSTEM_TABLES = frozenset({"flyway_schema_history"})

# ---- 成本优化开关 ----
CACHE_ENABLED = _env("CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
CACHE_TTL_SEC = int(_env("CACHE_TTL_SEC", "600") or 600)          # 问题→SQL 缓存有效期
SUMMARY_TEMPLATE_FIRST = _env("SUMMARY_TEMPLATE_FIRST", "true").lower() in ("1", "true", "yes")
SCHEMA_MAX_COLUMNS = int(_env("SCHEMA_MAX_COLUMNS", "0") or 0)   # 每张表最多注入多少列（0=不限制）
SCHEMA_DESC_MAX = int(_env("SCHEMA_DESC_MAX", "0") or 0)         # 列描述截断长度（0=不截断）

# 后端：mysql（真实库，用于完整评估）或 sqlite（公开演示快照，随仓库分发）
DB_BACKEND = _env("DB_BACKEND", "mysql").lower()
IS_SQLITE = DB_BACKEND == "sqlite"
SQL_DIALECT = "sqlite" if IS_SQLITE else "mysql"
SQLITE_PATH = _env("SQLITE_PATH", str(ROOT / "data" / "snapshot.sqlite"))

LLM = LLMConfig()
DB = DBConfig()
GUARD = GuardConfig()

SCHEMA_PATH = ROOT / "data" / "schema.json"
INDEX_PATH = ROOT / "data" / "schema_index.json"
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()

# 语义层范围判定是否启用 LLM 兜底（默认关闭）。
# 关闭的理由：判定必须可复现（同一问题同一结论），规则层已覆盖明确的越权问法；
# 兜底只在"规则一条都没命中"时介入 —— 它提高召回但会引入采样不确定性，
# 属于按场景选择的开关（行业/合规要求更严时可开）。
SCOPE_LLM_FALLBACK = _env("SCOPE_LLM_FALLBACK", "false").lower() in ("1", "true", "yes")


def setup_logging() -> None:
    """统一日志格式与级别（.env 的 LOG_LEVEL 可调；DEBUG 会打印每条执行明细）"""
    import sys

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=LOG_LEVEL,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")