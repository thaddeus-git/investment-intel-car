"""
fetch_financials.py — 从 SEC companyfacts API 拉取 XBRL 财务数据，标准化后存入 SQLite。

关键设计：
1. 用 companyfacts API（单次请求拿全部历史），不解析 XBRL 文件
2. 标签映射表：不同公司可能用不同 XBRL tag 表达同一指标
3. Carvana 的标签已在 PRD 附录 B 验证，其他 4 家首次运行需发现 + 人工确认

用法:
    python fetch_financials.py                  # 拉取全部 5 家公司
    python fetch_financials.py --ticker CVNA      # 只拉取一家
    python fetch_financials.py --ticker CVNA --discover  # 列出所有 USD 标签（用于建立映射）
    python fetch_financials.py --dry-run          # 只打印，不写数据库
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ============================================================================
# 配置
# ============================================================================

HEADERS = {
    "User-Agent": "CompetitorIntel/1.0 (your-email@company.com)",
    "Accept-Encoding": "gzip, deflate",
}

COMPETITORS = {
    "CVNA": {"cik": "0001690820", "name": "Carvana Co.", "sic": "5500"},
    "KMX":  {"cik": "0001170010", "name": "Carmax Inc.", "sic": "5500"},
    "AN":   {"cik": "0000350698", "name": "AutoNation, Inc.", "sic": "5500"},
    "UXIN": {"cik": "0001729173", "name": "Uxin Ltd.", "sic": "5500"},
    "ATHM": {"cik": "0001527636", "name": "Autohome Inc.", "sic": "7370"},
}

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "competitor_intel.db"
RATE_LIMIT = 0.3

# ============================================================================
# 核心：XBRL 标签 → 标准指标名 映射
# ============================================================================

# 每个标准指标对应一组候选 XBRL 标签（按优先级排序）。
# discover 模式会列出公司所有 USD 标签，人工确认后加入此表。
METRIC_TAG_MAP: dict[str, list[str]] = {
    # 收入类
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",  # Carvana, ASC 606
        "Revenues",                                              # 老标准
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueServicesNet",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
    ],

    # 盈利类
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
    ],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],

    # 费用类
    "sga_expense": [
        "SellingGeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
        "GeneralAndAdministrativeExpense",
    ],

    # 资产负债
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "Cash"],
    "long_term_debt": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
    ],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],

    # 现金流
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],

    # 运营指标
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}

# 反向索引：tag → metric_name（运行时构建）
_tag_to_metric: dict[str, str] | None = None


def get_tag_to_metric() -> dict[str, str]:
    """构建 tag → metric_name 的反向查找表。"""
    global _tag_to_metric
    if _tag_to_metric is None:
        _tag_to_metric = {}
        for metric, tags in METRIC_TAG_MAP.items():
            for tag in tags:
                if tag not in _tag_to_metric:
                    _tag_to_metric[tag] = metric
    return _tag_to_metric


# ============================================================================
# 数据库
# ============================================================================

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_financials_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS financials (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT 'USD',
            fiscal_year INTEGER,
            fiscal_quarter INTEGER,
            form_type TEXT,
            filing_date TEXT,
            frame TEXT,
            xbrl_tag TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(company_id, metric_name, fiscal_year, fiscal_quarter, form_type)
        );

        CREATE INDEX IF NOT EXISTS idx_financials_company_metric
            ON financials(company_id, metric_name, fiscal_year, fiscal_quarter);
    """)


def get_company_id(conn: sqlite3.Connection, ticker: str) -> int:
    cur = conn.execute("SELECT id FROM companies WHERE ticker = ?", (ticker,))
    row = cur.fetchone()
    if row:
        return row[0]
    raise ValueError(f"Company {ticker} not in DB. Run fetch_submissions.py first.")


def upsert_financials(conn: sqlite3.Connection, company_id: int,
                      records: list[dict]) -> int:
    """批量 upsert 财务数据。返回新插入数量。"""
    new_count = 0
    for r in records:
        cur = conn.execute("""
            INSERT INTO financials (company_id, metric_name, metric_value, unit,
                                    fiscal_year, fiscal_quarter, form_type,
                                    filing_date, frame, xbrl_tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, metric_name, fiscal_year, fiscal_quarter, form_type)
            DO UPDATE SET
                metric_value = excluded.metric_value,
                unit = excluded.unit,
                filing_date = COALESCE(excluded.filing_date, financials.filing_date),
                frame = COALESCE(excluded.frame, financials.frame),
                xbrl_tag = COALESCE(excluded.xbrl_tag, financials.xbrl_tag)
        """, (
            company_id,
            r["metric_name"],
            r["metric_value"],
            r.get("unit", "USD"),
            r["fiscal_year"],
            r.get("fiscal_quarter"),
            r.get("form_type"),
            r.get("filing_date"),
            r.get("frame"),
            r.get("xbrl_tag"),
        ))
        if cur.rowcount > 0:
            new_count += 1
    conn.commit()
    return new_count


