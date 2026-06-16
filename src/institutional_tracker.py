"""
竞品情报监控系统 — 13F 机构持仓追踪（Phase 2）

13F-HR: 管理资产 >$1 亿的机构投资人每季度提交的持仓报告。
- 使用 edgartools `ThirteenF` 类解析持仓明细
- 反向查询：维护 Top 25 机构 CIK 列表 → 逐一拉取 13F → grep 竞品 ticker
- 跨季度对比：同一机构 QoQ 持仓变动（加仓/减仓/清仓/建仓）

输出：机构动向信号 (Institutional Signal)，写入 SQLite。
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from edgar import Company, set_identity, ThirteenF

from config import (
    COMPETITORS,
    DATABASE_PATH,
    EDGAR_IDENTITY,
    TOP_INSTITUTIONS,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ═══════════════════════════════════════════════════════════
# 数据库表
# ═══════════════════════════════════════════════════════════

def init_institutional_tables(conn):
    """建 13F 持仓表（幂等）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS institutional_holdings (
            id                INTEGER PRIMARY KEY,
            company_id        INTEGER REFERENCES companies(id),
            institution_name  TEXT,
            institution_cik   TEXT,
            filing_date       DATE,
            report_period     TEXT,
            ticker            TEXT,
            issuer_name       TEXT,
            cusip             TEXT,
            value_x1000       REAL,
            shares            REAL,
            share_type        TEXT,
            investment_discretion TEXT,
            sole_voting       REAL,
            shared_voting     REAL,
            non_voting        REAL,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(institution_cik, report_period, ticker)
        );

        CREATE TABLE IF NOT EXISTS institutional_signal (
            id              INTEGER PRIMARY KEY,
            company_id      INTEGER REFERENCES companies(id),
            report_period   TEXT,
            total_institutions INTEGER DEFAULT 0,
            new_positions   INTEGER DEFAULT 0,
            exited_positions INTEGER DEFAULT 0,
            increased_positions INTEGER DEFAULT 0,
            decreased_positions INTEGER DEFAULT 0,
            signal_score    REAL DEFAULT 0,
            signal_label    TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_id, report_period)
        );
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════════
# 采集主流程
# ═══════════════════════════════════════════════════════════

