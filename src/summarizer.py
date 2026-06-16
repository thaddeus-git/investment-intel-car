"""
竞品情报监控系统 — LLM 摘要 & EC 纪要模块

职责：
1. 检测新 8-K/6-K → 规则分级 + LLM 中文摘要 → 写入 events 表
2. 检测新 10-Q/K → LLM 生成 EC 纪要 → 写入 earnings_call_notes 表
3. API Key 缺失时自动退化为规则摘要 / 跳过 EC 纪要

模型：
- DeepSeek Flash → 8-K 摘要（便宜快）
- DeepSeek Pro   → EC 纪要（推理强）
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime

from config import (
    COMPETITORS,
    DATABASE_PATH,
    EDGAR_IDENTITY,
    LLM_API_BASE,
    LLM_API_KEY_DEEP,
    LLM_API_KEY_SUMMARY,
    LLM_MODEL_DEEP,
    LLM_MODEL_SUMMARY,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ═══════════════════════════════════════════════════════════
# 8-K Item 分级规则（PRD v1.0 §3.4）
# ═══════════════════════════════════════════════════════════

ITEM_SEVERITY = {
    # 🔴 严重
    "1.01": ("high",   "重大合同/合作终止"),
    "1.02": ("high",   "重大合同终止"),
    "2.01": ("high",   "重大收购/处置完成"),
    "2.02": ("high",   "业绩发布（超预期/暴雷）"),
    "4.01": ("high",   "审计师变更"),
    "4.02": ("high",   "重述财务报表"),
    "5.01": ("high",   "控制权变更"),
    "5.02": ("high",   "高管/董事离职或任命"),
    # 🟡 关注
    "3.01": ("medium", "退市/转板通知"),
    "3.02": ("medium", "股票发行"),
    "3.03": ("medium", "股东权利变更"),
    "5.03": ("medium", "章程/制度修改"),
    "5.07": ("medium", "股东会投票结果"),
    "5.05": ("medium", "道德准则修订"),
    "8.01": ("medium", "其他重大事件"),
    # 🟢 信息
    "7.01": ("low",    "Regulation FD 披露"),
    "9.01": ("low",    "财报附件"),
}

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢", "info": "⚪"}


def classify_items(items_str):
    """
    解析 "2.02,9.01" 样式的 Item 字符串，返回 (最高严重级别, 事件类型字符串)。
    """
    if not items_str:
        return ("info", "未分类")

    parts = [s.strip() for s in items_str.split(",")]
    max_severity = "info"
    types = []

    for p in parts:
        # Normalize: "Item 2.02" → "2.02"
        p = p.replace("Item ", "").strip()
        sev, desc = ITEM_SEVERITY.get(p, ("info", "未分类"))
        types.append(f"{p}({desc})")
        # 优先级: high > medium > low > info
        if sev == "high":
            max_severity = "high"
        elif sev == "medium" and max_severity in ("low", "info"):
            max_severity = "medium"
        elif sev == "low" and max_severity == "info":
            max_severity = "low"

    return (max_severity, ", ".join(types))


# ═══════════════════════════════════════════════════════════
# DB helper
# ═══════════════════════════════════════════════════════════

def _get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ═══════════════════════════════════════════════════════════
# LLM 调用（DeepSeek / OpenAI 兼容 API）
# ═══════════════════════════════════════════════════════════

def _has_llm():
    """检测是否有 LLM API Key 可用。"""
    return bool(LLM_API_KEY_SUMMARY and LLM_API_KEY_SUMMARY.startswith("sk-"))


def _llm_chat(model, api_key, system_prompt, user_prompt, max_tokens=500):
    """
    通用 LLM 调用（OpenAI 兼容接口）。
    返回 response text，失败返回 None。
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai not installed, falling back to rule-based")
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
            temperature=0.3,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return None


# ═══════════════════════════════════════════════════════════
# 8-K 摘要
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT_8K = (
    "你是二手车行业的竞品分析师。你的任务是把 SEC 8-K filing 内容"
    "用 1-2 句中文总结核心信息。涉及金额、人名、日期必须保留。"
    "最后标注重要性评级（高/中/低）。"
    "只输出摘要文字，不要输出分析过程。"
)


def _rule_summary(name_cn, severity, event_type):
    """规则摘要（无 LLM 时的退化方案）。"""
    emoji = SEVERITY_EMOJI.get(severity, "⚪")
    level_cn = {"high": "严重", "medium": "关注", "low": "信息", "info": "其他"}.get(severity, "其他")
    return f"[{emoji}{level_cn}] {name_cn} 提交了 8-K/6-K 报告，涉及: {event_type}。详见 SEC EDGAR。"


def _generate_summary(name_cn, items, filing_text, severity, event_type):
    """生成中文摘要（LLM 优先，无 API 时退化规则）。"""
    if not _has_llm():
        return _rule_summary(name_cn, severity, event_type)

    # 截取前 8000 字符（节省 token）
    truncated = filing_text[:8000] if filing_text else ""

    user_prompt = (
        f"以下是 {name_cn} 的 8-K/6-K filing（Items: {items}）。\n\n"
        f"内容：\n{truncated}\n\n"
        f"请用 1-2 句中文总结核心内容。涉及数字、人名、日期必须保留。"
    )
    result = _llm_chat(
        model=LLM_MODEL_SUMMARY,
        api_key=LLM_API_KEY_SUMMARY,
        system_prompt=SYSTEM_PROMPT_8K,
        user_prompt=user_prompt,
        max_tokens=300,
    )
    if result:
        emoji = SEVERITY_EMOJI.get(severity, "⚪")
        return f"{emoji}{result}"
    return _rule_summary(name_cn, severity, event_type)


