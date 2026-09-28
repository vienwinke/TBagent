# -*- coding: utf-8 -*-
"""大模型端点探测：验证任意 OpenAI 兼容端点是否可用（含 Command Code Provider API）

检查项：
  1. 鉴权是否通过（GET /models）
  2. 可用模型列表（挑一个填进 .env 的 LLM_MODEL）
  3. 最小对话调用：内容 + usage（token）+ 延迟
  4. 是否支持 response_format={"type":"json_object"}（本项目做结构化输出依赖它；不支持则自动降级）
  5. 估算成本（按 .env 里的单价）

用法：
    python scripts/probe_endpoint.py                        # 用 .env 配置
    python scripts/probe_endpoint.py --base-url https://api.commandcode.ai/provider/v1 --model <名称>
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import LLM, setup_logging  # noqa: E402


def probe(base_url: str, model: str, api_key: str) -> int:
    from openai import OpenAI

    print("端点: %s" % base_url)
    print("模型: %s" % model)
    print("Key : %s" % ("已配置(%d 字符)" % len(api_key) if api_key else "【未配置】"))
    print("-" * 72)
    if not api_key:
        print("✗ 未配置 API Key：请在 .env 填 LLM_API_KEY（或 DEEPSEEK_API_KEY）后重试")
        return 2

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)
    ok = True

    # 1) /models
    models: list[str] = []
    try:
        resp = client.models.list()
        models = [m.id for m in resp.data]
        print("✓ 鉴权通过，可用模型 %d 个：" % len(models))
        for m in models[:40]:
            print("    - %s" % m)
        if len(models) > 40:
            print("    … 其余 %d 个省略" % (len(models) - 40))
        if model not in models:
            print("  ⚠️ .env 里的 LLM_MODEL=%s 不在列表中，请从上面挑一个" % model)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("✗ /models 调用失败: %s: %s" % (type(exc).__name__, str(exc)[:200]))
        print("  （部分平台不提供 /models，可忽略，继续测对话接口）")

    # 2) 最小对话调用
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "只回复两个字：可用"}],
            temperature=0, max_tokens=16)
        dt = time.time() - t0
        u = r.usage
        print("-" * 72)
        print("✓ 对话调用成功（%.2fs）" % dt)
        print("  返回: %s" % (r.choices[0].message.content or "").strip()[:60])
        if u:
            cost = (u.prompt_tokens or 0) / 1e6 * LLM.price_in + (u.completion_tokens or 0) / 1e6 * LLM.price_out
            print("  tokens: 输入 %s / 输出 %s / 合计 %s" % (u.prompt_tokens, u.completion_tokens, u.total_tokens))
            print("  本次成本估算: ¥%.6f（按 .env 单价 ¥%s/¥%s 每百万）" % (cost, LLM.price_in, LLM.price_out))
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("✗ 对话调用失败: %s: %s" % (type(exc).__name__, str(exc)[:300]))

    # 3) JSON 模式
    print("-" * 72)
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": '返回 JSON：{"ok": true}'}],
            temperature=0, max_tokens=32, response_format={"type": "json_object"})
        print("✓ 支持 response_format=json_object（结构化输出走硬约束）")
        print("  返回: %s" % (r.choices[0].message.content or "").strip()[:60])
    except Exception as exc:  # noqa: BLE001
        print("△ 不支持 response_format=json_object（%s）" % type(exc).__name__)
        print("  本项目已内置降级：仍会要求模型输出 JSON，并用正则提取首个 {...} 再解析 ✓")

    print("-" * 72)
    print("结论: %s" % ("端点可用，可以填进 .env 开跑 ✓" if ok else "存在问题，见上面错误信息"))
    return 0 if ok else 1


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="OpenAI 兼容端点探测")
    ap.add_argument("--base-url", default=LLM.base_url)
    ap.add_argument("--model", default=LLM.model)
    ap.add_argument("--api-key", default=LLM.api_key)
    args = ap.parse_args()
    raise SystemExit(probe(args.base_url, args.model, args.api_key))


if __name__ == "__main__":
    main()