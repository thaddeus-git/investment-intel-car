"""
SEC EDGAR API Cheatsheet — 竞品情报监控项目
已验证可用的 API 调用模式，代码片段可直接复用。
"""

import requests
import time

HEADERS = {
    "User-Agent": "CompanyName/1.0 (your-email@company.com)",
    "Accept-Encoding": "gzip, deflate",
}

# ============================================================================
# 1. 通过 Ticker 验证 CIK（永远不要凭记忆）
# ============================================================================

def get_cik_by_ticker(ticker: str) -> str:
    """通过 ticker 从 SEC 获取 10-digit padded CIK"""
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=&dateb=&owner=exclude&count=1"
    resp = requests.get(url, headers=HEADERS)
    # CIK 出现在 href 中: /cgi-bin/browse-edgar?action=getcompany&CIK=0001690820
    import re
    match = re.search(r'CIK=(\d{10})', resp.text)
    if match:
        return match.group(1)
    raise ValueError(f"CIK not found for ticker {ticker}")


# ============================================================================
# 2. 获取公司基本信息 + 最近 1000 条 Filing
# ============================================================================

def get_company_submissions(cik_padded: str):
    """获取公司信息和最近 1000 条 filing 列表"""
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    resp = requests.get(url, headers=HEADERS)
    data = resp.json()
    return {
        "name": data["name"],
        "tickers": data["tickers"],
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "recent_filings": [
            {
                "accession": data["filings"]["recent"]["accessionNumber"][i],
                "form": data["filings"]["recent"]["form"][i],
                "date": data["filings"]["recent"]["filingDate"][i],
                "items": data["filings"]["recent"].get("items", [""])[i],  # 8-K items
            }
            for i in range(min(20, len(data["filings"]["recent"]["accessionNumber"])))
        ],
    }


# ============================================================================
# 3. 获取全部历史财务数据（XBRL 标准化）— MVP 核心
# ============================================================================

def get_company_facts(cik_padded: str):
    """获取公司全部 XBRL 财务数据——一条 API 搞定所有历史和指标"""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    resp = requests.get(url, headers=HEADERS)
    return resp.json()


def extract_key_metrics(facts_data: dict):
    """从 companyfacts 数据中提取关键财务指标的最新值"""
    gaap = facts_data.get("facts", {}).get("us-gaap", {})

    # 标签映射——不同公司可能用不同标签，这是 Carvana 的映射
    metric_tags = {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "gross_profit": "GrossProfit",
        "net_income": "NetIncomeLoss",
        "operating_income": "OperatingIncomeLoss",
        "total_assets": "Assets",
        "cash": "CashAndCashEquivalentsAtCarryingValue",
        "sga": "SellingGeneralAndAdministrativeExpense",
        "eps_basic": "EarningsPerShareBasic",
        "long_term_debt": "LongTermDebt",
        "operating_cf": "NetCashProvidedByUsedInOperatingActivities",
        "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    }

    results = {}
    for metric, tag in metric_tags.items():
        if tag not in gaap:
            results[metric] = None
            continue
        units = gaap[tag]["units"]
        if "USD" in units:
            d = units["USD"][-1]  # 最新一条
            results[metric] = {
                "value": d["val"],
                "fy": d.get("fy"),
                "fp": d.get("fp"),
                "form": d.get("form"),
                "frame": d.get("frame"),
            }
        elif "USD/shares" in units:
            d = units["USD/shares"][-1]
            results[metric] = {
                "value": d["val"],
                "fy": d.get("fy"),
                "form": d.get("form"),
            }
    return results


# ============================================================================
# 4. 辅助：列出某公司所有 USD 金额标签（用于发现 XBRL 映射）
# ============================================================================

def list_all_usd_tags(facts_data: dict):
    """列出所有以 USD 计价的 us-gaap 标签——用于发现 tag 命名差异"""
    gaap = facts_data.get("facts", {}).get("us-gaap", {})
    usd_tags = []
    for tag, info in gaap.items():
        if "USD" in info.get("units", {}):
            latest = info["units"]["USD"][-1]
            if latest["val"] > 1_000_000:  # 只列出 >$1M 的
                usd_tags.append((tag, latest["val"], latest.get("fy"), latest.get("form")))
    return sorted(usd_tags, key=lambda x: abs(x[1]), reverse=True)


# ============================================================================
# 5. 安全调用（带限流保护）
# ============================================================================

class SECClient:
    """带限流保护的 SEC API 客户端"""

    def __init__(self, email: str, rate_limit: float = 0.2):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"CompetitorIntel/1.0 ({email})",
            "Accept-Encoding": "gzip, deflate",
        })
        self.rate_limit = rate_limit  # seconds between requests

    def get(self, url: str, params: dict = None) -> dict:
        time.sleep(self.rate_limit)
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_submissions(self, cik: str):
        return self.get(f"https://data.sec.gov/submissions/CIK{cik}.json")

    def get_companyfacts(self, cik: str):
        return self.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")


# ============================================================================
# MVP 已验证的 5 家竞品
# ============================================================================

COMPETITORS = {
    "CVNA": {"cik": "0001690820", "name": "Carvana Co."},
    "KMX":  {"cik": "0001170010", "name": "Carmax Inc."},
    "AN":   {"cik": "0000350698", "name": "AutoNation, Inc."},
    "UXIN": {"cik": "0001729173", "name": "Uxin Ltd."},
    "ATHM": {"cik": "0001527636", "name": "Autohome Inc."},
}


if __name__ == "__main__":
    # 快速验证
    client = SECClient(email="your-email@company.com", rate_limit=1.0)
    for ticker, info in COMPETITORS.items():
        try:
            data = client.get_submissions(info["cik"])
            name = data["name"]
            filings = len(data["filings"]["recent"]["accessionNumber"])
            print(f"✅ {ticker}: {name} — {filings} recent filings")
        except Exception as e:
            print(f"❌ {ticker}: {e}")
