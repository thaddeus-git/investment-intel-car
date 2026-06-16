"""
竞品情报监控系统 — 内部人交易追踪（Phase 2）

Form 4: Statement of Changes in Beneficial Ownership
  - 高管/董事/大股东交易后 2 个工作日内申报
  - 从 XML 中提取交易代码、股数、价格、买卖方向
  - 字段来源：edgartools `Company().get_filings(form="4")` + XML 解析

Form 144: Notice of Proposed Sale of Securities
  - 内部人大额减持计划事前通知（≥5,000 股或 ≥$50,000）
  - 从 XML + markdown 提取计划出售股数、预估金额

输出：内部人情绪指标 (Insider Sentiment Index)，写入 SQLite。
"""

import logging
import re
import sqlite3
from datetime import datetime, timedelta

from edgar import Company, set_identity

from config import (
    COMPETITORS,
    DATABASE_PATH,
    EDGAR_IDENTITY,
    LLM_API_BASE,
    LLM_API_KEY_SUMMARY,
    LLM_MODEL_SUMMARY,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ═══════════════════════════════════════════════════════════
# Form 4: 内部人角色权重（用于情绪指标）
# ═══════════════════════════════════════════════════════════

ROLE_WEIGHTS = {
    "CEO": 2.0,
    "CFO": 2.0,
    "Director": 1.0,
    "Officer": 1.0,
    "VP": 0.8,
    "SVP": 0.8,
    "EVP": 0.8,
    "10% Owner": 1.5,
    "Other": 0.5,
}

# 交易代码 → 方向
TRANSACTION_DIRECTION = {
    "P": "buy",       # Purchase (open market)
    "S": "sell",      # Sale (open market)
    "A": "buy",       # Award/Grant (compensation, 视为买入信号)
    "D": "sell",      # Disposition back to issuer
    "F": "sell",      # Tax withholding (technically a sale)
    "M": "buy",       # Exercise of options (视为买入信号)
    "G": "neutral",   # Gift
    "I": "buy",       # Discretionary (DRIP)
    "J": "neutral",   # Other
    "K": "neutral",   # Equity swap
    "U": "neutral",   # Tender offer
    "W": "neutral",   # Will/Inheritance
}


def _get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ═══════════════════════════════════════════════════════════
# 数据库表
# ═══════════════════════════════════════════════════════════

def init_insider_tables(conn):
    """建 insider 相关表（幂等）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS insider_transactions (
            id                INTEGER PRIMARY KEY,
            company_id        INTEGER REFERENCES companies(id),
            accession_number  TEXT,
            filing_date       DATE,
            owner_name        TEXT,
            is_director       BOOLEAN DEFAULT 0,
            is_officer        BOOLEAN DEFAULT 0,
            is_ten_percent_owner BOOLEAN DEFAULT 0,
            security_title    TEXT,
            transaction_code  TEXT,
            acquired_disposed TEXT,
            shares            REAL,
            price_per_share   REAL,
            total_value       REAL,
            shares_owned_after REAL,
            transaction_date  DATE,
            is_derivative     BOOLEAN DEFAULT 0,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(accession_number, owner_name, transaction_date, transaction_code,
                   security_title, shares)
        );

        CREATE TABLE IF NOT EXISTS form144_filings (
            id                  INTEGER PRIMARY KEY,
            company_id          INTEGER REFERENCES companies(id),
            accession_number    TEXT UNIQUE,
            filing_date         DATE,
            seller_name         TEXT,
            securities_class    TEXT,
            shares_to_sell      REAL,
            aggregate_market_value REAL,
            broker_name         TEXT,
            approximate_sale_date TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS insider_sentiment (
            id            INTEGER PRIMARY KEY,
            company_id    INTEGER REFERENCES companies(id),
            period_start  DATE,
            period_end    DATE,
            buy_count     INTEGER DEFAULT 0,
            sell_count    INTEGER DEFAULT 0,
            buy_value     REAL DEFAULT 0,
            sell_value    REAL DEFAULT 0,
            sentiment_score REAL DEFAULT 0,
            sentiment_label TEXT,   -- bullish / neutral / bearish
            summary_cn    TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_id, period_start, period_end)
        );
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════════
# Form 4 XML 解析
# ═══════════════════════════════════════════════════════════

def _parse_form4_xml(xml_text):
    """
    从 Form 4 XML 中提取交易记录。

    XML 结构（EDGAR Form 4 Submission）:
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2026-06-10</value></transactionDate>
          <transactionCoding>
            <transactionCode>S</transactionCode>
          </transactionCoding>
          <transactionAmounts>
            <transactionShares><value>14525</value></transactionShares>
            <transactionPricePerShare><value>70.00</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
          <postTransactionAmounts>
            <sharesOwnedFollowingTransaction><value>214960</value></sharesOwnedFollowingTransaction>
          </postTransactionAmounts>
        </nonDerivativeTransaction>
      </nonDerivativeTable>

    Returns: list of dicts with transaction data
    """
    results = []

    owner_name = ""
    name_match = re.search(r"<rptOwnerName>(.*?)</rptOwnerName>", xml_text, re.DOTALL)
    if name_match:
        owner_name = name_match.group(1).strip()

    is_director = bool(re.search(r"<isDirector>1</isDirector>", xml_text))
    is_officer = bool(re.search(r"<isOfficer>1</isOfficer>", xml_text))
    is_10p = bool(re.search(r"<isTenPercentOwner>1</isTenPercentOwner>", xml_text))

    # 确定角色（用于权重计算）
    if is_10p:
        role = "10% Owner"
    elif is_director and not is_officer:
        role = "Director"
    elif is_officer:
        # Try to get title from XML
        title_match = re.search(r"<officerTitle>(.*?)</officerTitle>", xml_text, re.DOTALL)
        if title_match:
            title = title_match.group(1).strip().upper()
            if "CEO" in title or "CHIEF EXECUTIVE" in title:
                role = "CEO"
            elif "CFO" in title or "CHIEF FINANCIAL" in title:
                role = "CFO"
            elif "EVP" in title or "EXECUTIVE VICE" in title:
                role = "EVP"
            elif "SVP" in title or "SENIOR VICE" in title:
                role = "SVP"
            elif "VP" in title or "VICE PRESIDENT" in title:
                role = "VP"
            else:
                role = "Officer"
        else:
            role = "Officer"
    elif is_director and is_officer:
        role = "Director"
    else:
        role = "Other"

    def _extract_value(text, tag):
        """从 XML 中提取 <tag><value>...</value></tag> 的内容。"""
        pattern = rf"<{tag}>\s*(?:<value>)?\s*([^<\s][^<]*?)\s*(?:</value>)?\s*</{tag}>"
        m = re.search(pattern, text, re.DOTALL)
        if m:
            val = m.group(1).strip().replace(",", "")
            return val
        return None

    def _safe_float(val):
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # Parse non-derivative transactions
    nd_blocks = re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
        xml_text, re.DOTALL,
    )
    for block in nd_blocks:
        txn = {
            "owner_name": owner_name,
            "role": role,
            "is_director": is_director,
            "is_officer": is_officer,
            "is_ten_percent_owner": is_10p,
            "is_derivative": False,
            "security_title": _extract_value(block, "securityTitle"),
            "transaction_code": _extract_value(block, "transactionCode"),
            "acquired_disposed": _extract_value(block, "transactionAcquiredDisposedCode"),
            "shares": _safe_float(_extract_value(block, "transactionShares")),
            "price_per_share": _safe_float(_extract_value(block, "transactionPricePerShare")),
            "shares_owned_after": _safe_float(_extract_value(block, "sharesOwnedFollowingTransaction")),
            "transaction_date": _extract_value(block, "transactionDate"),
        }
        # Compute total value
        if txn["shares"] and txn["price_per_share"]:
            txn["total_value"] = txn["shares"] * txn["price_per_share"]
        else:
            txn["total_value"] = None
        results.append(txn)

    # Parse derivative transactions
    d_blocks = re.findall(
        r"<derivativeTransaction>(.*?)</derivativeTransaction>",
        xml_text, re.DOTALL,
    )
    for block in d_blocks:
        txn = {
            "owner_name": owner_name,
            "role": role,
            "is_director": is_director,
            "is_officer": is_officer,
            "is_ten_percent_owner": is_10p,
            "is_derivative": True,
            "security_title": _extract_value(block, "securityTitle"),
            "transaction_code": _extract_value(block, "transactionCode"),
            "acquired_disposed": _extract_value(block, "transactionAcquiredDisposedCode"),
            "shares": _safe_float(_extract_value(block, "transactionShares")),
            "price_per_share": _safe_float(_extract_value(block, "transactionPricePerShare")),
            "shares_owned_after": None,
            "transaction_date": _extract_value(block, "transactionDate"),
            "total_value": None,  # derivatives often have $0 exercise price
        }
        # For options exercised, the economic value = shares × market price
        # But we don't always have market price in derivative transactions
        if txn["shares"] and txn["price_per_share"] and txn["price_per_share"] > 0:
            txn["total_value"] = txn["shares"] * txn["price_per_share"]
        results.append(txn)

    return results


# ═══════════════════════════════════════════════════════════
# Form 144 XML/Markdown 解析
# ═══════════════════════════════════════════════════════════

def _parse_form144(filing):
    """
    从 Form 144 filing 中提取减持计划关键字段。

    优先级：XML 结构化数据 > markdown 表格解析
    """
    result = {
        "seller_name": None,
        "securities_class": None,
        "shares_to_sell": None,
        "aggregate_market_value": None,
        "broker_name": None,
        "approximate_sale_date": None,
    }

    try:
        xml_text = filing.xml()

        # XML fields
        for tag, key in [
            ("nameOfSeller", "seller_name"),
            ("classOfSecurities", "securities_class"),
            ("numberOfSharesToBeSold", "shares_to_sell"),
            ("aggregateMarketValue", "aggregate_market_value"),
            ("nameOfBroker", "broker_name"),
        ]:
            m = re.search(
                rf"<{tag}>\s*(?:<value>)?([^<]*?)(?:</value>)?\s*</{tag}>",
                xml_text, re.DOTALL,
            )
            if m:
                val = m.group(1).strip()
                if val and val not in ("", "N/A", "0"):
                    result[key] = val

        # Parse numeric values
        for key in ("shares_to_sell", "aggregate_market_value"):
            if result[key]:
                try:
                    result[key] = float(result[key].replace(",", "").replace("$", ""))
                except ValueError:
                    pass

    except Exception as e:
        logger.debug("Form 144 XML parse failed for %s: %s", filing.accession_number, e)

    # Fallback: try markdown for missing fields
    if not result["shares_to_sell"] or not result["aggregate_market_value"]:
        try:
            md = filing.markdown()
            # Look for "Number of Shares or Other Units To Be Sold"
            # followed by a number
            shares_m = re.search(
                r"Number of Shares.*?To Be Sold[| ]+([\d,]+)",
                md, re.IGNORECASE,
            )
            if shares_m and not result["shares_to_sell"]:
                try:
                    result["shares_to_sell"] = float(shares_m.group(1).replace(",", ""))
                except ValueError:
                    pass

            value_m = re.search(
                r"Aggregate Market Value[| ]+\$?([\d,]+)",
                md, re.IGNORECASE,
            )
            if value_m and not result["aggregate_market_value"]:
                try:
                    result["aggregate_market_value"] = float(value_m.group(1).replace(",", ""))
                except ValueError:
                    pass

        except Exception as e:
            logger.debug("Form 144 markdown parse failed: %s", e)

    return result


# ═══════════════════════════════════════════════════════════
# 采集主流程
# ═══════════════════════════════════════════════════════════

def collect_form4(comp, conn):
    """拉取一家公司的 Form 4 内部人交易，增量写入。"""
    ticker = comp["ticker"]
    if not comp.get("has_section16", True):
        logger.info("  %s: 无 Section 16 申报义务，跳过 Form 4 采集", comp["name_cn"])
        return

    cid_row = conn.execute(
        "SELECT id FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not cid_row:
        return
    company_id = cid_row[0]

    # 已有 accession 的去重集合
    existing = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT accession_number FROM insider_transactions WHERE company_id = ?",
            (company_id,),
        )
    )

    c = Company(ticker)
    form4s = list(c.get_filings(form="4").latest(30))

    new_txns = 0
    for f4 in form4s:
        acc = f4.accession_number
        if acc in existing:
            continue

        try:
            xml_text = f4.xml()
            txns = _parse_form4_xml(xml_text)
        except Exception as e:
            logger.warning("  Form 4 parse error %s: %s", acc, e)
            continue

        for txn in txns:
            if not txn["transaction_code"]:
                continue
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO insider_transactions
                        (company_id, accession_number, filing_date, owner_name,
                         is_director, is_officer, is_ten_percent_owner,
                         security_title, transaction_code, acquired_disposed,
                         shares, price_per_share, total_value,
                         shares_owned_after, transaction_date, is_derivative)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    company_id, acc, f4.filing_date, txn["owner_name"],
                    txn["is_director"], txn["is_officer"], txn["is_ten_percent_owner"],
                    txn["security_title"], txn["transaction_code"], txn["acquired_disposed"],
                    txn["shares"], txn["price_per_share"], txn["total_value"],
                    txn["shares_owned_after"], txn["transaction_date"], txn["is_derivative"],
                ))
                new_txns += 1
            except Exception as e:
                logger.debug("  Insert failed: %s", e)

        existing.add(acc)

    conn.commit()
    logger.info("  %s: %d new Form 4 transactions", ticker, new_txns)


def collect_form144(comp, conn):
    """拉取一家公司的 Form 144 减持计划，增量写入。"""
    ticker = comp["ticker"]
    if not comp.get("has_section16", True):
        logger.info("  %s: 无 Section 16 申报义务，跳过 Form 144 采集", comp["name_cn"])
        return

    cid_row = conn.execute(
        "SELECT id FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not cid_row:
        return
    company_id = cid_row[0]

    existing = set(
        r[0] for r in conn.execute(
            "SELECT accession_number FROM form144_filings WHERE company_id = ?",
            (company_id,),
        )
    )

    c = Company(ticker)
    form144s = list(c.get_filings(form="144").latest(10))

    new_filings = 0
    for f144 in form144s:
        acc = f144.accession_number
        if acc in existing:
            continue

        parsed = _parse_form144(f144)

        conn.execute("""
            INSERT OR IGNORE INTO form144_filings
                (company_id, accession_number, filing_date,
                 seller_name, securities_class, shares_to_sell,
                 aggregate_market_value, broker_name, approximate_sale_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_id, acc, f144.filing_date,
            parsed["seller_name"], parsed["securities_class"],
            parsed["shares_to_sell"], parsed["aggregate_market_value"],
            parsed["broker_name"], parsed["approximate_sale_date"],
        ))
        existing.add(acc)
        new_filings += 1

    conn.commit()
    logger.info("  %s: %d new Form 144 filings", ticker, new_filings)