def collect_13f_holdings(conn):
    """
    反向查询：对 Top 25 机构逐一拉取最新 13F，
    检查是否持有竞品股票，写入 institutional_holdings。
    """
    competitor_tickers = {c["ticker"]: c for c in COMPETITORS}
    competitor_cids = {}
    for comp in COMPETITORS:
        row = conn.execute(
            "SELECT id FROM companies WHERE ticker = ?", (comp["ticker"],)
        ).fetchone()
        if row:
            competitor_cids[comp["ticker"]] = row[0]

    total_new = 0
    for inst_cik, inst_name in TOP_INSTITUTIONS.items():
        try:
            c = Company(inst_cik)
            f13_list = list(c.get_filings(form="13F-HR").latest(2))
        except Exception as e:
            logger.warning("  Failed to get 13F for %s (%s): %s", inst_name, inst_cik, e)
            continue

        if not f13_list:
            continue

        # Use the latest 13F
        latest_filing = f13_list[0]

        try:
            tf = ThirteenF(latest_filing)
        except Exception as e:
            logger.warning("  ThirteenF parse error for %s: %s", inst_name, e)
            continue

        infotable = tf.infotable
        if infotable is None or (hasattr(infotable, 'empty') and infotable.empty):
            logger.debug("  %s: empty infotable", inst_name)
            continue

        report_period = str(tf.report_period)[:10] if hasattr(tf, 'report_period') and tf.report_period else ""

        for _, row_data in infotable.iterrows():
            ticker = str(row_data.get("Ticker", "")).strip().upper()
            if ticker not in competitor_tickers:
                continue

            comp_ticker = ticker
            company_id = competitor_cids.get(comp_ticker)
            if not company_id:
                continue

            try:
                conn.execute("""
                    INSERT OR REPLACE INTO institutional_holdings
                        (company_id, institution_name, institution_cik,
                         filing_date, report_period, ticker, issuer_name,
                         cusip, value_x1000, shares, share_type,
                         investment_discretion, sole_voting, shared_voting, non_voting)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    company_id, inst_name, inst_cik,
                    latest_filing.filing_date, report_period,
                    ticker,
                    str(row_data.get("Issuer", "")),
                    str(row_data.get("Cusip", "")),
                    float(row_data.get("Value", 0)),
                    float(row_data.get("SharesPrnAmount", 0)),
                    str(row_data.get("Type", "Shares")),
                    str(row_data.get("InvestmentDiscretion", "")),
                    float(row_data.get("SoleVoting", 0)),
                    float(row_data.get("SharedVoting", 0)),
                    float(row_data.get("NonVoting", 0)),
                ))
                total_new += 1
            except Exception as e:
                logger.debug("  Insert holding error: %s", e)

        logger.info("  %s: %d competitor holdings found", inst_name, total_new)

    conn.commit()
    logger.info("13F collection done. %d total holdings written.", total_new)


# ═══════════════════════════════════════════════════════════
# 机构动向信号
# ═══════════════════════════════════════════════════════════

def calculate_institutional_signal(conn):
    """
    对比最近两期 13F 数据，计算机构动向信号。

    公式（PRD §3.8）:
      InstitutionalSignal = (new - exited) × 2 + (increased - decreased) × 1

    阈值:
      > +5 → bullish  🟢
      -5 ~ +5 → neutral  🟡
      < -5 → bearish  🔴
    """
    # Find distinct report periods per competitor
    periods = conn.execute("""
        SELECT DISTINCT report_period FROM institutional_holdings
        WHERE report_period != ''
        ORDER BY report_period DESC
    """).fetchall()

    if len(periods) < 2:
        logger.info("Not enough 13F periods for comparison (need ≥2)")
        return

    latest = periods[0][0]
    previous = periods[1][0]

    for comp in COMPETITORS:
        ticker = comp["ticker"]
        cid_row = conn.execute(
            "SELECT id FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
        if not cid_row:
            continue
        company_id = cid_row[0]

        # Current period holdings
        curr_insts = set(
            r[0] for r in conn.execute("""
                SELECT DISTINCT institution_cik FROM institutional_holdings
                WHERE company_id = ? AND report_period = ?
            """, (company_id, latest))
        )

        prev_insts = set(
            r[0] for r in conn.execute("""
                SELECT DISTINCT institution_cik FROM institutional_holdings
                WHERE company_id = ? AND report_period = ?
            """, (company_id, previous))
        )

        new_positions = len(curr_insts - prev_insts)
        exited_positions = len(prev_insts - curr_insts)
        common = curr_insts & prev_insts

        increased = 0
        decreased = 0
        for inst_cik in common:
            curr_val = conn.execute("""
                SELECT COALESCE(SUM(value_x1000), 0) FROM institutional_holdings
                WHERE company_id = ? AND report_period = ? AND institution_cik = ?
            """, (company_id, latest, inst_cik)).fetchone()[0]

            prev_val = conn.execute("""
                SELECT COALESCE(SUM(value_x1000), 0) FROM institutional_holdings
                WHERE company_id = ? AND report_period = ? AND institution_cik = ?
            """, (company_id, previous, inst_cik)).fetchone()[0]

            if prev_val > 0:
                change_pct = (curr_val - prev_val) / prev_val
                if change_pct > 0.05:
                    increased += 1
                elif change_pct < -0.05:
                    decreased += 1

        signal = (new_positions - exited_positions) * 2 + (increased - decreased) * 1

        if signal > 5:
            label = "bullish"
        elif signal < -5:
            label = "bearish"
        else:
            label = "neutral"

        total_insts = len(curr_insts)

        conn.execute("""
            INSERT OR REPLACE INTO institutional_signal
                (company_id, report_period, total_institutions,
                 new_positions, exited_positions,
                 increased_positions, decreased_positions,
                 signal_score, signal_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_id, latest, total_insts,
            new_positions, exited_positions,
            increased, decreased,
            signal, label,
        ))
        logger.info(
            "  %s 13F signal: %s (score=%d, total=%d, new=%d, exited=%d, +%d/-%d)",
            ticker, label, signal, total_insts,
            new_positions, exited_positions, increased, decreased,
        )

    conn.commit()


# ═══════════════════════════════════════════════════════════
# 总入口
# ═══════════════════════════════════════════════════════════

def collect_all_institutional():
    """拉取所有机构的 13F + 计算信号。"""
    set_identity(EDGAR_IDENTITY)
    conn = _get_db()
    init_institutional_tables(conn)

    try:
        collect_13f_holdings(conn)
    except Exception as e:
        logger.error("13F collection error: %s", e, exc_info=True)

    try:
        calculate_institutional_signal(conn)
    except Exception as e:
        logger.error("13F signal calculation error: %s", e, exc_info=True)

    # 模块 F: 交叉持股分析（13F 采集后自动刷新，复用当前连接）
    try:
        from cross_holding import run_cross_holding_analysis
        logger.info("Running cross-holding analysis...")
        run_cross_holding_analysis(existing_conn=conn)
    except Exception as e:
        logger.error("Cross-holding analysis error: %s", e, exc_info=True)

    conn.close()
    logger.info("Institutional tracking done.")


if __name__ == "__main__":
    collect_all_institutional()