# ============================================================================
# SEC API
# ============================================================================

def fetch_company_facts(cik_padded: str) -> dict:
    """拉取公司全部 XBRL 数据。"""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# 数据提取
# ============================================================================

# map EDGAR fiscal period codes to quarter numbers
FP_TO_QUARTER = {
    "Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4,
    "FY": None,  # annual
}

# 季报 form types（报告期为季度的那一类）
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def classify_filing_form(fp: str, form: str) -> tuple[int | None, str | None]:
    """
    根据 fiscal period 和 form type 判断是季度还是年度数据，以及标准化的 form 类别。

    返回 (fiscal_quarter, form_type_category)
    - fiscal_quarter: 1-4 for quarterly, None for annual
    - form_type_category: '10-Q' or '10-K' or None if unknown
    """
    fp_upper = (fp or "").strip().upper()
    form_upper = (form or "").strip().upper()

    quarter = FP_TO_QUARTER.get(fp_upper)

    if form_upper in ANNUAL_FORMS:
        return None, "10-K"
    elif form_upper in QUARTERLY_FORMS:
        return FP_TO_QUARTER.get(fp_upper), "10-Q"
    else:
        # 根据 fp 判断：FY → annual; Q1/Q2/Q3/Q4 → quarterly
        if fp_upper == "FY":
            return None, "10-K"  # assume annual
        elif fp_upper in FP_TO_QUARTER:
            return FP_TO_QUARTER[fp_upper], "10-Q"
        else:
            return FP_TO_QUARTER.get(fp_upper), None


def extract_metrics(facts_data: dict, ticker: str) -> list[dict]:
    """
    从 companyfacts JSON 中提取标准化财务指标。

    XBRL 数据关键坑位：
    - 有 `frame` 字段的条目是单期数据（如 CY2026Q1 = Q1 单季）
    - 无 `frame` 且 fp=Q2/Q3/Q4 的条目是 **YTD 累计值**（如 val=H1 累计），必须跳过
    - 无 `frame` 且 fp=Q1 的条目是单季（Q1 没有历史可累计）
    - 无 `frame` 且 fp=FY 的条目是全年数据

    去重策略：
    1. 同一 (fy, fp, form) 组内优先取 frame 匹配 fy 的条目
    2. 同一 (metric, fy, fp, form) 跨 tag 优先取 METRIC_TAG_MAP 中排在前面的 tag
       （如 NetIncomeLoss 优先于 ProfitLoss）
    """
    gaap = facts_data.get("facts", {}).get("us-gaap", {})
    tag_to_metric = get_tag_to_metric()

    # 构建 tag 优先级: 在 METRIC_TAG_MAP 中越靠前优先级越高
    tag_priority: dict[str, int] = {}
    for metric, tags in METRIC_TAG_MAP.items():
        for idx, tag in enumerate(tags):
            if tag not in tag_priority:
                tag_priority[tag] = idx

    # 先收集所有候选记录
    candidates: list[dict] = []
    seen_keys: dict[tuple, int] = {}  # (metric, fy, fp, form) -> candidates index

    for tag, tag_data in gaap.items():
        metric_name = tag_to_metric.get(tag)
        if metric_name is None:
            continue

        units = tag_data.get("units", {})
        # 包含 USD 和 USD/shares（EPS 等 per-share 指标）
        relevant_units = [u for u in units if u.startswith("USD")]

        for unit in relevant_units:
            entries = units[unit]

            # 按 (fy, fp, form) 分组，每组优先保留有 frame 的
            groups: dict[tuple, list[dict]] = {}
            for entry in entries:
                val = entry.get("val")
                fy = entry.get("fy")
                if val is None or fy is None:
                    continue
                fp = entry.get("fp", "")
                form = entry.get("form", "")
                key = (fy, fp, form)
                if key not in groups:
                    groups[key] = []
                groups[key].append(entry)

            for (fy, fp, form), group_entries in groups.items():
                # 挑选最佳条目：
                # 1) 优先有 frame 的，且 frame 年份匹配 fy（剔除去年同期对比数据）
                # 2) 若无匹配的 framed，取 unframed（但要跳过 Q2/Q3/Q4 YTD 累计值）
                framed = [e for e in group_entries if e.get("frame")]
                unframed = [e for e in group_entries if not e.get("frame")]

                selected = None
                if framed:
                    # frame 格式如 CY2026Q1 — 提取 CY 年份，匹配 fy
                    best = None
                    for e in framed:
                        fr = e.get("frame", "")
                        if str(fy) in fr:
                            best = e
                            break
                    if best is None:
                        best = framed[0]  # fallback: 第一个 framed
                    selected = best
                elif unframed:
                    # 无 frame：Q2/Q3/Q4 是 YTD 累计，跳过
                    fp_upper = (fp or "").strip().upper()
                    if fp_upper in ("Q2", "Q3", "Q4"):
                        continue
                    selected = unframed[0]

                if selected is None:
                    continue

                val = selected["val"]
                fiscal_quarter, form_category = classify_filing_form(fp, form)

                candidate = {
                    "metric_name": metric_name,
                    "metric_value": val,
                    "unit": unit,
                    "fiscal_year": fy,
                    "fiscal_quarter": fiscal_quarter,
                    "form_type": form_category or form,
                    "filing_date": selected.get("filed"),
                    "frame": selected.get("frame"),
                    "xbrl_tag": tag,
                }

                # 跨 tag 去重：同一 (metric, fy, fp, form_category) 只保留 tag 优先级最高的
                cand_key = (metric_name, fy, fp, form_category or form)
                if cand_key in seen_keys:
                    prev_idx = seen_keys[cand_key]
                    prev_tag = candidates[prev_idx]["xbrl_tag"]
                    prev_prio = tag_priority.get(prev_tag, 999)
                    curr_prio = tag_priority.get(tag, 999)
                    if curr_prio < prev_prio:
                        candidates[prev_idx] = candidate
                else:
                    seen_keys[cand_key] = len(candidates)
                    candidates.append(candidate)

    return candidates