# ═══════════════════════════════════════════════════════════
# 内部人情绪指标
# ═══════════════════════════════════════════════════════════

def calculate_insider_sentiment(company_id, conn, months=3):
    """
    计算最近 N 个月的内部人情绪得分。

    公式（PRD §3.7）:
      InsiderSentiment = Σ(买入金额_i × 角色权重_i) - Σ(卖出金额_j × 角色权重_j)
      归一化到 [-100, +100]

    阈值:
      > +30 → bullish  🟢
      -30 ~ +30 → neutral  🟡
      < -30 → bearish  🔴
    """
    cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT owner_name, is_director, is_officer, is_ten_percent_owner,
               transaction_code, total_value, shares, price_per_share
        FROM insider_transactions
        WHERE company_id = ?
          AND transaction_date >= ?
          AND total_value IS NOT NULL
    """, (company_id, cutoff)).fetchall()

    if not rows:
        return {
            "buy_count": 0, "sell_count": 0,
            "buy_value": 0, "sell_value": 0,
            "sentiment_score": 0, "sentiment_label": "neutral",
        }

    buy_value = 0.0
    sell_value = 0.0
    buy_count = 0
    sell_count = 0

    for owner_name, is_dir, is_off, is_10p, code, total_val, shares, price in rows:
        direction = TRANSACTION_DIRECTION.get(code, "neutral")
        if direction == "neutral":
            continue

        # Determine role weight
        if is_10p:
            weight = ROLE_WEIGHTS["10% Owner"]
        elif is_dir and is_off:
            weight = ROLE_WEIGHTS["Director"]
        elif is_dir:
            weight = ROLE_WEIGHTS["Director"]
        elif is_off:
            # Try to infer title from name pattern (simplified)
            weight = ROLE_WEIGHTS["Officer"]
        else:
            weight = ROLE_WEIGHTS["Other"]

        total_val = total_val or 0

        if direction == "buy":
            buy_value += total_val * weight
            buy_count += 1
        elif direction == "sell":
            sell_value += total_val * weight
            sell_count += 1

    raw_score = buy_value - sell_value
    max_possible = max(buy_value + sell_value, 1)
    normalized = max(-100, min(100, (raw_score / max_possible) * 100))

    if normalized > 30:
        label = "bullish"
    elif normalized < -30:
        label = "bearish"
    else:
        label = "neutral"

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "sentiment_score": round(normalized, 1),
        "sentiment_label": label,
    }


def generate_insider_sentiment(conn):
    """
    为所有有 Section 16 义务的公司计算并存储内部人情绪。
    """
    now = datetime.now()
    period_start = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")

    for comp in COMPETITORS:
        if not comp.get("has_section16", True):
            continue

        cid_row = conn.execute(
            "SELECT id FROM companies WHERE ticker = ?", (comp["ticker"],)
        ).fetchone()
        if not cid_row:
            continue
        company_id = cid_row[0]

        sentiment = calculate_insider_sentiment(company_id, conn)

        # Generate LLM summary
        summary = _generate_insider_summary(comp, company_id, sentiment, conn)

        conn.execute("""
            INSERT OR REPLACE INTO insider_sentiment
                (company_id, period_start, period_end,
                 buy_count, sell_count, buy_value, sell_value,
                 sentiment_score, sentiment_label, summary_cn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_id, period_start, period_end,
            sentiment["buy_count"], sentiment["sell_count"],
            sentiment["buy_value"], sentiment["sell_value"],
            sentiment["sentiment_score"], sentiment["sentiment_label"],
            summary,
        ))
        logger.info(
            "  %s sentiment: %s (score=%s, buys=%d, sells=%d)",
            comp["ticker"], sentiment["sentiment_label"],
            sentiment["sentiment_score"], sentiment["buy_count"],
            sentiment["sell_count"],
        )

    conn.commit()


