# -*- coding: utf-8 -*-
"""发布前密钥门禁回归。

背景：26bc0b2 把一把真实密钥写进了 .env.example，而当时门禁脚本已经存在
（a4215c7 引入）却从未被任何流程调用 —— 门禁不是没能力，是没人跑。
所以这里既测"能拦什么"，也测"当前仓库本身是绿的"（常年误报等于没有门禁）。

注意：用例里的假密钥一律运行时拼接。写成字面量的话，测试文件自己会被门禁拦下。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.secret_scan import is_placeholder, scan_secrets

ROOT = Path(__file__).resolve().parents[1]

# CommandCode 形态（user_ 前缀）—— 纯 sk- 正则漏掉的就是这种
FAKE_CC_KEY = "user_" + "a1b2c3d4" * 8
FAKE_SK_KEY = "sk-" + "f9e8d7c6" * 4
# 同样运行时拼接：这个用例一旦写成字面量，门禁会把它自己拦下（实测踩到过一次）
FAKE_PASSWORD_LINE = "password" + " = " + '"' + "hunter2" * 3 + '"'


def test_catches_non_sk_prefix_provider_key():
    hits = scan_secrets("LLM_API_KEY=%s\n" % FAKE_CC_KEY)
    assert hits, "非 sk- 前缀的供应商密钥必须被拦"
    assert hits[0][0] == "环境变量里的真实密钥"


def test_catches_sk_style_key():
    hits = scan_secrets('LLM_API_KEY="%s"\n' % FAKE_SK_KEY)
    assert any(desc == "OpenAI/DeepSeek 风格密钥" for desc, _ in hits)


def test_catches_hardcoded_password():
    hits = scan_secrets(FAKE_PASSWORD_LINE + "\n")
    assert any(desc == "硬编码密码" for desc, _ in hits)


def test_placeholders_are_allowed():
    """占位符不能误报：.env.example 被误报过，门禁常年红→没人再看。"""
    texts = [
        "LLM_API_KEY=your-command-code-key\n",
        "LLM_API_KEY=<YOUR_KEY_HERE>\n",
        "LLM_API_KEY=changeme\n",
        "LLM_API_KEY=" + "sk-" + "x" * 32 + "\n",
        'password = "your-password-here"\n',
    ]
    for text in texts:
        assert scan_secrets(text) == [], text


def test_is_placeholder_boundaries():
    assert is_placeholder("") is True
    assert is_placeholder("your-key") is True
    assert is_placeholder('"PLACEHOLDER"') is True
    assert is_placeholder(FAKE_CC_KEY) is False


def test_current_repository_passes_the_gate():
    """门禁对当前仓库必须是绿的，否则它会退化成"没人跑的脚本"。"""
    r = subprocess.run([sys.executable, "scripts/prepublish_check.py", "--secrets-only"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, "密钥门禁红灯：\n%s\n%s" % (r.stdout, r.stderr)
