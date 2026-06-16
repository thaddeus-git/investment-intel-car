"""
竞品情报监控系统 — 数据采集模块

用 edgartools 拉取 Filing 列表、8-K 文本、10-Q/K XBRL 财务数据。
写入 SQLite（5 张表，schema 照抄 PRD v1.0 §4.4）。

Phase 1: 先跑通 Carvana，验证通过再扩展到 5 家。
"""

import logging
import math
from datetime import datetime

from edgar import Company, set_identity

from config import (
    ACTIVIST_INSTITUTIONS,
    COMPETITORS,
    DATABASE_PATH,
    EDGAR_IDENTITY,
    INSTITUTION_STYLES,
    METRIC_TAGS,
    TOP_INSTITUTIONS,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ── 各指标对应的 XBRL Statement ──
STATEMENT_METRICS = {
    "IncomeStatement": [
        "revenue", "gross_profit", "operating_income", "net_income",
        "eps_basic", "cost_of_revenue", "sga",
    ],
    "BalanceSheet": [
        "total_assets", "cash_and_equivalents", "long_term_debt",
    ],
}

# 现金流星表名字各地不一样，逐个试
CF_STATEMENT_NAMES = [
    "CashFlows", "CashFlowStatement", "StatementOfCashFlows",
]
CF_METRICS = ["operating_cash_flow", "capex"]

# 费用/支出类指标在 XBRL 中以负值表示，存储时取绝对值
EXPENSE_METRICS = {"cost_of_revenue", "sga", "capex"}


# ═══════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════

def get_db(db_path=None):
    """打开 SQLite 连接（WAL 模式 + 外键）。"""
    path = db_path or DATABASE_PATH
    conn = __import__("sqlite3").connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    """建表 + 写入 5 家公司元数据（幂等）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id           INTEGER PRIMARY KEY,
            ticker       TEXT UNIQUE,
            cik          TEXT UNIQUE,
            name         TEXT,
            name_cn      TEXT,
            sic_code     TEXT,
            sic_description TEXT,
            fiscal_year_end  TEXT
        );

        CREATE TABLE IF NOT EXISTS filings (
            id               INTEGER PRIMARY KEY,
            company_id       INTEGER REFERENCES companies(id),
            accession_number TEXT UNIQUE,
            form_type        TEXT,
            filing_date      DATE,
            items            TEXT,
            is_processed     BOOLEAN DEFAULT 0,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS financials (
            id             INTEGER PRIMARY KEY,
            company_id     INTEGER REFERENCES companies(id),
            metric_name    TEXT,
            metric_value   REAL,
            unit           TEXT,
            fiscal_year    INTEGER,
            fiscal_quarter INTEGER,
            form_type      TEXT,
            filing_date    DATE,
            frame          TEXT,
            UNIQUE(company_id, metric_name, fiscal_year, fiscal_quarter)
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY,
            company_id  INTEGER REFERENCES companies(id),
            filing_id   INTEGER REFERENCES filings(id),
            event_type  TEXT,
            severity    TEXT,
            summary_cn  TEXT,
            raw_text    TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS earnings_call_notes (
            id                INTEGER PRIMARY KEY,
            company_id        INTEGER REFERENCES companies(id),
            fiscal_year       INTEGER,
            fiscal_quarter    INTEGER,
            source            TEXT,
            highlights        TEXT,
            management_remarks TEXT,
            qa_summary        TEXT,
            risks             TEXT,
            full_text_md      TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Phase 2: 内部人交易 + 机构持仓
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
            id              INTEGER PRIMARY KEY,
            company_id      INTEGER REFERENCES companies(id),
            period_start    DATE,
            period_end      DATE,
            buy_count       INTEGER DEFAULT 0,
            sell_count      INTEGER DEFAULT 0,
            buy_value       REAL DEFAULT 0,
            sell_value      REAL DEFAULT 0,
            sentiment_score REAL DEFAULT 0,
            sentiment_label TEXT,
            summary_cn      TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_id, period_start, period_end)
        );

        CREATE TABLE IF NOT EXISTS institutional_holdings (
            id                  INTEGER PRIMARY KEY,
            company_id          INTEGER REFERENCES companies(id),
            institution_name    TEXT,
            institution_cik     TEXT,
            filing_date         DATE,
            report_period       TEXT,
            ticker              TEXT,
            issuer_name         TEXT,
            cusip               TEXT,
            value_x1000         REAL,
            shares              REAL,
            share_type          TEXT,
            investment_discretion TEXT,
            sole_voting         REAL,
            shared_voting       REAL,
            non_voting          REAL,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(institution_cik, report_period, ticker)
        );

        CREATE TABLE IF NOT EXISTS institutional_signal (
            id                  INTEGER PRIMARY KEY,
            company_id          INTEGER REFERENCES companies(id),
            report_period       TEXT,
            total_institutions  INTEGER DEFAULT 0,
            new_positions       INTEGER DEFAULT 0,
            exited_positions    INTEGER DEFAULT 0,
            increased_positions INTEGER DEFAULT 0,
            decreased_positions INTEGER DEFAULT 0,
            signal_score        REAL DEFAULT 0,
            signal_label        TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_id, report_period)
        );

        -- 模块 F: 交叉持股分析
        CREATE TABLE IF NOT EXISTS cross_holding_matrix (
            id                  INTEGER PRIMARY KEY,
            report_period       TEXT,
            institution_name    TEXT,
            institution_cik     TEXT,
            cvna_value_x1000    REAL DEFAULT 0,
            kmx_value_x1000     REAL DEFAULT 0,
            an_value_x1000      REAL DEFAULT 0,
            uxin_value_x1000    REAL DEFAULT 0,
            athm_value_x1000    REAL DEFAULT 0,
            total_value_x1000   REAL DEFAULT 0,
            peer_avg_x1000      REAL DEFAULT 0,
            style_label         TEXT,
            activism_level      TEXT,
            turnover_proxy      TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(institution_cik, report_period)
        );

        CREATE TABLE IF NOT EXISTS institution_styles (
            institution_cik TEXT PRIMARY KEY,
            institution_name TEXT,
            style_label     TEXT,
            activism_level  TEXT,
            source          TEXT DEFAULT 'manual',
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    for comp in COMPETITORS:
        conn.execute("""
            INSERT OR IGNORE INTO companies
                (ticker, cik, name, name_cn, sic_code)
            VALUES (?, ?, ?, ?, ?)
        """, (comp["ticker"], comp["cik"], comp["name"], comp["name_cn"], comp["sic"]))

    # 模块 F: 种子机构风格数据（幂等写入）
    for inst_cik, style_label in INSTITUTION_STYLES.items():
        top_name = TOP_INSTITUTIONS.get(inst_cik)
        activism = ACTIVIST_INSTITUTIONS.get(inst_cik, {}).get("activism_level")
        conn.execute("""
            INSERT OR REPLACE INTO institution_styles
                (institution_cik, institution_name, style_label, activism_level)
            VALUES (?, ?, ?, ?)
        """, (inst_cik, top_name or inst_cik, style_label, activism))

    conn.commit()
    logger.info("Database initialized (10 tables + 5 companies).")


def _existing_accessions(conn):
    rows = conn.execute("SELECT accession_number FROM filings").fetchall()
    return {r[0] for r in rows}


# ═══════════════════════════════════════════════════════════
# XBRL 解析（通过 render_statement().to_dataframe()）
# ═══════════════════════════════════════════════════════════

def _build_period_map(rendered):
    """
    从 rendered.periods 构建 {column_name → (fiscal_year, fiscal_quarter)} 映射。

    10-Q IS: end_date=2026-03-31, quarter=Q1 → col "2026-03-31 (Q1)"
    10-K IS: end_date=2025-12-31, quarter=None → col "2025-12-31"
    BS:      end_date=2026-03-31, quarter=None, is_duration=False
    """
    period_map = {}
    for p in rendered.periods:
        end = str(p.end_date)   # "2026-03-31"
        fy = int(end[:4])
        month = int(end[5:7])

        if p.quarter:  # Q1, Q2, Q3, Q4 → 季度
            fq = int(str(p.quarter).lstrip("Q"))
        elif not p.is_duration:
            # Balance sheet 快照：用月份推断季度
            fq = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
        else:
            # 10-K Income Statement：年度（duration 为全年，quarter=None）
            fq = None

        period_map[end] = (fy, fq)
    return period_map


def _extract_from_dataframe(df, period_map, metric_keys, company_id,
                            form_type, filing_date, conn):
    """
    从 render_statement().to_dataframe() 的结果中提取指标。
    """
    if df is None or df.empty:
        return

    # 找出所有日期列（非元数据列）
    meta_cols = {"concept", "label", "level", "abstract", "dimension"}
    period_cols = [c for c in df.columns if c not in meta_cols]

    for _, row in df.iterrows():
        concept = str(row.get("concept", ""))
        # 去掉 namespace 前缀："us-gaap_Revenue..." → "Revenue..."
        if "_" in concept:
            short = concept.split("_", 1)[1]
        else:
            short = concept

        # 跳过抽象/合计行
        if row.get("abstract") is True:
            continue

        # 匹配目标指标
        matched = None
        for mk in metric_keys:
            if METRIC_TAGS.get(mk) == short:
                matched = mk
                break
        if not matched:
            continue

        for pc in period_cols:
            val = row[pc]
            # 跳过 None / 空 / NaN
            if val is None or val == "":
                continue
            if isinstance(val, float) and math.isnan(val):
                continue

            # 从列名提取日期（"2026-03-31" 或 "2026-03-31 (Q1)"）
            date_str = str(pc)[:10]  # 取前 10 个字符 "YYYY-MM-DD"
            parsed = period_map.get(date_str)
            if parsed is None:
                continue
            fy, fq = parsed
            if fy is None:
                continue

            frame = f"CY{fy}Q{fq}" if fq else f"CY{fy}"

            try:
                numeric_val = float(val)
            except (ValueError, TypeError):
                continue

            # 费用/支出类指标取绝对值（XBRL 中常为负）
            if matched in EXPENSE_METRICS:
                numeric_val = abs(numeric_val)

            conn.execute("""
                INSERT OR REPLACE INTO financials
                    (company_id, metric_name, metric_value, unit,
                     fiscal_year, fiscal_quarter, form_type, filing_date, frame)
                VALUES (?, ?, ?, 'USD', ?, ?, ?, ?, ?)
            """, (company_id, matched, numeric_val, fy, fq,
                  form_type, filing_date, frame))


def _extract_financials(filing, company_id, conn):
    """
    从一个 10-Q / 10-K filing 的 XBRL 中提取全部 12 个核心指标。
    使用 render_statement().to_dataframe() 获取标准化表格。
    """
    acc = filing.accession_number
    try:
        xbrl = filing.xbrl()
    except Exception as e:
        logger.warning("  XBRL parse failed for %s: %s", acc, e)
        return

    # Income Statement + Balance Sheet
    for stmt_name, metrics in STATEMENT_METRICS.items():
        try:
            rendered = xbrl.render_statement(stmt_name)
            period_map = _build_period_map(rendered)
            df = rendered.to_dataframe()
            _extract_from_dataframe(df, period_map, metrics, company_id,
                                    filing.form, filing.filing_date, conn)
        except Exception as e:
            logger.debug("  No %s in %s: %s", stmt_name, acc, e)

    # Cash Flow（试多个名字）
    for cf_name in CF_STATEMENT_NAMES:
        try:
            rendered = xbrl.render_statement(cf_name)
            period_map = _build_period_map(rendered)
            df = rendered.to_dataframe()
            _extract_from_dataframe(df, period_map, CF_METRICS, company_id,
                                    filing.form, filing.filing_date, conn)
            break
        except Exception:
            continue


# ═══════════════════════════════════════════════════════════
# 采集主流程
# ═══════════════════════════════════════════════════════════

def collect_company(comp, conn):
    """拉取一家公司的 filings + financials，增量写入 SQLite。"""
    ticker = comp["ticker"]
    logger.info("── %s (%s) ──", ticker, comp["name_cn"])

    row = conn.execute(
        "SELECT id FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not row:
        logger.error("  Company %s not in DB!", ticker)
        return
    company_id = row[0]

    existing = _existing_accessions(conn)

    # 拉最近 60 条 filing（保证覆盖到最新一份 10-Q/K）
    c = Company(ticker)
    filings = c.get_filings().latest(60)

    new_f, new_fin = 0, 0
    for f in filings:
        acc = f.accession_number
        if acc in existing:
            continue
        existing.add(acc)

        items = None
        if f.form in ("8-K", "6-K"):
            items = getattr(f, "items", None)

        conn.execute("""
            INSERT INTO filings (company_id, accession_number, form_type, filing_date, items)
            VALUES (?, ?, ?, ?, ?)
        """, (company_id, acc, f.form, f.filing_date, items))
        new_f += 1

        if f.form in ("10-Q", "10-K", "20-F"):
            _extract_financials(f, company_id, conn)
            new_fin += 1

    conn.commit()
    logger.info("  → %d new filings, %d financials extracted", new_f, new_fin)


def collect_all(tickers=None):
    """全量采集（可指定 ticker 子集，如 ["CVNA"]）。"""
    set_identity(EDGAR_IDENTITY)
    conn = get_db()
    init_db(conn)

    companies = COMPETITORS
    if tickers:
        companies = [c for c in COMPETITORS if c["ticker"] in tickers]

    for comp in companies:
        try:
            collect_company(comp, conn)
        except Exception as e:
            logger.error("Error collecting %s: %s", comp["ticker"], e, exc_info=True)

    conn.close()
    logger.info("Collection done.")


# ═══════════════════════════════════════════════════════════
# 验证（对照 PRD v1.0 附录 B）
# ═══════════════════════════════════════════════════════════

EXPECTED_CARVANA_Q1 = {
    "revenue":         6_432_000_000,
    "gross_profit":    1_271_000_000,
    "net_income":        250_000_000,
    "operating_income":  581_000_000,
    "sga":               690_000_000,
}


def verify_carvana():
    """打印 Carvana 2026Q1 数据 vs 预期，验证数据采集质量。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM companies WHERE ticker = 'CVNA'"
    ).fetchone()
    if not row:
        print("❌ Carvana not in DB. Run collect_all() first.")
        conn.close()
        return
    cid = row[0]

    print("Carvana 2026Q1 数据校验")
    print(f"{'指标':<25} {'实测值':>18} {'预期值':>18} {'状态'}")
    print("-" * 70)

    all_ok = True
    for metric, expected in EXPECTED_CARVANA_Q1.items():
        r = conn.execute("""
            SELECT metric_value FROM financials
            WHERE company_id = ? AND metric_name = ?
              AND fiscal_year = 2026 AND fiscal_quarter = 1
        """, (cid, metric)).fetchone()
        actual = r[0] if r else None

        if actual is None:
            status = "⚠️  缺失"
            all_ok = False
        elif abs(actual - expected) / expected < 0.01:
            status = "✅"
        else:
            status = f"❌ 偏差 {abs(actual - expected)/expected*100:.1f}%"
            all_ok = False

        actual_str = f"{actual:>18,}" if actual else f"{'N/A':>18}"
        print(f"{metric:<25} {actual_str} {expected:>18,} {status}")

    print("-" * 70)
    if all_ok:
        print("✅ Carvana 2026Q1 全部指标通过校验")
    else:
        print("⚠️  部分指标未通过，请检查 XBRL 数据")

    conn.close()


if __name__ == "__main__":
    collect_all()
    verify_carvana()
