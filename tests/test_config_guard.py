# -*- coding: utf-8 -*-
"""配置自检单测：把"把占位符当密钥"这类部署事故变成可读报错（云端实测事故）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import LLMConfig  # noqa: E402


def test_empty_key():
    assert any("LLM_API_KEY 为空" in i for i in LLMConfig(api_key="").issues())


def test_non_ascii_key_detected():
    assert any("非 ASCII" in i for i in LLMConfig(api_key="你的 Command Code Key").issues())


def test_placeholder_key_detected():
    assert any("占位符" in i for i in LLMConfig(api_key="your-api-key-here-123456").issues())


def test_short_key_detected():
    assert any("长度可疑" in i for i in LLMConfig(api_key="sk-abc").issues())


def test_ok_key_has_no_issues():
    assert LLMConfig(api_key="sk-" + "a1b2c3d4" * 6).issues() == []


def test_bad_base_url_and_model():
    issues = LLMConfig(api_key="sk-" + "a" * 40, base_url="api.example.com", model="模型名").issues()
    assert any("http" in i for i in issues)
    assert any("LLM_MODEL" in i for i in issues)
