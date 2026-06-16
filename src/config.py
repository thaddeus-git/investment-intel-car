"""
竞品情报监控系统 — 配置中心
"""

# ── edgartools identity（SEC 要求，必须设置后调 API） ──
EDGAR_IDENTITY = "CompetitorIntel/1.0 (your-email@company.com)"

# ── 5 家竞品 ──
# CIK 必须是 10 位 padded 格式
COMPETITORS = [
    {
        "ticker": "CVNA",
        "cik":   "0001690820",
        "name":  "Carvana Co.",
        "name_cn": "Carvana",
        "sic":   "5500",
        "has_section16": True,   # 美国公司，适用 SEC Section 16
    },
    {
        "ticker": "KMX",
        "cik":   "0001170010",
        "name":  "CarMax, Inc.",
        "name_cn": "Carmax",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "AN",
        "cik":   "0000350698",
        "name":  "AutoNation, Inc.",
        "name_cn": "AutoNation",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "UXIN",
        "cik":   "0001729173",
        "name":  "Uxin Limited",
        "name_cn": "优信",
        "sic":   "5500",
        "has_section16": False,  # ADR 结构，中国高管不受 Section 16 管辖
    },
    {
        "ticker": "ATHM",
        "cik":   "0001527636",
        "name":  "Autohome Inc.",
        "name_cn": "汽车之家",
        "sic":   "7370",
        "has_section16": False,  # ADR 结构，中国高管不受 Section 16 管辖
    },
]

# ── LLM API（DeepSeek，OpenAI 兼容接口） ──
# Flash = 便宜快 → 8-K 摘要  |  Pro = 推理强 → EC 纪要
LLM_MODEL_SUMMARY = "deepseek-v4-flash"
LLM_MODEL_DEEP    = "deepseek-v4-pro"
LLM_API_BASE      = "https://api.deepseek.com/v1"
LLM_API_KEY_SUMMARY = "YOUR_KEY_HERE"   # Flash
LLM_API_KEY_DEEP    = "YOUR_KEY_HERE"   # Pro

# ── SQLite 数据库 ──
DATABASE_PATH = "data/competitor_intel.db"

# ── XBRL 标签映射（12 个核心指标 → 公司级别的 tag 差异） ──
# "基准标签" 是 Carvana 实测可用的概念名。
# 其他公司的标签如果不同，在这里补。目前仅 Carvana 已确认。
METRIC_TAGS = {
    "revenue":                  "RevenueFromContractWithCustomerExcludingAssessedTax",
    "gross_profit":             "GrossProfit",
    "operating_income":         "OperatingIncomeLoss",
    "net_income":               "NetIncomeLoss",
    "eps_basic":                "EarningsPerShareBasic",
    "cost_of_revenue":          "CostOfRevenue",
    "sga":                      "SellingGeneralAndAdministrativeExpense",
    "total_assets":             "Assets",
    "cash_and_equivalents":     "CashAndCashEquivalentsAtCarryingValue",
    "long_term_debt":           "LongTermDebtNoncurrent",
    "operating_cash_flow":      "NetCashProvidedByUsedInOperatingActivities",
    "capex":                    "PaymentsToAcquirePropertyPlantAndEquipment",
}

# 中文标签（用于图表展示）
METRIC_LABELS_CN = {
    "revenue":              "营业收入",
    "gross_profit":         "毛利润",
    "operating_income":     "营业利润",
    "net_income":           "净利润",
    "eps_basic":            "每股收益",
    "cost_of_revenue":      "营业成本",
    "sga":                  "销售及管理费用",
    "total_assets":         "总资产",
    "cash_and_equivalents": "现金及等价物",
    "long_term_debt":       "长期债务",
    "operating_cash_flow":  "经营活动现金流",
    "capex":                "资本支出",
}

# 财务指标分类
METRIC_CATEGORIES = {
    "revenue": "收入",
    "gross_profit": "盈利",
    "operating_income": "盈利",
    "net_income": "盈利",
    "eps_basic": "盈利",
    "cost_of_revenue": "成本",
    "sga": "成本",
    "total_assets": "资产负债",
    "cash_and_equivalents": "资产负债",
    "long_term_debt": "资产负债",
    "operating_cash_flow": "现金流",
    "capex": "现金流",
}

# ── 13F 机构持仓监控 — 目标机构池（CIK → 机构名） ──
# 用于反向查询"哪些大机构持有竞品股票"。
# 13F 只能正向查（某机构持有啥），不能反向查（谁持有某股票），
# 所以需要维护一个种子机构列表，逐一拉取 13F 后 grep 竞品 ticker。
# 数据源：NASDAQ.com / WhaleWisdom / SEC EDGAR Full-Text Search
TOP_INSTITUTIONS = {
    "0001067983": "Berkshire Hathaway",
    "0001364742": "BlackRock",
    "0000102909": "Vanguard Group",
    "0000093751": "State Street",
    "0001037389": "Renaissance Technologies",
    "0001061165": "Baillie Gifford",
    "0000938836": "FMR (Fidelity)",
    "0001350694": "T. Rowe Price",
    "0001423053": "Geode Capital",
    "0001103804": "Morgan Stanley",
    "0000769993": "Goldman Sachs",
    "0000312769": "JPMorgan Chase",
    "0001567619": "Citadel Advisors",
    "0001166559": "Dimensional Fund Advisors",
    "0000355911": "Wellington Management",
    "0001418814": "Invesco",
    "0000915002": "Northern Trust",
    "0000070858": "Bank of America",
    "0001179392": "Nuveen Asset Management",
    "0000312435": "Franklin Resources",
    "0000893749": "AllianceBernstein",
    "0000868154": "LSV Asset Management",
    "0001688453": "Two Sigma Investments",
    "0001178453": "Legal & General Group",
    "0001336528": "Ameriprise Financial",
}

# 13F 调度：季末日期 → 季末+50天（13F 截止日后）的采集日期
# 例如 Q1 季末 3/31，截止日 5/15 → 采集触发日 5/16
SCHEDULE_13F = {
    1: (3, 31, 5, 16),   # Q1: 季末 3/31，5/16 触发
    2: (6, 30, 8, 15),   # Q2: 季末 6/30，8/15 触发
    3: (9, 30, 11, 15),  # Q3: 季末 9/30，11/15 触发
    4: (12, 31, 2, 15),  # Q4: 季末 12/31，次年 2/15 触发
}