# ═══════════════════════════════════════════════════════════
# LLM 摘要
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT_INSIDER = (
    "你是二手车行业的竞品分析师。你的任务是根据内部人交易记录，"
    "用 1-2 句中文总结内部人对公司前景的判断。"
    "只输出摘要文字，不要输出分析过程。"
)


def _has_llm():
    return bool(LLM_API_KEY_SUMMARY and LLM_API_KEY_SUMMARY.startswith("sk-"))


def _llm_chat(system_prompt, user_prompt, max_tokens=300):
    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        client = OpenAI(api_key=LLM_API_KEY_SUMMARY, base_url=LLM_API_BASE)
        resp = client.chat.completions.create(
            model=LLM_MODEL_SUMMARY,
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


def _generate_insider_summary(comp, company_id, sentiment, conn):
    """用 LLM 生成内部人动向中文摘要。"""
    ticker = comp["ticker"]
    name_cn = comp["name_cn"]

    # Get recent transactions for context
    rows = conn.execute("""
        SELECT owner_name, transaction_code, shares, price_per_share,
               total_value, transaction_date, is_director, is_officer,
               is_ten_percent_owner
        FROM insider_transactions
        WHERE company_id = ?
        ORDER BY transaction_date DESC
        LIMIT 10
    """, (company_id,)).fetchall()

    if not rows:
        return f"[🟡 中性] {name_cn}：最近 3 个月无内部人交易记录。"

    # Build transaction table
    table_lines = ["| 日期 | 内部人 | 角色 | 交易 | 股数 | 金额 |"]
    table_lines.append("|------|--------|------|------|------|------|")
    for r in rows[:10]:
        parts = []
        if r[5]:
            parts.append("CEO/CFO" if (r[6] or r[7]) else "")
        elif r[6]:
            parts.append("Director")
        elif r[7]:
            parts.append("Officer")
        elif r[8]:
            parts.append("10% Owner")
        else:
            parts.append("Other")
        role_str = "/".join(filter(None, ["CEO" if r[6] and r[7] else "",
                                          "Director" if r[6] and not r[7] else "",
                                          "Officer" if r[7] and not r[6] else ""])) or "Other"

        direction = TRANSACTION_DIRECTION.get(r[1], "?")
        emoji = {"buy": "🟢", "sell": "🔴", "neutral": "⚪"}.get(direction, "⚪")
        date = str(r[5])[:10] if r[5] else "?"

        val_str = f"${r[4]:,.0f}" if r[4] else "N/A"
        shares_str = f"{r[2]:,.0f}" if r[2] else "N/A"

        table_lines.append(f"| {date} | {r[0]} | {role_str} | {emoji}{r[1]} | {shares_str} | {val_str} |")

    table = "\n".join(table_lines)

    if not _has_llm():
        label_cn = {"bullish": "看多", "neutral": "中性", "bearish": "看空"}.get(
            sentiment["sentiment_label"], "中性"
        )
        emoji = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(
            sentiment["sentiment_label"], "🟡"
        )
        return (
            f"{emoji} {name_cn}：近 3 个月内部人{sentiment['buy_count']}笔买入 / "
            f"{sentiment['sell_count']}笔卖出，情绪{label_cn}。"
        )

    user_prompt = (
        f"以下是 {name_cn} ({ticker}) 内部人最近 3 个月的交易记录：\n\n"
        f"{table}\n\n"
        f"总体统计：买入 {sentiment['buy_count']} 笔 / 卖出 {sentiment['sell_count']} 笔，"
        f"情绪得分 {sentiment['sentiment_score']}。\n\n"
        f"请用 1-2 句中文总结内部人动向："
        f"1. 总体净买入还是净卖出？涉及金额多大？"
        f"2. 有 CEO/CFO 级别的关键人物交易吗？"
        f"3. 与一般趋势相比如何？"
    )

    result = _llm_chat(SYSTEM_PROMPT_INSIDER, user_prompt)
    if result:
        emoji = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(
            sentiment["sentiment_label"], "🟡"
        )
        return f"{emoji}{result}"

    # LLM 退化
    label_cn = {"bullish": "看多", "neutral": "中性", "bearish": "看空"}
    return (
        f"{emoji} {name_cn}：近 3 个月 {sentiment['buy_count']} 笔买入 / "
        f"{sentiment['sell_count']} 笔卖出，情绪{label_cn.get(sentiment['sentiment_label'], '?')}。"
    )


# ═══════════════════════════════════════════════════════════
# 总入口
# ═══════════════════════════════════════════════════════════

def collect_all_insider(tickers=None):
    """
    采集所有公司的 Form 4 + Form 144 + 计算情绪指标。
    可按 ticker 子集过滤。
    """
    set_identity(EDGAR_IDENTITY)
    conn = _get_db()
    init_insider_tables(conn)

    companies = COMPETITORS
    if tickers:
        companies = [c for c in COMPETITORS if c["ticker"] in tickers]

    for comp in companies:
        try:
            collect_form4(comp, conn)
        except Exception as e:
            logger.error("Form 4 error for %s: %s", comp["ticker"], e, exc_info=True)

        try:
            collect_form144(comp, conn)
        except Exception as e:
            logger.error("Form 144 error for %s: %s", comp["ticker"], e, exc_info=True)

    # Calculate sentiment for all companies
    generate_insider_sentiment(conn)

    conn.close()
    logger.info("Insider tracking done.")


if __name__ == "__main__":
    collect_all_insider()
