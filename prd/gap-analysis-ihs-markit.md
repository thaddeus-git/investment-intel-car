# IHS Markit Cross Ownership Report — 差距分析

> **日期**: 2026-06-17
> **状态**: 交付甲方评审用
> **参考**: `/Users/liuming/sec/reference/RLX Q3'21 Cross Ownership Report.md`

---

## 0. 目的

甲方提供了一份 IHS Markit 的 Cross Ownership Report（RLX Technology Q3'2021），希望我们的竞品情报监控系统产出同等质量的交叉持股分析。

本文档逐页拆解 IHS 报告，明确：
1. 哪些维度**已有数据基础、可以直接实现**
2. 哪些维度**能近似但存在精度差距**
3. 哪些维度**完全无法实现**，以及为什么
4. 如果要弥补差距，需要采购什么

---

## 1. IHS Markit 报告逐页功能拆解

IHS 报告共 11 页，结构如下：

| 页码 | 内容 | 核心能力 |
|------|------|---------|
| 1 | 封面（分析师信息、联系方式） | — |
| 2 | **Top Holders Positions** — 25 家机构排名，每家机构在目标公司 + 8 家同业的总持仓、分持仓，附带 Style 和 Turnover 标签 | 13F 持仓 × 同业组交叉矩阵 + 风格/换手率分类 |
| 3 | **Top Holders Changes** — 同上但显示 QoQ 变动金额 | 13F 跨期对比 |
| 4 | **Top 25 Activists** — 知名激进投资者持仓，标注 "Occasional" / "Often" | Activist 机构数据库 |
| 5 | **Top Peer Buyers** — 按 QoQ 增持总额排名 Top 25 | 跨同业组汇总 Δ > 0 |
| 6 | **Top Peer Sellers** — 按 QoQ 减持总额排名 Top 25 | 跨同业组汇总 Δ < 0 |
| 7 | **Top Initiations & Liquidations** — 新建仓 Top 10 + 清仓 Top 10 | 跨期持仓出现/消失 |
| 8 | **Investor Style Comparison** — 饼图 (Active/Passive 取向) + 柱状图 (按取向的资金流向) | 投资取向分类 + 资金流向归因 |
| 9 | **Investor Style Comparison** — 饼图 (12 类风格) + 柱状图 (按风格的资金流向) | 12 类投资风格分类 + 资金流向归因 |
| 10 | **Investor Turnover Comparison** — 饼图 (4 档 turnover) + 柱状图 (按 turnover 的资金流向) | 组合换手率计算 + 资金流向归因 |
| 11 | **Glossary** — 术语定义 | — |

---

## 2. 能力差距逐项分析

### 2.1 完全可实现（13F 数据充足）

以下 6 个维度**数据源 = 13F，我们已采集**，可直接实现：

| # | 维度 | 数据源 | 我方实现路径 |
|---|------|--------|------------|
| 1 | **Top Holders Positions** | 13F | `institutional_holdings` 表 → 按 report_period 筛选 → 按机构 GROUP BY SUM(value_x1000) → 排名。每行 = 机构，每列 = 竞品，交叉格 = 持仓市值 |
| 2 | **Top Holders Changes** | 13F × 2 期 | 已有 `calculate_institutional_signal()` 中跨期对比逻辑，扩展即可 |
| 3 | **Top Peer Buyers** | 13F × 2 期 | 跨 5 家竞品汇总 Δvalue > 0 的机构，按增持总额降序 |
| 4 | **Top Peer Sellers** | 13F × 2 期 | 同上，Δvalue < 0 |
| 5 | **Top Initiations** | 13F × 2 期 | 上期无持仓 → 本期有持仓 |
| 6 | **Top Liquidations** | 13F × 2 期 | 上期有持仓 → 本期无持仓 |

**结论**：IHS 报告中最核心的 **6 页排名表**（第 2-7 页的主要表格）完全可复现。

### 2.2 只能近似实现（数据源存在但精度不足）

#### 2.2.1 Active / Passive 投资取向分类（第 8 页）

**IHS 怎么做：**

