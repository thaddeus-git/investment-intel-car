"""
fetch_submissions.py — 拉取竞品公司 SEC filing 列表并存入 SQLite。

用法:
    python fetch_submissions.py              # 拉取全部 5 家公司
    python fetch_submissions.py --ticker CVNA  # 只拉取一家
    python fetch_submissions.py --dry-run    # 只打印，不写数据库
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import requests

# ============================================================================
# 配置
# ============================================================================

# SEC API 要求 User-Agent 包含机构名和联系邮箱
HEADERS = {
    "User-Agent": "CompetitorIntel/1.0 (your-email@company.com)",
    "Accept-Encoding": "gzip, deflate",
}

# 5 家竞品公司（CIK 已验证）
COMPETITORS = {
    "CVNA": {"cik": "0001690820", "name": "Carvana Co."},
    "KMX":  {"cik": "0001170010", "name": "Carmax Inc."},
    "AN":   {"cik": "0000350698", "name": "AutoNation, Inc."},
    "UXIN": {"cik": "0001729173", "name": "Uxin Ltd."},
    "ATHM": {"cik": "0001527636", "name": "Autohome Inc."},
}

# 数据库路径（项目根目录）
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "competitor_intel.db"

# SEC API 速率限制（秒）
RATE_LIMIT = 0.3


# ============================================================================
# 数据库初始化
# ============================================================================

def init_db(db_path: Path) -> sqlite3.Connection:
    """创建数据库和表（如果不存在）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY,
            ticker TEXT UNIQUE NOT NULL,
            cik TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            sic_code TEXT,
            sic_description TEXT,
            fiscal_year_end TEXT
        );

        CREATE TABLE IF NOT EXISTS filings (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            accession_number TEXT UNIQUE NOT NULL,
            form_type TEXT,
            filing_date TEXT,
            report_date TEXT,
            items TEXT,
            is_processed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_filings_company_date
            ON filings(company_id, filing_date DESC);

        CREATE INDEX IF NOT EXISTS idx_filings_form
            ON filings(form_type);
    """)

    return conn


def upsert_company(conn: sqlite3.Connection, ticker: str, cik: str, name: str,
                   sic_code: str = None, sic_desc: str = None, fy_end: str = None) -> int:
    """插入或更新公司记录，返回 company_id。"""
    cur = conn.execute("""
        INSERT INTO companies (ticker, cik, name, sic_code, sic_description, fiscal_year_end)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = excluded.cik,
            name = excluded.name,
            sic_code = COALESCE(excluded.sic_code, companies.sic_code),
            sic_description = COALESCE(excluded.sic_description, companies.sic_description),
            fiscal_year_end = COALESCE(excluded.fiscal_year_end, companies.fiscal_year_end)
    """, (ticker, cik, name, sic_code, sic_desc, fy_end))
    conn.commit()
    return cur.lastrowid


def upsert_filings(conn: sqlite3.Connection, company_id: int,
                   filings: list[dict]) -> int:
    """批量 upsert filing 记录。返回新插入数量。"""
    new_count = 0
    for f in filings:
        cur = conn.execute("""
            INSERT INTO filings (company_id, accession_number, form_type,
                                 filing_date, report_date, items)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_number) DO UPDATE SET
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                report_date = COALESCE(excluded.report_date, filings.report_date),
                items = COALESCE(excluded.items, filings.items),
                updated_at = datetime('now')
        """, (
            company_id,
            f["accession_number"],
            f["form_type"],
            f["filing_date"],
            f.get("report_date"),
            f.get("items"),
        ))
        if cur.rowcount > 0:
            new_count += 1
    conn.commit()
    return new_count


# ============================================================================
# SEC API 调用
# ============================================================================

def fetch_submissions(cik_padded: str, limit: int = 20) -> dict:
    """
    获取公司最新 filing 列表。
    返回: {"company_info": {...}, "filings": [...]}
    """
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # 基础信息
    company_info = {
        "cik": data.get("cik"),
        "name": data.get("name"),
        "tickers": data.get("tickers", []),
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
    }

    # 最近 filings（从 .filings.recent 取）
    recent = data.get("filings", {}).get("recent", {})
    n = min(limit, len(recent.get("accessionNumber", [])))

    filings = []
    for i in range(n):
        items_raw = recent.get("items", [None])[i] or ""
        filings.append({
            "accession_number": recent["accessionNumber"][i],
            "form_type": recent["form"][i],
            "filing_date": recent["filingDate"][i],
            "report_date": recent.get("reportDate", [None])[i],
            "items": items_raw,
        })

    return {"company_info": company_info, "filings": filings}


# ============================================================================
# 展示
# ============================================================================

# 8-K item 分类映射（用于标注）
ITEM_LABELS = {
    "1.01": "📄 重大合同",
    "1.02": "📄 合同终止",
    "1.03": "📄 破产/接管",
    "2.02": "📊 业绩发布",
    "2.03": "📊 直接财务义务",
    "3.01": "⚠️ 退市/转板通知",
    "3.02": "🔴 权益出售（未注册）",
    "3.03": "🔴 股东权利变更",
    "4.01": "📋 审计师变更",
    "4.02": "📋 重述（非信赖）",
    "5.01": "👤 控制权变更",
    "5.02": "🔴 高管/董事变动",
    "5.03": "🟡 章程修改",
    "5.07": "🟡 股东投票结果",
    "7.01": "🟢 Regulation FD 披露",
    "8.01": "🟢 其他事件",
    "9.01": "🟢 财报附件",
}


def classify_filing(form_type: str, items: str) -> tuple[str, str]:
    """返回 (category_emoji, category_label)。"""
    form = (form_type or "").strip().upper()

    # 10-K / 10-Q
    if form in ("10-K", "10-K/A"):
        return "📊", "年报"
    if form in ("10-Q", "10-Q/A"):
        return "📊", "季报"

    # 8-K
    if form in ("8-K", "8-K/A"):
        item_list = [i.strip() for i in items.split(",") if i.strip()] if items else []
        # 检查是否有高优先级 item
        for i in item_list:
            if i in ("2.02", "5.02", "1.01"):
                return "⚠️", f"事件 ({', '.join(item_list)})"
        for i in item_list:
            if i in ("5.03", "5.07", "3.01"):
                return "🟡", f"事件 ({', '.join(item_list)})"
        return "📋", f"事件 ({', '.join(item_list)})" if item_list else "事件"

    # 内部人交易
    if form in ("3", "4", "5"):
        return "👤", "内部人交易"
    if form in ("SC 13G", "SC 13G/A"):
        return "👤", "持仓变动"
    if form in ("SC 13D", "SC 13D/A"):
        return "👤", "大股东变动"
    if form == "144":
        return "⚠️", "内部人减持"
    if form in ("S-1", "S-3", "S-4"):
        return "💰", "融资/发行"
    if form == "6-K":
        return "📋", "外国发行人报告"

    return "📋", "其他"


def print_filings(ticker: str, company_name: str, filings: list[dict]):
    """美观打印 filing 列表。"""
    print(f"\n{'='*80}")
    print(f"  {ticker} — {company_name}")
    print(f"  最近 {len(filings)} 条 Filing")
    print(f"{'='*80}")
    print(f"{'日期':<12} {'类型':<10} {'标签':<22} {'Accession Number'}")
    print(f"{'-'*12} {'-'*10} {'-'*22} {'-'*30}")

    for f in filings:
        emoji, label = classify_filing(f["form_type"], f.get("items", ""))
        tag = f"{emoji} {label}"
        print(f"{f['filing_date']:<12} {f['form_type']:<10} {tag:<22} {f['accession_number']}")

    # 统计
    forms = {}
    for f in filings:
        ft = f["form_type"]
        forms[ft] = forms.get(ft, 0) + 1
    print(f"\n  📈 类型分布: ", end="")
    print(" | ".join(f"{k}×{v}" for k, v in sorted(forms.items())))


# ============================================================================
# 主流程
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="拉取竞品公司 SEC filing 列表")
    parser.add_argument("--ticker", type=str, default=None,
                        help="只拉取指定 ticker（如 CVNA）")
    parser.add_argument("--limit", type=int, default=20,
                        help="每家拉取的 filing 条数（默认 20）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写入数据库")
    args = parser.parse_args()

    # 确定要拉取的公司
    if args.ticker:
        ticker = args.ticker.upper()
        if ticker not in COMPETITORS:
            print(f"❌ 未知 ticker: {ticker}，可选: {', '.join(COMPETITORS)}")
            sys.exit(1)
        targets = {ticker: COMPETITORS[ticker]}
    else:
        targets = COMPETITORS

    # 初始化数据库
    conn = None
    if not args.dry_run:
        conn = init_db(DB_PATH)
        print(f"📦 数据库: {DB_PATH}")

    total_new = 0

    for ticker, info in targets.items():
        cik = info["cik"]
        print(f"\n🔍 正在获取 {ticker} (CIK {cik}) ...")

        try:
            result = fetch_submissions(cik, limit=args.limit)
        except requests.exceptions.RequestException as e:
            print(f"  ❌ API 请求失败: {e}")
            continue
        except Exception as e:
            print(f"  ❌ 解析失败: {e}")
            continue

        company_name = result["company_info"]["name"]
        filings = result["filings"]
        print(f"  ✅ 获取到 {len(filings)} 条 filing")

        # 打印展示
        print_filings(ticker, company_name, filings)

        # 写数据库
        if conn:
            ci = result["company_info"]
            company_id = upsert_company(
                conn, ticker, cik, company_name,
                sic_code=ci.get("sic"),
                sic_desc=ci.get("sic_description"),
                fy_end=ci.get("fiscal_year_end"),
            )

            new_count = upsert_filings(conn, company_id, filings)
            total_new += new_count
            print(f"  💾 新增 {new_count} 条（company_id={company_id}）")

        # 请求间延迟（避免被限流）
        time.sleep(RATE_LIMIT)

    if conn:
        # 汇总
        conn.close()
        print(f"\n{'='*80}")
        print(f"  ✅ 完成！总计新增 {total_new} 条 filing 记录")
        print(f"  📦 数据库: {DB_PATH}")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