def discover_usd_tags(facts_data: dict, min_value: float = 1_000_000) -> list[dict]:
    """发现公司所有 USD 标签（用于建立映射表）。"""
    gaap = facts_data.get("facts", {}).get("us-gaap", {})
    tags = []
    tag_to_metric = get_tag_to_metric()

    for tag, tag_data in gaap.items():
        units = tag_data.get("units", {})
        usd_units = [u for u in units if u.startswith("USD")]
        if not usd_units:
            continue

        # 取最新一条的值
        latest_val = None
        latest_fy = None
        latest_form = None
        for unit in usd_units:
            for entry in units[unit]:
                v = entry.get("val")
                if v is not None and (latest_val is None or entry.get("fy", 0) > (latest_fy or 0)):
                    latest_val = v
                    latest_fy = entry.get("fy")
                    latest_form = entry.get("form")

        if latest_val and abs(latest_val) > min_value:
            already_mapped = tag_to_metric.get(tag)
            tags.append({
                "tag": tag,
                "latest_value": latest_val,
                "latest_fy": latest_fy,
                "latest_form": latest_form,
                "already_mapped_to": already_mapped,
                "description": tag_data.get("label", ""),
            })

    return sorted(tags, key=lambda x: abs(x["latest_value"]), reverse=True)


# ============================================================================
# 展示
# ============================================================================

METRIC_LABELS_CN = {
    "revenue": "营业收入",
    "cost_of_revenue": "营业成本",
    "gross_profit": "毛利润",
    "operating_income": "营业利润",
    "net_income": "净利润",
    "eps_basic": "每股收益(基本)",
    "eps_diluted": "每股收益(稀释)",
    "sga_expense": "SG&A费用",
    "total_assets": "总资产",
    "total_liabilities": "总负债",
    "current_assets": "流动资产",
    "current_liabilities": "流动负债",
    "cash": "现金及等价物",
    "long_term_debt": "长期债务",
    "total_equity": "股东权益",
    "operating_cf": "经营活动现金流",
    "capex": "资本支出",
    "shares_outstanding": "流通股数",
}