IHS Markit 对每家机构的基金产品做投资策略标注（index-tracking → Passive，discretionary stock-picking → Active）。这是基于基金招募说明书 + 持仓特征的综合判断。

**我们缺什么：**

SEC 13F 报告不包含基金的 "investment strategy" 字段。我们无法从 13F 本身判断某家机构是主动还是被动管理。

**我们能做的近似：**

基于**实体类型标签**区分已知指数基金：

```
已知被动/指数基金（实体名称即标识）：
  - Vanguard Group          → Index
  - BlackRock Fund Advisors → Index
  - State Street (SSgA)     → Index
  - Geode Capital           → Index
  - Charles Schwab IM       → Index
  - Northern Trust          → Index
  - Legal & General IM      → Index

其他全部标注为 "Active"（但无法细分策略类型）
```

**差距**：无法区分主动管理中的不同策略（Value / Growth / GARP）。所有非指数基金统一标为 "Active"。

#### 2.2.2 Portfolio Turnover（第 10 页）

**IHS 怎么做：**

IHS 跟踪每个基金的日常交易，计算 12 个月滚动 turnover rate：

```
Turnover = min(buys, sells) / avg(portfolio value)
```

4 档分类：
- Low: 0–33.3%/年
- Medium: 33.3%–66.6%/年
- High: 66.6%–100%/年
- Very Active: >100%/年

**我们缺什么：**

13F 是**季度快照**，不是每日交易流水。我们只能看到季末持仓，无法看到季度内的买卖——一只股票可能在季度内被买入又卖出，在 13F 上完全不可见。

**我们能做的近似：**

基于 QoQ 13F 快照的 **Churn Proxy**：

```
Churn Proxy = Σ|Δvalue_i| / Σ avg(value_i)   （仅针对竞品 peer group）
```

3 档简化分类：
- Low: < 20% QoQ 变动
- Medium: 20%–50%
- High: > 50%

**差距**：
1. 精度：季度快照 ≠ 实际交易量，季末调仓可能被误判
2. 分档：3 档 vs IHS 的 4 档
3. 范围：仅基于 5 只竞品的 13F 持仓变动，不是机构的完整组合 turnover

#### 2.2.3 激进投资者识别（第 4 页）

**IHS 怎么做：**

IHS 维护一个 activist 数据库，追踪：
- 13D 申报（持股 >5% 且意图影响公司经营时必须提交）
- 历史 proxy fight / shareholder proposal 记录
- 对冲基金的 activist campaign 追踪

**我们能做的近似：**

维护一个**静态种子名单**（~8-10 家已知 activist），手动标注 activism_level（"often" / "occasional"）。来源：公开 13D 记录、WhaleWisdom 标签、新闻追踪。

**差距**：静态名单不完整，无法动态发现新 activist。

### 2.3 完全无法实现（数据源缺失）

#### 2.3.1 12 类投资风格细分（第 9 页）⚠️ 核心差距

**IHS 的分类体系：**

| 风格 | 典型特征 |
|------|---------|
| Aggressive Growth | 极高营收/EPS 增速、极高估值倍数、不派息、早期成长阶段 |
| Growth | 增速和倍数高于市场平均，但不过度极端，不敏感于股息率 |
| GARP | 估值低于市场，但预期增速高于市场，持有期较长 |
| Value | 低估值（低 P/E、P/B、PEG），基本面强劲，增速缓慢稳定，长期持有 |
| Deep Value | 极端低估值，公司或行业长期不受市场青睐 |
| Alternative | 对冲基金，策略不属于传统分类 |
| Broker | 经纪商实体 |
| Specialty | 行业/板块特定策略 |
| Yield | 收益导向 |
| Index | 指数跟踪 |
| Venture Capital | 风险投资 |
| Private Equity | 私募股权 |

**IHS 的分类方法论：**

```
对每家机构：
  1. 获取其完整 13F 持仓（可能 500-2000 只股票）
  2. 对每只持仓股票计算基本面指标：
     - P/E（市盈率）
     - P/B（市净率）
     - PEG（市盈率/增速）
     - Revenue Growth（营收增速）
     - EPS Growth（每股收益增速）
     - Dividend Yield（股息率）
  3. 对组合做加权平均，得到组合整体估值特征
  4. 将组合特征与 IHS 的 12 类风格阈值对比，归入最匹配的类别
```

