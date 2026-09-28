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


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class LLMConfig:
    """大模型接入（OpenAI 兼容）：换平台只改 .env 的 BASE_URL / MODEL"""

    api_key: str = _env("DEEPSEEK_API_KEY")
    base_url: str = _env("LLM_BASE_URL", "https://api.deepseek.com")
    model: str = _env("LLM_MODEL", "deepseek-chat")
    model_cheap: str = _env("LLM_MODEL_CHEAP", "deepseek-chat")
    timeout: float = float(_env("LLM_TIMEOUT", "60") or 60)
    max_retries: int = int(_env("LLM_MAX_RETRIES", "3") or 3)
    # 单价（元 / 百万 token）：用于成本统计，换模型时同步更新
    price_in: float = float(_env("LLM_PRICE_IN", "2.0") or 2.0)
    price_out: float = float(_env("LLM_PRICE_OUT", "9.0") or 9.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


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
        """SQLAlchemy URL（密码做 URL 编码，避免 @ : / 等字符破坏结构）"""
        from urllib.parse import quote_plus

        return "mysql+pymysql://%s:%s@%s:%d/%s?charset=utf8mb4" % (
            quote_plus(self.user), quote_plus(self.password), self.host, self.port, self.name
        )

    @property
    def label(self) -> str:
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

LLM = LLMConfig()
DB = DBConfig()
GUARD = GuardConfig()

SCHEMA_PATH = ROOT / "data" / "schema.json"
INDEX_PATH = ROOT / "data" / "schema_index.json"
