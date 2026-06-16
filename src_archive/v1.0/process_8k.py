"""
process_8k.py — 8-K 事件检测 + LLM 中文摘要

流程:
1. 从 SQLite 找出未处理的 8-K filing
2. 下载 8-K 正文文本
3. 按 Item 号分级（严重/关注/信息）
4. 调用 Claude API 生成中文摘要（API key 从环境变量取）
5. 若无 API key，退化为规则摘要
6. 存入 events 表，标记 filing 已处理

用法:
    python process_8k.py                     # 处理全部未处理的 8-K
    python process_8k.py --ticker CVNA         # 只处理一家
    python process_8k.py --dry-run             # 只看不写
    python process_8k.py --no-llm              # 跳过 LLM，只用规则摘要
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import textwrap
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

# ============================================================================
# 配置
# ============================================================================

HEADERS = {
    "User-Agent": "CompetitorIntel/1.0 (your-email@company.com)",
    "Accept-Encoding": "gzip, deflate",
}

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "competitor_intel.db"

# Claude API 配置
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
LLM_MODEL = "claude-sonnet-4-6"  # 当前最新 Sonnet

# SEC 请求间隔
RATE_LIMIT = 0.3
LLM_RATE_LIMIT = 2.0  # LLM API 调用间隔

# ============================================================================
# 8-K Item 分级规则（PRD 3.4 节）
# ============================================================================

ITEM_SEVERITY: dict[str, tuple[str, str]] = {
    "1.01": ("high",   "重大合同/合作"),
    "1.02": ("high",   "合同终止"),
    "1.03": ("high",   "破产/接管"),
    "2.02": ("high",   "业绩发布"),
    "2.03": ("high",   "重大财务义务"),
    "5.01": ("high",   "控制权变更"),
    "5.02": ("high",   "高管/董事变动"),
    # --- medium ---
    "3.01": ("medium", "退市/转板通知"),
    "3.02": ("medium", "权益出售"),
    "3.03": ("medium", "股东权利变更"),
    "4.01": ("medium", "审计师变更"),
    "5.03": ("medium", "章程/制度修改"),
    "5.07": ("medium", "股东投票结果"),
    # --- low ---
    "7.01": ("low",    "Regulation FD 披露"),
    "8.01": ("low",    "其他事件"),
    "9.01": ("low",    "财报附件"),
}

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def classify_8k_items(items_str: str) -> dict:
    """
    解析 8-K items 字段，返回最高严重级别和事件类型。
    items_str 格式如 "2.02,9.01" 或 "5.02,5.03,5.07,9.01"
    """
    item_list = [i.strip() for i in items_str.split(",") if i.strip()] if items_str else []

    severities = []
    event_types = []
    for item in item_list:
        info = ITEM_SEVERITY.get(item)
        if info:
            severities.append(info[0])
            event_types.append(info[1])

    if not severities:
        return {"severity": "low", "event_types": event_types or ["未知事件"], "items": item_list}

    # 取最高严重级别
    if "high" in severities:
        highest = "high"
    elif "medium" in severities:
        highest = "medium"
    else:
        highest = "low"

    return {
        "severity": highest,
        "event_types": event_types,
        "items": item_list,
    }


# ============================================================================
# 数据库
# ============================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_events_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            filing_id INTEGER NOT NULL REFERENCES filings(id),
            event_type TEXT,
            severity TEXT,
            summary_cn TEXT,
            raw_text TEXT,
            source TEXT DEFAULT 'sec_8k',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_company_severity
            ON events(company_id, severity, created_at DESC);
    """)


def get_unprocessed_8ks(conn: sqlite3.Connection, ticker: str | None = None):
    """获取未处理的 8-K filing 列表。"""
    query = """
        SELECT f.id, f.company_id, c.ticker, c.cik, c.name,
               f.accession_number, f.form_type, f.filing_date, f.items
        FROM filings f
        JOIN companies c ON f.company_id = c.id
        WHERE f.form_type IN ('8-K', '8-K/A')
          AND f.is_processed = 0
    """
    params: list = []
    if ticker:
        query += " AND c.ticker = ?"
        params.append(ticker)

    query += " ORDER BY f.filing_date DESC"
    return conn.execute(query, params).fetchall()


