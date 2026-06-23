"""
竞品情报监控系统 — 配置中心
"""
from pathlib import Path

# ── edgartools identity（SEC 要求，必须设置后调 API） ──
EDGAR_IDENTITY = "CompetitorIntel/1.0 (your-email@company.com)"

# ── 17 家竞品（汽车零售生态对标组） ──
# CIK 必须是 10 位 padded 格式
COMPETITORS = [
    # ── 线上二手车零售商（最直接对标） ──
    {
        "ticker": "CVNA",
        "cik":   "0001690820",
        "name":  "Carvana Co.",
        "name_cn": "Carvana",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "KMX",
        "cik":   "0001170010",
        "name":  "CarMax, Inc.",
        "name_cn": "Carmax",
        "sic":   "5500",
        "has_section16": True,
    },
    # ── 大型经销商集团 ──
    {
        "ticker": "AN",
        "cik":   "0000350698",
        "name":  "AutoNation, Inc.",
        "name_cn": "AutoNation",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "LAD",
        "cik":   "0001023128",
        "name":  "Lithia Motors, Inc.",
        "name_cn": "Lithia",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "PAG",
        "cik":   "0001019849",
        "name":  "Penske Automotive Group, Inc.",
        "name_cn": "Penske",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "GPI",
        "cik":   "0001031203",
        "name":  "Group 1 Automotive, Inc.",
        "name_cn": "Group 1",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "SAH",
        "cik":   "0001043509",
        "name":  "Sonic Automotive, Inc.",
        "name_cn": "Sonic",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "ABG",
        "cik":   "0001144980",
        "name":  "Asbury Automotive Group, Inc.",
        "name_cn": "Asbury",
        "sic":   "5500",
        "has_section16": True,
    },
    # ── 线上汽车交易/信息平台 ──
    {
        "ticker": "CARG",
        "cik":   "0001494259",
        "name":  "CarGurus, Inc.",
        "name_cn": "CarGurus",
        "sic":   "7370",
        "has_section16": True,
    },
    {
        "ticker": "CARS",
        "cik":   "0001683606",
        "name":  "Cars.com Inc.",
        "name_cn": "Cars.com",
        "sic":   "7370",
        "has_section16": True,
    },
    {
        "ticker": "TRUE",
        "cik":   "0001327318",
        "name":  "TrueCar, Inc.",
        "name_cn": "TrueCar",
        "sic":   "7370",
        "has_section16": True,
    },
    # ── 批发拍卖 ──
    {
        "ticker": "KAR",
        "cik":   "0001395942",
        "name":  "OPENLANE, Inc.",
        "name_cn": "OPENLANE",
        "sic":   "5500",
        "has_section16": True,
    },
    {
        "ticker": "ACVA",
        "cik":   "0001637873",
        "name":  "ACV Auctions Inc.",
        "name_cn": "ACV Auctions",
        "sic":   "7389",
        "has_section16": True,
    },
    {
        "ticker": "RUSHA",
        "cik":   "0001012019",
        "name":  "Rush Enterprises, Inc.",
        "name_cn": "Rush Enterprises",
        "sic":   "5500",
        "has_section16": True,
    },
    # ── 中国竞品（ADR） ──
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
    # ── 线上二手车（濒临退市，保留监控） ──
    {
        "ticker": "VRM",
        "cik":   "0001580864",
        "name":  "Vroom, Inc.",
        "name_cn": "Vroom",
        "sic":   "5500",
        "has_section16": True,
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
# 用绝对路径，避免 Streamlit Cloud 等环境下 CWD 不匹配的问题
DATABASE_PATH = str(Path(__file__).resolve().parent.parent / "data" / "competitor_intel.db")

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
# 31 家种子机构，CIK 经 SEC JSON API + 13F 申报 + SEC EDGAR 网页搜索三重验证（2026-06-18 R2）。
# R2 新增 6 家：Baillie Gifford / Geode / Nuveen / AllianceBernstein / LSV / Legal & General
TOP_INSTITUTIONS = {
    # ── Index / Passive ──
    "0000102909": "Vanguard Group",
    "0001364742": "BlackRock",
    "0000093751": "State Street",
    "0000073124": "Northern Trust",
    "0001214717": "Geode Capital Management",
    # ── Broker / Dealer ──
    "0000895421": "Morgan Stanley",
    "0000886982": "Goldman Sachs Group",
    # ── Active — 资产管理巨头 ──
    "0000315066": "FMR LLC (Fidelity)",
    "0000080255": "T. Rowe Price Associates",
    "0000354204": "Dimensional Fund Advisors",
    "0000914208": "Invesco",
    "0000820027": "Ameriprise Financial",
    "0000038777": "Franklin Resources",
    "0000070858": "Bank of America",
    "0000019617": "JPMorgan Chase",
    "0001088875": "Baillie Gifford & Co",
    "0001521019": "Nuveen Asset Management",
    "0001109448": "AllianceBernstein",
    "0001050470": "LSV Asset Management",
    "0000764068": "Legal & General Group",
    # ── Active — 对冲基金 / Alternative ──
    "0001037389": "Renaissance Technologies",
    "0001423053": "Citadel Advisors",
    "0001179392": "Two Sigma Investments",
    "0000902219": "Wellington Management",
    "0001350694": "Bridgewater Associates",
    "0001103804": "Viking Global Investors",
    "0001061165": "Lone Pine Capital",
    "0001418814": "ValueAct Holdings",
    "0001336528": "Pershing Square Capital",
    # ── 其他 ──
    "0001067983": "Berkshire Hathaway",
    "0001166559": "Bill & Melinda Gates Foundation Trust",
}

# 13F 调度：季末日期 → 季末+50天（13F 截止日后）的采集日期
# 例如 Q1 季末 3/31，截止日 5/15 → 采集触发日 5/16
SCHEDULE_13F = {
    1: (3, 31, 5, 16),   # Q1: 季末 3/31，5/16 触发
    2: (6, 30, 8, 15),   # Q2: 季末 6/30，8/15 触发
    3: (9, 30, 11, 15),  # Q3: 季末 9/30，11/15 触发
    4: (12, 31, 2, 15),  # Q4: 季末 12/31，次年 2/15 触发
}

# ── 模块 F：交叉持股分析 — 机构风格静态标签 ──
# ⚠️ 这是基于实体类型的简化分类，非 IHS Markit 专业风格标签（12 类）。
# CIK 经 2026-06-18 SEC JSON API 全面验证修正。

# 激进投资者名单（来源：公开 13D 记录 / WhaleWisdom）
# activism_level: "often" = 有多次 activist campaign 记录, "occasional" = 偶尔参与
ACTIVIST_INSTITUTIONS = {
    "0000315066": {"name": "FMR LLC (Fidelity)", "activism_level": "occasional"},
    "0000080255": {"name": "T. Rowe Price Associates", "activism_level": "occasional"},
    "0001423053": {"name": "Citadel Advisors", "activism_level": "often"},
    "0001179392": {"name": "Two Sigma Investments", "activism_level": "often"},
    "0001037389": {"name": "Renaissance Technologies", "activism_level": "occasional"},
    "0000902219": {"name": "Wellington Management", "activism_level": "often"},
    "0001418814": {"name": "ValueAct Holdings", "activism_level": "often"},
    "0001336528": {"name": "Pershing Square Capital", "activism_level": "often"},
    "0001088875": {"name": "Baillie Gifford & Co", "activism_level": "occasional"},
    "0001050470": {"name": "LSV Asset Management", "activism_level": "occasional"},
}

# 机构投资风格简化标签（3 类：Index / Active / Broker）
INSTITUTION_STYLES = {
    "0000102909": "Index",       # Vanguard Group
    "0001364742": "Index",       # BlackRock
    "0000093751": "Index",       # State Street
    "0000073124": "Index",       # Northern Trust
    "0001214717": "Index",       # Geode Capital Management
    "0000895421": "Broker",      # Morgan Stanley
    "0000886982": "Broker",      # Goldman Sachs Group
    "0000315066": "Active",      # FMR LLC (Fidelity)
    "0000080255": "Active",      # T. Rowe Price Associates
    "0000354204": "Active",      # Dimensional Fund Advisors
    "0000914208": "Active",      # Invesco
    "0000820027": "Active",      # Ameriprise Financial
    "0000038777": "Active",      # Franklin Resources
    "0000070858": "Active",      # Bank of America
    "0000019617": "Active",      # JPMorgan Chase
    "0001088875": "Active",      # Baillie Gifford & Co
    "0001521019": "Active",      # Nuveen Asset Management
    "0001109448": "Active",      # AllianceBernstein
    "0001050470": "Active",      # LSV Asset Management
    "0000764068": "Active",      # Legal & General Group
    "0001037389": "Active",      # Renaissance Technologies
    "0001423053": "Active",      # Citadel Advisors
    "0001179392": "Active",      # Two Sigma Investments
    "0000902219": "Active",      # Wellington Management
    "0001350694": "Active",      # Bridgewater Associates
    "0001103804": "Active",      # Viking Global Investors
    "0001061165": "Active",      # Lone Pine Capital
    "0001418814": "Active",      # ValueAct Holdings
    "0001336528": "Active",      # Pershing Square Capital
    "0001067983": "Active",      # Berkshire Hathaway
    "0001166559": "Active",      # Gates Foundation Trust
}