**我们为什么做不了：**

1. **缺完整 13F 持仓**：我们只采集了每家机构在 5 只竞品上的持仓，不是机构的完整 13F。要做风格分类，需要每家机构在**所有股票**上的完整持仓（每家机构可能持有 500-2000 只股票）。

2. **缺美股基本面数据库**：即使拿到完整 13F，也需要对每只股票计算 P/E、P/B、PEG、营收增速等指标。这需要一个覆盖全美股的基本面数据库（FactSet / Bloomberg / Morningstar / Refinitiv）。这些数据库的 API 许可费用通常在 $20K-$100K/年。

3. **缺风格分类模型**：即使有了完整持仓 + 基本面数据，还需要一个分类模型/规则引擎来将组合特征映射到 12 类风格。IHS 的分类方法是其核心 IP，没有公开的阈值或模型。

**需要的投入（如果要实现）：**

| 投入项 | 说明 | 预估成本 |
|--------|------|---------|
| 完整 13F 数据 | 拉取 25-100 家机构的全部 13F 持仓（不是只提取竞品） | 工程投入：~2 周 |
| 美股基本面数据库 | FactSet / Morningstar / Refinitiv API | $20K-$100K/年 |
| 风格分类模型 | 基于组合特征聚类的分类引擎 | 工程投入：~4 周 + 量化研究员 |
| 合计 | — | 至少 $20K/年 + 6 周工程 |

#### 2.3.2 完整 Peer Average（贯穿全报告）

IHS 报告的每张表都有 "Peer Average" 列（同业组均值）。IHS 的同业组通常包含 8-15 家可比公司，基于行业分类 (GICS/ICB) + 市值区间 + 业务模式匹配。

我们的同业组只有 5 家（CVNA/KMX/AN/UXIN/ATHM），且这 5 家本身差异较大（3 家美国经销商 + 2 家中国 ADR）。算出的"均值"不完全代表行业基准。

#### 2.3.3 资本流向归因图（第 8-10 页的柱状图）

IHS 报告第 8-10 页的柱状图展示 "Capital Flows by Investor Orientation/Style/Turnover"。这些图依赖两个前提：
1. 将每个机构正确分类（风格/取向/turnover）—— 即上述 2.3.1 / 2.2.1 / 2.2.2 的前提
2. 计算每个分类的 QoQ 资金净流入/流出

分类本身做不准确，归因图就出不来。我们可以画一个简化版（按我们自己的 3 类标签做归因），但需要加注"基于简化分类，非 IHS 等价"。

---

## 3. 差距汇总表

| # | IHS 能力 | 我们的能力 | 差距等级 | 原因 | 弥补途径 |
|---|---------|-----------|---------|------|---------|
| 1 | Top Holders 排名 | ✅ 完全可复现 | 🟢 无 | 13F 数据充足 | — |
| 2 | Top Holders QoQ 变动 | ✅ 完全可复现 | 🟢 无 | 跨期对比逻辑已实现 | — |
| 3 | Top Buyers/Sellers 排名 | ✅ 完全可复现 | 🟢 无 | 聚合排名 | — |
| 4 | Initiations/Liquidations | ✅ 完全可复现 | 🟢 无 | 跨期存在性判断 | — |
| 5 | Activist 识别 | ⚠️ 静态名单 (~8 家) | 🟡 精度差距 | 缺 13D 追踪数据库 | 采购 WhaleWisdom / SharkWatch 数据 |
| 6 | Active/Passive 取向 | ⚠️ 实体类型标签 (~7 家 Index，其余 Active) | 🟡 精度差距 | 缺基金策略标注 | 采购 Morningstar Fund Data |
| 7 | Portfolio Turnover | ⚠️ Churn proxy (3 档) | 🟡 精度差距 | 缺日度交易流水 | 采购 FactSet Ownership 或 Bloomberg PORT |
| 8 | **12 类投资风格** | **❌ 不可实现** | 🔴 核心差距 | 缺完整 13F + 基本面数据库 + 分类模型 | 采购 Morningstar / FactSet / IHS Markit |
| 9 | Peer Average | ⚠️ 5 家均值 | 🟡 口径差距 | 同业组较小 | 扩展同业组（添加更多二手车/汽车零售美股） |
| 10 | 资本流向归因图 | ⚠️ 简化版(3 类) | 🟡 依赖差距 | 依赖 #6/#7/#8 的分类 | 分类完善后归因自动跟进 |
| 11 | 机构覆盖率 | ⚠️ 25 家种子 | 🟡 覆盖差距 | 反向查询需要种子机构列表 | SEC EDGAR Full-Text Search 发现更多 13F 申报人 |