# ═══════════════════════════════════════════════════════════
# EC 纪要（Earnings Call Notes）
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT_EC = (
    "你是二手车行业的资深分析师。你的任务是从公司财报/8-K新闻稿文本中"
    "提取关键信息，生成结构化的 Earnings Call 纪要。"
    "严格遵守输出格式，提取不到的内容写「暂无」。"
    "所有财务数字必须保留原始数值，禁止编造。"
    "输出纯 Markdown，不要输出分析过程。"
)


def _generate_ec_note(name_cn, form_type, filing_text):
    """
    用 DeepSeek Pro 从 10-Q/K 或 8-K Item 2.02 文本生成 EC 纪要。
    """
    if not _has_llm() or not filing_text:
        return None

    truncated = filing_text[:12000]

    user_prompt = (
        f"请从以下 {name_cn} 的 {form_type} 文本中提取关键信息，按以下格式输出：\n\n"
        f"## 📊 财务亮点\n"
        f"- Revenue: [金额，注明同比变化]\n"
        f"- Net Income: [金额，注明同比变化]\n"
        f"- 其他关键指标\n\n"
        f"## 🗣️ 管理层陈述要点\n"
        f"1. [战略方向/市场判断]\n"
        f"2. [运营数据/里程碑]\n"
        f"3. [下季度/全年指引]\n\n"
        f"## ⚠️ 风险提示\n"
        f"- [从 Forward-looking statements 提取的关键风险]\n\n"
        f"---\n"
        f"财报/新闻稿文本：\n{truncated}"
    )

    result = _llm_chat(
        model=LLM_MODEL_DEEP,
        api_key=LLM_API_KEY_DEEP,
        system_prompt=SYSTEM_PROMPT_EC,
        user_prompt=user_prompt,
        max_tokens=1200,
    )
    return result


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def _fetch_filing_text(ticker, accession_number):
    """通过 edgartools 获取指定 filing 的 Markdown 文本。"""
    try:
        from edgar import Company, set_identity
        set_identity(EDGAR_IDENTITY)
        c = Company(ticker)
        for f in c.get_filings().latest(60):
            if f.accession_number == accession_number:
                return f.markdown()
    except Exception as e:
        logger.warning("Failed to fetch text for %s: %s", accession_number, e)
    return None


def summarize_8k():
    """
    处理未处理的 8-K / 6-K filings：
    1. 规则分级
    2. LLM 摘要（或退化规则摘要）
    3. 写入 events 表
    """
    conn = _get_db()

    rows = conn.execute("""
        SELECT f.id, f.company_id, f.accession_number, f.form_type,
               f.items, c.ticker, c.name_cn
        FROM filings f
        JOIN companies c ON f.company_id = c.id
        WHERE f.form_type IN ('8-K', '6-K')
          AND f.is_processed = 0
          AND f.items IS NOT NULL
        LIMIT 20
    """).fetchall()

    if not rows:
        logger.info("No new 8-K/6-K to summarize.")
        conn.close()
        return

    new_events = 0
    for fid, cid, acc, form_type, items, ticker, name_cn in rows:
        severity, event_type = classify_items(items)

        # 获取 filing 正文
        filing_text = _fetch_filing_text(ticker, acc)

        # 生成摘要
        summary = _generate_summary(name_cn, items, filing_text, severity, event_type)

        # 写入 events 表
        conn.execute("""
            INSERT INTO events (company_id, filing_id, event_type, severity, summary_cn, raw_text)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cid, fid, event_type, severity, summary, filing_text or ""))

        conn.execute("UPDATE filings SET is_processed = 1 WHERE id = ?", (fid,))
        new_events += 1
        logger.info("  ✅ %s (%s) → %s", name_cn, form_type, severity)

    conn.commit()
    logger.info("summarize_8k: %d events created.", new_events)
    conn.close()


def summarize_ec():
    """
    为最近未处理的 10-Q/K/20-F 生成 EC 纪要。
    """
    conn = _get_db()

    rows = conn.execute("""
        SELECT f.id, f.company_id, f.accession_number, f.form_type,
               f.filing_date, c.ticker, c.name_cn
        FROM filings f
        JOIN companies c ON f.company_id = c.id
        WHERE f.form_type IN ('10-Q', '10-K', '20-F')
          AND f.id NOT IN (
              SELECT filing_id FROM events WHERE event_type LIKE '%earnings%'
          )
        LIMIT 10
    """).fetchall()

    if not rows:
        logger.info("No new 10-Q/K/20-F for EC notes.")
        conn.close()
        return

    new_notes = 0
    for fid, cid, acc, form_type, filing_date, ticker, name_cn in rows:
        # 判断季度
        try:
            dt = datetime.strptime(str(filing_date), "%Y-%m-%d")
        except ValueError:
            dt = None

        filing_text = _fetch_filing_text(ticker, acc)
        if not filing_text:
            continue

        note = _generate_ec_note(name_cn, form_type, filing_text)
        if not note:
            continue

        fy = dt.year if dt else None
        fq = None

        conn.execute("""
            INSERT OR REPLACE INTO earnings_call_notes
                (company_id, fiscal_year, fiscal_quarter, source, full_text_md)
            VALUES (?, ?, ?, ?, ?)
        """, (cid, fy, fq, form_type, note))
        new_notes += 1
        logger.info("  📝 EC note: %s %s", name_cn, form_type)

    conn.commit()
    logger.info("summarize_ec: %d notes created.", new_notes)
    conn.close()


def summarize_new():
    """总入口：摘要 + EC 纪要。"""
    summarize_8k()
    summarize_ec()


if __name__ == "__main__":
    logger.info("LLM available: %s", _has_llm())
    summarize_new()