def print_metrics(facts_data: dict, records: list[dict], ticker: str):
    """打印财务数据摘要。—— 显示最新季度的核心指标"""
    name = facts_data.get("entityName", ticker)

    print(f"\n{'='*80}")
    print(f"  {ticker} — {name}")
    print(f"  标准化财务指标")
    print(f"{'='*80}")

    # 按指标分组，取最新数据（优先季度，其次年度）
    latest = {}
    for r in records:
        key = r["metric_name"]
        r_fq = r.get("fiscal_quarter") or 0
        if key not in latest:
            latest[key] = r
        else:
            cur_fq = latest[key].get("fiscal_quarter") or 0
            # 比较：先比年份，再比季度（季度优先于年度）
            if (r["fiscal_year"], r_fq) > (latest[key]["fiscal_year"], cur_fq):
                latest[key] = r

    print(f"  {'指标':<20} {'值':>16} {'期间':>12}  {'Tag'}")
    print(f"  {'-'*20} {'-'*16} {'-'*12}  {'-'*40}")

    # per-share 指标名（用 unit 判断也行，这里直接用指标名）
    PER_SHARE_METRICS = {"eps_basic", "eps_diluted"}

    for metric_name in METRIC_TAG_MAP:
        r = latest.get(metric_name)
        if r is None:
            continue

        label_cn = METRIC_LABELS_CN.get(metric_name, metric_name)

        val = r["metric_value"]
        is_per_share = metric_name in PER_SHARE_METRICS or "shares" in r.get("unit", "")

        if is_per_share:
            val_str = f"${val:>14.2f}"
        elif abs(val) > 1e9:
            val_str = f"${val / 1e9:>14.0f}B"
        else:
            val_str = f"${val / 1e6:>14.0f}M"

        fy = r.get("fiscal_year", "?")
        fq = r.get("fiscal_quarter")
        period = f"FY{fy}" if fq is None else f"FY{fy} Q{fq}"

        tag_short = r.get("xbrl_tag", "")[:40]

        print(f"  {label_cn:<20} {val_str}  {period:<12}  {tag_short}")

    print(f"\n  共提取 {len(records)} 条数据记录，{len(latest)} 个指标有数据")


def print_discovery(ticker: str, tags: list[dict]):
    """打印 discover 模式的结果（USD 标签列表）。"""
    print(f"\n{'='*80}")
    print(f"  {ticker} — USD 标签发现（> $1M）")
    print(f"  共 {len(tags)} 个标签")
    print(f"{'='*80}")
    print(f"  {'Mapped?':<10} {'Value (latest)':>18} {'FY':>6} {'Tag'}")
    print(f"  {'-'*10} {'-'*18} {'-'*6} {'-'*45}")

    for t in tags:
        mapped = "✅ " + t["already_mapped_to"] if t["already_mapped_to"] else "❌ 未映射"
        val_b = t["latest_value"] / 1e9 if abs(t["latest_value"]) > 1e9 else t["latest_value"] / 1e6
        unit_label = "B" if abs(t["latest_value"]) > 1e9 else "M"
        print(f"  {mapped:<10} ${val_b:>16.0f}{unit_label}  {t['latest_fy'] or '?':>6}  {t['tag']}")


# ============================================================================
# 主流程
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="拉取竞品公司 XBRL 财务数据")
    parser.add_argument("--ticker", type=str, default=None,
                        help="只拉取指定 ticker（如 CVNA）")
    parser.add_argument("--discover", action="store_true",
                        help="发现模式：列出所有 USD 标签（用于建立映射表）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写入数据库")
    args = parser.parse_args()

    if args.ticker:
        ticker = args.ticker.upper()
        if ticker not in COMPETITORS:
            print(f"❌ 未知 ticker: {ticker}，可选: {', '.join(COMPETITORS)}")
            sys.exit(1)
        targets = {ticker: COMPETITORS[ticker]}
    else:
        targets = COMPETITORS

    conn = None if args.dry_run else get_db()
    if conn:
        init_financials_table(conn)

    total_records = 0

    for ticker, info in targets.items():
        cik = info["cik"]
        print(f"\n🔍 正在获取 {ticker} companyfacts (CIK {cik}) ...")

        try:
            facts_data = fetch_company_facts(cik)
        except requests.exceptions.RequestException as e:
            print(f"  ❌ API 请求失败: {e}")
            continue
        except Exception as e:
            print(f"  ❌ 解析失败: {e}")
            continue

        print(f"  ✅ 获取成功: {facts_data.get('entityName', 'Unknown')}")
        print(f"  📊 us-gaap 标签数: {len(facts_data.get('facts', {}).get('us-gaap', {}))}")

        if args.discover:
            tags = discover_usd_tags(facts_data)
            print_discovery(ticker, tags)
            continue

        # 标准模式：提取 + 存储
        records = extract_metrics(facts_data, ticker)
        print_metrics(facts_data, records, ticker)

        if conn:
            company_id = get_company_id(conn, ticker)
            new_count = upsert_financials(conn, company_id, records)
            total_records += new_count
            print(f"  💾 新增 {new_count} 条财务记录（company_id={company_id}）")

        time.sleep(RATE_LIMIT)

    if conn:
        conn.close()
        print(f"\n{'='*80}")
        print(f"  ✅ 完成！总计新增 {total_records} 条财务数据记录")
        print(f"  📦 数据库: {DB_PATH}")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