---

## 4. 如果甲方需要完整版：采购清单

### 4.1 核心差距：投资风格分类

> **需要以下至少一项：**

| 方案 | 数据服务 | 覆盖内容 | 预估年费 | 集成难度 |
|------|---------|---------|---------|---------|
| A | **Morningstar Direct** | 基金风格分类 (Style Box)、组合持仓、Fund Flow | $15K-$30K | 中（API + 数据映射） |
| B | **FactSet Ownership** | 机构持仓、风格标签、13F 汇总 | $25K-$50K | 高（终端 + API） |
| C | **Bloomberg PORT** | 组合分析、风格归因、peer analysis | $24K+/终端 | 高（Bloomberg 生态） |
| D | **IHS Markit (现 S&P Global)** | 直接采购 Cross Ownership Report | 按报告定价 | 零（成品报告） |

### 4.2 次要差距：Activist 识别

| 方案 | 数据服务 | 覆盖内容 |
|------|---------|---------|
| E | **WhaleWisdom** | 13F 汇总 + 13D/G 追踪，有 free tier |
| F | **SharkWatch (FactSet)** | Activist 追踪、13D 申报监控 |

### 4.3 缩小机构覆盖差距

| 方案 | 方式 | 成本 |
|------|------|------|
| G | SEC EDGAR Full-Text Search | 搜索 13F-HR + 竞品 ticker，发现所有申报人 | 工程投入 ~1 周 |
| H | NASDAQ.com 机构持有人页面 | 爬虫获取竞品的主要持有人名单 | 反爬风险 |

---

## 5. 建议的分阶段路线

| 阶段 | 内容 | 投入 | 产出 |
|------|------|------|------|
| **现在** | 模块 F MVP：7 个可实现维度 + 简化风格标签 + Churn proxy | ~5 小时工程 | 交叉持股看板 + 静态报告 |
| **近期** | 机构覆盖扩展：SEC Full-Text Search → 发现更多 13F 申报人，种子池 25 → 100+ | ~1 周工程 | 更完整的 Top Holders 排名 |
| **中期** | （需预算）采购 Morningstar 或 WhaleWisdom 数据 → 风格分类升级 | $15K+/年 + 4 周集成 | 接近 IHS 质量的风格分类 |
| **远期** | （需预算）采购完整 IHS Markit Cross Ownership Report → 我方系统 vs IHS 报告交叉验证 | 按报告定价 | 专业级交付 |

---

## 6. 附录：术语参考 (from IHS Markit Glossary)

```
Portfolio Turnover:
  High:        66.6–100% / year
  Low:         0–33.3% / year
  Very Active: >100% / year
  Medium:      33.3%–66.6% / year

Investment Orientation:
  Active  — Subjective analysis by investment professionals
  Passive — Computer-driven models / index replication

Investment Styles:
  Aggressive Growth — Very high revenue/EPS growth, high multiples, no dividends
  Growth — Multiples and growth rates higher than market
  GARP — Discount to market, expected to grow faster than market
  Value — Low valuations, fundamentally strong, slow/steady growth
  Deep Value — Extreme value, companies/industries out of favor
  Specialty — Industry/sector specific focus
  Alternative — Hedge funds, non-traditional strategies
  Index — Index replication
  Broker — Broker/dealer entities
  Yield — Income-oriented
  Venture Capital — VC investments
  Private Equity — PE investments
```