def upsert_event(conn: sqlite3.Connection, company_id: int, filing_id: int,
                 event_type: str, severity: str, summary: str,
                 raw_text: str, source: str = "sec_8k") -> bool:
    """插入事件，如果 (company_id, filing_id) 已存在则不重复。"""
    cur = conn.execute("""
        INSERT OR IGNORE INTO events (company_id, filing_id, event_type,
                                      severity, summary_cn, raw_text, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (company_id, filing_id, event_type, severity, summary, raw_text, source))
    conn.commit()
    return cur.rowcount > 0


def mark_filing_processed(conn: sqlite3.Connection, filing_id: int):
    conn.execute("UPDATE filings SET is_processed = 1, updated_at = datetime('now') WHERE id = ?",
                 (filing_id,))
    conn.commit()


# ============================================================================
# SEC Filing 文本获取
# ============================================================================

class MLStripper(HTMLParser):
    """去除 HTML 标签，保留文本内容。"""
    def __init__(self):
        super().__init__()
        self.reset()
        self.text: list[str] = []

    def handle_data(self, d):
        self.text.append(d)

    def get_text(self) -> str:
        return "".join(self.text)


def strip_html(html: str) -> str:
    s = MLStripper()
    s.feed(html)
    return s.get_text()


def fetch_8k_text(cik_padded: str, accession_number: str) -> str | None:
    """
    下载 8-K filing 完整文本。

    SEC EDGAR .txt 完整提交文件包含 SGML 头 + 文档正文。
    URL 格式: /Archives/edgar/data/{cik_no_pad}/{acc_no_dash}/{accession}.txt
    """
    cik_no_pad = cik_padded.lstrip("0")
    acc_no_dash = accession_number.replace("-", "")

    url = (f"https://www.sec.gov/Archives/edgar/data/"
           f"{cik_no_pad}/{acc_no_dash}/{accession_number}.txt")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 404:
            return None  # 不是致命的，有些 filing 可能没有 .txt
        raise
    except requests.exceptions.RequestException as e:
        print(f"    ⚠️ 网络错误: {e}")
        return None


def extract_8k_body(raw_text: str) -> str:
    """
    从完整提交文本中提取 8-K 正文。

    SEC 完整提交格式包含多个 <DOCUMENT> 块：
    - <TYPE>8-K     → cover sheet（通常是套话，内容少）
    - <TYPE>EX-99.1 → 股东信 / 详细业绩说明（最有价值）
    - <TYPE>EX-99.2 → 新闻稿（简洁版）
    - <TYPE>GRAPHIC → 图片（跳过）
    - <TYPE>EX-101.* → XBRL 数据（跳过）

    策略：优先拼接 EX-99.1 + EX-99.2 的内容。
    """
    # 提取所有 DOCUMENT 块
    doc_pattern = re.compile(
        r'<DOCUMENT>(.*?)</DOCUMENT>', re.DOTALL | re.IGNORECASE
    )
    type_pattern = re.compile(r'<TYPE>(.*?)(?:\r?\n|\r)', re.IGNORECASE)
    text_pattern = re.compile(r'<TEXT>(.*?)</TEXT>', re.DOTALL | re.IGNORECASE)

    # 要提取的 exhibit 类型（按优先级）
    priority_types = ["EX-99.1", "EX-99.2", "8-K"]

    docs_found: dict[str, str] = {}
    for doc_match in doc_pattern.finditer(raw_text):
        doc_content = doc_match.group(1)
        type_match = type_pattern.search(doc_content)
        text_match = text_pattern.search(doc_content)
        if not type_match or not text_match:
            continue
        dtype = type_match.group(1).strip()
        if dtype not in priority_types:
            continue
        # 避免重复（同类型只取第一个）
        if dtype not in docs_found:
            docs_found[dtype] = text_match.group(1)

    # 按优先级拼接
    parts = []
    for dtype in priority_types:
        if dtype in docs_found:
            html = docs_found[dtype]
            clean = strip_html(html)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if len(clean) > 50:  # 过滤掉空/过短的内容
                parts.append(clean)

    if not parts:
        # 回退：提取第一个 <TEXT> 块
        text_match = re.search(r'<TEXT>(.*?)</TEXT>', raw_text, re.DOTALL | re.IGNORECASE)
        if text_match:
            clean = strip_html(text_match.group(1))
            clean = re.sub(r'\s+', ' ', clean).strip()
            parts.append(clean)

    full_text = "\n\n".join(parts)

    # 限制总长度（LLM 上下文有限）
    if len(full_text) > 6000:
        full_text = full_text[:6000] + "..."

    return full_text


# ============================================================================
# LLM 摘要
# ============================================================================

LLM_PROMPT_TEMPLATE = """你是二手车行业的竞品分析师。以下是 {company_name} 的 8-K filing 正文（Item {items}）。

请用 2-3 句中文总结关键内容，并给出"重要性评级"（高/中/低）。
如果涉及数字（金额、百分比、日期），必须在摘要中保留。

请按以下格式输出：
重要性: [高/中/低]
摘要: [你的中文摘要]"""


def call_claude_api(prompt: str, max_tokens: int = 500) -> str | None:
    """调用 Claude API 生成摘要。"""
    if not ANTHROPIC_API_KEY:
        return None

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as e:
        print(f"    ⚠️ LLM API 调用失败: {e}")
        return None


def parse_llm_response(response: str) -> dict:
    """解析 LLM 返回的文本，提取重要性评级和摘要。"""
    severity_map = {"高": "high", "中": "medium", "低": "low"}

    severity = "medium"  # default
    summary = response

    # 尝试匹配 "重要性: 高" 格式
    sev_match = re.search(r'重要性[：:]\s*(高|中|低)', response)
    if sev_match:
        severity = severity_map.get(sev_match.group(1), "medium")

    # 尝试匹配 "摘要:" 后的内容
    summary_match = re.search(r'摘要[：:]\s*(.*)', response, re.DOTALL)
    if summary_match:
        summary = summary_match.group(1).strip()
    else:
        # 去掉 "重要性:" 行
        summary = re.sub(r'重要性[：:].*?\n', '', response).strip()

    return {"severity": severity, "summary": summary}


def generate_summary(company_name: str, items_str: str, raw_text: str,
                     use_llm: bool = True) -> dict:
    """生成 8-K 事件摘要。"""
    if use_llm and ANTHROPIC_API_KEY:
        prompt = LLM_PROMPT_TEMPLATE.format(
            company_name=company_name,
            items=items_str,
        )
        # 把正文附在 prompt 后面
        full_prompt = f"{prompt}\n\n8-K 正文:\n{raw_text[:2500]}"

        llm_resp = call_claude_api(full_prompt)
        if llm_resp:
            return parse_llm_response(llm_resp)

    # 回退：规则摘要
    return rule_based_summary(company_name, items_str, raw_text)


def rule_based_summary(company_name: str, items_str: str, raw_text: str) -> dict:
    """无 LLM 时的规则兜底摘要。"""
    item_list = [i.strip() for i in items_str.split(",") if i.strip()]
    item_descs = []
    for item in item_list:
        info = ITEM_SEVERITY.get(item)
        if info:
            item_descs.append(f"Item {item} ({info[1]})")
        else:
            item_descs.append(f"Item {item}")

    # 尝试从文本中提取金额
    money_pattern = r'\$[\d,]+(?:\s?(?:million|billion|M|B))?'
    money_matches = re.findall(money_pattern, raw_text[:500], re.IGNORECASE)
    money_str = f"，涉及金额: {', '.join(money_matches[:3])}" if money_matches else ""

    classification = classify_8k_items(items_str)

    summary = (
        f"{company_name} 于文件中披露了: {'; '.join(item_descs)}。"
        f"严重级别: {classification['severity']}。"
        f"{money_str}"
        f"（规则摘要，建议配置 ANTHROPIC_API_KEY 环境变量获取 LLM 摘要）"
    )

    return {
        "severity": classification["severity"],
        "summary": summary,
    }


# ============================================================================
# 展示
# ============================================================================

def print_event(ticker: str, company_name: str, filing_date: str, items_str: str,
                severity: str, summary: str, llm_used: bool):
    """打印单个事件卡片。"""
    emoji = SEVERITY_EMOJI.get(severity, "⚪")
    llm_tag = "🤖 LLM" if llm_used else "📋 规则"

    print(f"\n  {'─'*74}")
    print(f"  {emoji} {ticker} — {company_name}")
    print(f"  📅 {filing_date}  |  Items: {items_str}  |  {llm_tag}")
    print(f"  {'─'*74}")
    # 折行显示摘要
    wrapped = textwrap.fill(summary, width=72, initial_indent="  ", subsequent_indent="  ")
    print(wrapped)


# ============================================================================
# 主流程
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="8-K 事件检测 + LLM 摘要")
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写入数据库")
    parser.add_argument("--no-llm", action="store_true",
                        help="强制使用规则摘要（跳过 LLM）")
    parser.add_argument("--limit", type=int, default=10,
                        help="最多处理条数（默认 10）")
    args = parser.parse_args()

    use_llm = not args.no_llm
    if use_llm and not ANTHROPIC_API_KEY:
        print("💡 ANTHROPIC_API_KEY 未设置，将使用规则摘要。")
        print("   export ANTHROPIC_API_KEY=sk-ant-... 以获得更准确的 LLM 摘要。\n")
        use_llm = False

    conn = None if args.dry_run else get_db()
    if conn:
        init_events_table(conn)

    filings = get_unprocessed_8ks(conn, args.ticker) if conn else []
    if not filings and not args.dry_run:
        print("✅ 没有未处理的 8-K filing。")
        if conn:
            conn.close()
        return

    # dry-run 模式：从数据库读（不判断 is_processed）
    if args.dry_run:
        dry_conn = get_db()
        query = """
            SELECT f.id, f.company_id, c.ticker, c.cik, c.name,
                   f.accession_number, f.form_type, f.filing_date, f.items
            FROM filings f
            JOIN companies c ON f.company_id = c.id
            WHERE f.form_type IN ('8-K', '8-K/A')
        """
        params: list = []
        if args.ticker:
            query += " AND c.ticker = ?"
            params.append(args.ticker)
        query += " ORDER BY f.filing_date DESC LIMIT ?"
        params.append(args.limit)
        filings = dry_conn.execute(query, params).fetchall()
        dry_conn.close()

    # 限制数量
    filings = filings[:args.limit]

    total_processed = 0
    for row in filings:
        filing_id, company_id, ticker, cik, name, acc_no, form, fdate, items = row

        # 1. 分级
        classification = classify_8k_items(items)

        print(f"\n🔍 [{ticker}] {fdate} 8-K — Items: {items or '(无)'} "
              f"→ 级别: {classification['severity']}")

        # 2. 下载 8-K 正文
        raw_text = fetch_8k_text(cik, acc_no)
        if raw_text:
            body_text = extract_8k_body(raw_text)
            print(f"    📄 原文获取成功 ({len(body_text)} chars)")
        else:
            body_text = f"[无法获取 8-K 正文] accession={acc_no}"
            print(f"    ⚠️ 无法获取原文 (accession={acc_no})")

        # 3. LLM 摘要
        summary_result = generate_summary(name, items, body_text, use_llm=use_llm)
        llm_severity = summary_result["severity"]
        summary_text = summary_result["summary"]

        # 综合严重级别（取规则分级和 LLM 分级的较高者）
        sev_order = {"high": 3, "medium": 2, "low": 1}
        final_severity = "high" if sev_order.get(classification["severity"], 0) >= 3 or \
                                     sev_order.get(llm_severity, 0) >= 3 else \
                         "medium" if sev_order.get(classification["severity"], 0) >= 2 or \
                                     sev_order.get(llm_severity, 0) >= 2 else \
                         "low"

        # 4. 展示
        event_type_str = ", ".join(classification["event_types"])
        llm_used = use_llm and ANTHROPIC_API_KEY and "规则摘要" not in summary_text
        print_event(ticker, name, fdate, items, final_severity, summary_text, llm_used)

        # 5. 存入数据库
        if conn:
            upsert_event(conn, company_id, filing_id, event_type_str,
                        final_severity, summary_text, body_text)
            mark_filing_processed(conn, filing_id)
            total_processed += 1

        time.sleep(RATE_LIMIT)
        if llm_used:
            time.sleep(LLM_RATE_LIMIT)

    if conn:
        conn.close()
        print(f"\n{'='*80}")
        print(f"  ✅ 完成！处理了 {total_processed} 条 8-K filing")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
