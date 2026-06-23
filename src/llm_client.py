"""
竞品情报监控系统 — LLM 调用公共模块

统一 LLM API 调用入口，消除 summarizer.py / insider_tracker.py 中的重复代码。
支持 OpenAI 兼容接口（当前后端：DeepSeek）。
"""

import logging

from config import (
    LLM_API_BASE,
    LLM_API_KEY_DEEP,
    LLM_API_KEY_SUMMARY,
    LLM_MODEL_DEEP,
    LLM_MODEL_SUMMARY,
)

logger = logging.getLogger(__name__)


def has_llm(key=None):
    """检测是否有可用的 LLM API Key。

    Args:
        key: 指定要检查的 key 字符串。不传则默认检查 LLM_API_KEY_SUMMARY。

    Returns:
        bool: 是否有有效的 API key（非空且以 "sk-" 开头）。
    """
    k = key or LLM_API_KEY_SUMMARY
    return bool(k and k.startswith("sk-"))


def chat(
    model=None,
    api_key=None,
    system_prompt="",
    user_prompt="",
    max_tokens=500,
    temperature=0.3,
):
    """通用 LLM 调用（OpenAI 兼容接口）。

    Args:
        model:     模型名。默认 LLM_MODEL_SUMMARY (Flash)。
        api_key:   API key。默认 LLM_API_KEY_SUMMARY。
        system_prompt: 系统提示词。
        user_prompt:   用户提示词。
        max_tokens:    最大输出 token 数，默认 500。
        temperature:   生成温度，默认 0.3。

    Returns:
        str or None: 模型返回的文本，失败返回 None。
    """
    model = model or LLM_MODEL_SUMMARY
    api_key = api_key or LLM_API_KEY_SUMMARY

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai not installed, LLM call skipped")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=LLM_API_BASE)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return None


# ── 便捷别名（向后兼容旧的函数名） ──
def chat_summary(system_prompt, user_prompt, max_tokens=300):
    """用 Flash 模型快速摘要（8-K / Insider）。"""
    return chat(
        model=LLM_MODEL_SUMMARY,
        api_key=LLM_API_KEY_SUMMARY,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
    )


def chat_deep(system_prompt, user_prompt, max_tokens=1200):
    """用 Pro 模型深度分析（EC 纪要）。"""
    return chat(
        model=LLM_MODEL_DEEP,
        api_key=LLM_API_KEY_DEEP,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
    )
