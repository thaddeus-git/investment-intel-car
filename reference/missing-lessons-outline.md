# 缺失课程提纲 — 机构投资者 / 13F / 内部人交易 / 交叉持股方法论

> 前 5 课（0001-0005）覆盖了 SEC 披露基础、财务指标、8-K、Earnings Call、竞品框架。
> 以下 4 课（0006-0009）补全机构投资者与内部人交易领域，是模块 F（交叉持股分析）的前提知识。

---

## 0006: 13F 与机构持仓 — 交叉持股分析的数据底座

### 6.1 13F 是什么

- Section 13(f) of the Securities Exchange Act of 1934
- 管理资产 > $1 亿的机构投资者必须每季度提交
- 提交截止日：季度结束后 45 天内（Q1 → 5/15，Q2 → 8/14，Q3 → 11/14，Q4 → 2/14）
- 约 5,000-6,000 家机构提交

### 6.2 13F 里有什么

- 每只持仓股票的：名称、CUSIP、**股数**、**市值（value_x1000 = 千美元）**
- 只有多头仓位（long positions），不含空头、不含衍生品
- 不含做空仓位、不含美国以外的持仓、不含非上市证券
- **只显示季末持仓快照**，不显示季度内的交易（一只股票可能在季中买入又卖出，13F 上完全不可见）

### 6.3 13F 的核心限制（直接影响你的系统设计）

| 限制 | 含义 | 对你的系统的影响 |
|------|------|----------------|
| 45 天滞后 | 季末 3/31 的持仓，最早 5/15 才能看到 | 报告上的"当前持仓"其实是 1.5-4.5 个月前的 |
| 只能正向查 | 你可以查"Vanguard 持有哪些股票"，但不能查"谁持有了 CVNA" | 必须维护种子机构列表，逐个拉取 13F 后 grep 竞品 ticker |
| 只有季末快照 | 看不到季度内的买卖 | Turnover 只能用 Churn Proxy 估算，做不到 IHS 精度 |
| 只有多头 | 看不到做空仓位 | 看不到对冲基金的空头押注（而这可能是最重要的信号） |
| value_x1000 单位 | 字段名是 `value_x1000`，值是千美元（即 value_x1000=1000 表示 $1,000,000） | 单位转换是常见 bug 来源——多除或少除一个 1000 会让金额差 1000 倍 |

### 6.4 13F 的单位转换陷阱

```
SEC 原始数据: value = 12345 → 表示 $12,345,000 → $12.345M
edgartools 返回: 字段名 value_x1000 → 值 12345 → 含义: 12345 × $1,000 = $12,345,000

常见错误:
  错误 A: 把 value_x1000 当成 $K 展示 → 金额变成实际的 1/1000
  错误 B: 展示时又除以 1000 → 金额变成实际的 1/1,000,000
  错误 C: 机构在不同竞品的 value_x1000 量级差异极大（CVNA 大市值 vs UXIN 小市值）
          → 汇总时不加区分地 SUM 会导致 UXIN 的变动被淹没

验证方法:
  取 Vanguard 在 CVNA 的 13F 原始 XML，手工读取 value 字段
  与 edgartools 解析后的 value_x1000 对比
  再用 CVNA 当时的股价 × 股数 交叉验证
```

### 6.5 13F 对比的正确姿势

```
跨期对比的边界条件:
  1. 机构可能只在某一期有数据（新建仓 / 清仓）
  2. 同一机构可能用不同实体名提交（CIK 相同但名称不同）
  3. 机构可能拆分/合并持仓（股数变了但市值没变 = 股价变动，不是主动调仓）
  4. 股数变化 < 市价变动幅度 → 可能是拆股/合股，不是主动买卖

正确的对比逻辑:
  curr = holdings WHERE report_period = '2026-03-31'
  prev = holdings WHERE report_period = '2025-12-31'
  merged = curr FULL OUTER JOIN prev ON (institution_cik, ticker)

  for each row:
    if curr_value > 0 and prev_value == 0:  → Initiation (新建仓)
    if curr_value == 0 and prev_value > 0:  → Liquidation (清仓)
    if curr_value > prev_value:              → Increase (加仓)
    if curr_value < prev_value:              → Decrease (减仓)
    if curr_value == prev_value:             → No change (不变)

  注意: 当 prev_value = 0 时，不要计算 delta_pct（除零错误）
  注意: institution_name 可能只在某一期出现，outer join 后需要补全
```

### 6.6 13F 在交叉持股分析中的角色

```
输入: 25 家种子机构 × 5 家竞品 × 2 期 13F
输出:
  - 交叉持股矩阵 (机构 × 竞品)
  - QoQ 持仓变动 (delta value per institution per ticker)
  - 跨竞品 Buyers/Sellers 排名
  - Initiations & Liquidations
  - Churn Proxy (turnover 近似)

不可输出（因为 13F 数据不包含）:
  - 投资风格分类 (Value/Growth/GARP) → 需要全持仓 + 基本面数据库
  - 精确 turnover → 需要日度交易流水
  - 做空仓位 → 需要 13D/G 或其他数据源
```

---

## 0007: Form 3/4/5 与内部人交易 — 高管在用脚投票

### 7.1 Section 16 是什么

- 1934 年证券交易法第 16 条
- 管辖对象：高管（officer）、董事（director）、持股 >10% 的股东
- 管辖范围：持有**美国注册证券**的内部人
- **关键限制**：中国 ADR 公司的高管通常持有开曼/VIE 层面权益，不受 Section 16 管辖

### 7.2 三张表的区别

| 表格 | 全称 | 什么时候提交 | 内容 |
|------|------|-------------|------|
| Form 3 | Initial Statement of Beneficial Ownership | 成为内部人后 10 天内 | 初始持股声明（一次性） |
| Form 4 | Statement of Changes in Beneficial Ownership | **交易后 2 个工作日内** | 每次买卖都要报 |
| Form 5 | Annual Statement of Beneficial Ownership | 财年结束后 45 天内 | 年度汇总（某些豁免交易） |

### 7.3 Form 4 的结构

```
Table I (Non-Derivative Securities): 直接持股的买卖
  - Title of Security: 股票名称
  - Transaction Date: 交易日
  - Transaction Code: P=买入, S=卖出, G=赠与, A=授予(期权行权), F=交税卖出
  - Amount: 股数
  - Price: 单价
  - Amount After: 交易后持股数

Table II (Derivative Securities): 期权的行权/授予
  - 期权行权 → 通常会触发同日 Table I 的卖出（行权后立刻卖掉 = cashless exercise）
  - 看 Table II 的 Code M (行权) + Table I 的 Code F (交税卖出) 组合

常见误判:
  - Code F（交税卖出）≠ 看空信号。期权行权时预扣税是标准操作。
  - Code G（赠与）可能是慈善捐赠或家族信托，不一定代表看空。
  - Code A（授予期权）是薪酬的一部分，不是主动买入。
  - 真正有信号意义的是 Code P（主动买入）和 Code S（主动卖出，且金额显著）。
```

### 7.4 内部人情绪指标的正确设计

```
错误做法: 简单统计买卖笔数 → 10-K 前大量交税卖出被误判为"内部人看空"
正确做法: 加权算法

InsiderSentiment = Σ(买入金额_i × 角色权重_i × 交易代码权重_i)
                  - Σ(卖出金额_j × 角色权重_j × 交易代码权重_j)

角色权重:
  CEO / CFO                   2.0  (最高决策层)
  10% Owner                   1.5  (大股东)
  Director                    1.0  (董事会)
  EVP / SVP / VP              0.8  (执行层)

交易代码权重:
  P (主动买入)                1.0
  S (主动卖出)                1.0
  F (交税卖出)                0.2  (大部分是无害的)
  G (赠与)                    0.3  (可能是税务规划)
  A (期权授予)                0.0  (不计入，不是主动交易)
  M (期权行权)                0.0  (只看行权后的卖出行为)

结果归一化到 [-100, +100]:
  > +30 → 看多,  -30~+30 → 中性,  < -30 → 看空
```

### 7.5 Form 144 — 减持的"预告片"

- Form 144 是**事前通知**：内部人计划在未来 90 天内出售 ≥5,000 股或 ≥$50,000
- 不等于"已经卖了"，但等于"准备卖"
- 单笔减持计划可能持续数月，多份 Form 144 连续提交 = 持续减持信号
- **注意**：Form 144 的 `aggregate market value` 是"预估市值"（基于提交日的股价估算），实际成交价可能不同

### 7.6 中国竞品的特殊处理

```
为什么 UXIN/ATHM 没有 Form 4:
  - 优信和汽车之家是在美上市的中国公司（ADR 结构）
  - 中国籍高管/董事通常不持有美国注册证券
  - 他们持有的是开曼群岛控股公司或 VIE 层面的权益
  - 这些权益不受 SEC Section 16 管辖

系统处理:
  - config.py 中标记 has_section16 = False
  - Dashboard 上显示 "不适用" 而非 "无数据"
  - 如果中国竞品突然出现 Form 4 → 这是极重要信号（罕见事件，说明内部人特意注册了美股证券来交易）
```

---

## 0008: Schedule 13D/G 与 Activist 投资者 — 谁在试图改变公司

### 8.1 13D vs 13G

| | Schedule 13D | Schedule 13G |
|---|-------------|-------------|
| 触发条件 | 持股 >5% **且**意图影响公司经营 | 持股 >5% 但**无意**影响经营 |
| 提交时限 | 达到 5% 后 10 天内 | 年度提交（被动投资者） |
| 谁提交 | Activist 投资者、对冲基金 | 指数基金、养老金 |
| 后续更新 | 每次买入/卖出 >1% 都要更新 | 年度更新即可 |
| 竞品监控价值 | ⭐⭐⭐⭐⭐ 极高 | ⭐ 低（被动持有） |

### 8.2 怎么判断一家机构是 Activist

```
IHS Markit 的判断标准（我们无法完全复刻）:
  1. 历史上有无 13D 申报记录
  2. 有无 proxy fight / shareholder proposal 记录
  3. 有无公开的 activist campaign（如公开信要求换 CEO、分拆业务等）
  4. 对冲基金背景 + 集中持仓 + 高换手率

我们能做的近似:
  1. 维护静态名单（已知 activist funds，来源：WhaleWisdom 13D 追踪、公开新闻）
  2. 标注 activism_level: "often" (多次 campaign) / "occasional" (偶尔参与)
  3. 定期从 WhaleWisdom / SEC EDGAR 搜索 13D 申报补充名单
```

### 8.3 常见 Activist 策略类型

| 策略 | 典型动作 | 信号 |
|------|---------|------|
| 董事会席位 | 提名自己的董事候选人 | 最高级别——试图从内部改变公司 |
| 资本配置 | 要求分红、回购、分拆业务 | 对现有管理层的不信任投票 |
| M&A 推动 | 要求公司出售自己或收购目标 | 可能引发控制权变更 |
| 运营改善 | 要求削减成本、换 CEO、改变战略 | 认为公司有隐藏价值 |

### 8.4 在交叉持股报告中的展示

```
IHS P4 格式:
  Rank | Institution | Activism Level | Total Holdings ($M) | Peer1 | Peer2 | ...

我们的当前状态:
  - config.py 有 ACTIVIST_INSTITUTIONS (8 家)
  - institution_styles 表有 activism_level 字段
  - Dashboard 有 "Activists" Tab
  - Markdown 报告有 Activist 章节

差距:
  - 静态名单仅 8 家，IHS 示例报告 12 家
  - 无法动态发现新 activist（需要 13D 追踪数据库）
  - 采购建议: WhaleWisdom ($500-2000/年) 或 SharkWatch (FactSet, 价格不详)
```

---

## 0009: 交叉持股分析的方法论 — 这份报告到底在回答什么问题

### 9.1 每页 IHS 报告回答的投资问题

| 页 | 表格/图表 | 回答的投资问题 | 下游怎么用 |
|----|---------|--------------|-----------|
| P2 | Top Holders Positions | **谁在重仓我们和竞争对手？** | 识别"聪明钱"的配置——哪些大机构同时持有我们和竞品 |
| P3 | Top Holders Changes | **这些大机构在加仓还是减仓？** | 判断机构情绪的边际变化——比静态持仓更有信息量 |
| P4 | Activists | **哪些激进投资者在盯着我们或竞品？** | 预警潜在的 activist campaign——activists 可能要求公司出售、换 CEO、分拆 |
| P5 | Top Buyers | **谁在积极买入竞品赛道？** | 发现新进入的资金——可能意味着有人看好这个行业 |
| P6 | Top Sellers | **谁在撤离竞品赛道？** | 预警资金外流——可能意味着行业基本面在恶化 |
| P7 | Initiations / Liquidations | **谁刚进入/完全退出？** | 最极端的信号——新建仓 = 强烈看多，清仓 = 强烈看空 |
| P8 | Active/Passive 取向 | **持仓主要是主动管理还是被动跟踪？** | 被动资金多 = 持仓变动可能是指数调仓而非主动判断 |
| P9 | 投资风格分类 | **持有者是价值型还是成长型投资者？** | 价值投资者增持 vs 成长投资者减持 = 公司生命周期在变化 |
| P10 | Turnover 分布 | **持有者在频繁交易还是长期持有？** | 高频交易者持仓 = 信号噪音大；长期持有者变动 = 信号强 |

### 9.2 跨页之间的信号叠加

```
单个信号的价值有限。真正的洞察来自跨页交叉验证:

场景 1: 确认性信号
  P2: 某机构是 Top 1 持有人
  P3: 该机构大幅加仓
  P5: 该机构是 Top Buyer
  → 强烈的看多信号（大机构在真金白银加仓）

场景 2: 矛盾性信号（需要深入分析）
  P2: 某机构是 Top 3 持有人
  P3: 该机构小幅减仓
  P6: 但该机构不是 Top Seller（减仓金额不够大）
  → 可能是正常的组合再平衡，不一定是看空

场景 3: 危险信号
  P4: 某 activist 机构持有竞品
  P7: 该 activist 本期新建仓（首次进入）
  → 高度关注——activist 进入往往意味着即将推动变革

场景 4: 行业转向信号
  P8: 被动资金占比上升，主动资金占比下降
  P9: 成长型投资者减仓，价值型投资者加仓
  → 行业从"高增长预期"转向"价值重估"
```

### 9.3 正确的报告阅读顺序（给甲方/老板看的）

```
30 秒速览:
  1. 概览卡片: 覆盖机构数、新建仓数、退出数
  2. Top 3 Holders 是谁

2 分钟快速判断:
  3. Top Buyers / Sellers 前 5 名
  4. 有没有 Activist 新进入

5 分钟深度分析:
  5. Initiations 前 5 + Liquidations 前 5
  6. 跨页交叉验证（上述 4 种场景）
  7. 风格/Turnover 趋势变化

你的 Dashboard 和报告应该支持这个阅读顺序
→ 概览卡片 → Top Holders → Buyers/Sellers → Activists → Init/Liq → Charts
```

### 9.4 输出质量的自检清单

```
交付甲方前，逐项检查:

□ 所有表格的金额单位是否一致（$M）
□ 所有排名是否按正确指标排序（不是按字母序、不是按 CIK）
□ 是否存在 institution_name = None 的行
□ 是否存在 delta_pct = inf 或 NaN（除零未处理）
□ Buyers 和 Sellers 是否都有数据（如果某一边完全为空，99% 是 bug）
□ 跨期对比的上期和本期是否确实是相邻两期（不是跳期对比）
□ 所有免责声明是否在报告中可见（不是在代码注释里）
□ 数据时滞是否在报告日期中体现（"数据截止 2026-03-31" vs "报告生成日 2026-06-17"）
```

### 9.5 从 IHS 报告学到的设计原则

```
1. 每页回答一个问题（不要把所有东西堆在一页上）
2. 每页底部留 Commentary 空白（分析师要写判断，系统只提供数据）
3. 表格列要对齐 IHS 的列定义（Style、Turnover、Peer Average 缺一不可）
4. 排名表必须有明确的排名指标（按什么排的）
5. 图表和表格配套（饼图看分布，柱状图看资金流向，互相印证）
6. 免责声明必须可见（不是"在文档末尾有一行小字"）
```

---

## 附录 A：推荐在线视频课程

> 搜索结果表明：中文互联网上几乎没有专门讲 13F / Form 4 / 内部人交易的系统课程。
> 以下推荐以英文 YouTube + 专业平台为主。

### A.1 SEC 披露体系入门

| # | 课程/频道 | 平台 | 时长 | 适合 |
|---|----------|------|------|------|
| 1 | **"How to Read an Annual Report (10-K)"** — The Plain Bagel | YouTube | ~15 min | 10-K/10-Q 入门，动画讲解，零基础友好 |
| 2 | **"SEC Filings Explained"** — Wall Street Survivor | YouTube | ~10 min | 快速了解 10-K/10-Q/8-K 的区别 |
| 3 | **SEC EDGAR Full-Text Search Tutorial** — SEC 官方 | YouTube / SEC.gov | ~20 min | 学会用 EDGAR 搜索任意 filing，找到持有某只股票的机构 |

### A.2 13F 机构持仓分析

| # | 课程/频道 | 平台 | 时长 | 适合 |
|---|----------|------|------|------|
| 4 | **"How to Use 13F Filings to Track Institutional Investors"** — Everything Money | YouTube | ~12 min | 讲清楚 13F 能做什么、不能做什么、45 天滞后意味着什么 |
| 5 | **WhaleWisdom 官方教程** | YouTube (WhaleWisdom 频道) | 系列视频 | 用 WhaleWisdom 工具追踪 13F 变动，可视化机构持仓变化 |
| 6 | **Dataroma 官网** | dataroma.com (免费) | 自定节奏 | 不需要视频——直接在网站上看到 Superinvestors 的 13F 持仓变动，最好的"看实际数据学习"的方式 |

### A.3 内部人交易 (Form 4 / 144)

| # | 课程/频道 | 平台 | 时长 | 适合 |
|---|----------|------|------|------|
| 7 | **"Insider Trading: What Form 4 Filings Tell Us"** — Investor's Business Daily | YouTube | ~8 min | Form 4 基础解读，哪些买卖有信号、哪些没有 |
| 8 | **OpenInsider 官网** | openinsider.com (免费) | 自定节奏 | 最好的内部人交易数据网站。直接搜索 CVNA/KMX/AN，看实际 Form 4 数据长什么样。对着数据理解 Table I / Table II 的区别 |

### A.4 对冲基金 / Activist 策略

| # | 课程/频道 | 平台 | 时长 | 适合 |
|---|----------|------|------|------|
| 9 | **"Activist Investing"** — Aswath Damodaran (NYU Stern) | YouTube | ~20 min | 达摩达兰讲 activist investing 的本质——什么时候 activist 会介入、为什么 |
| 10 | **"13D Monitor"** — Schulte Roth & Zabel | YouTube / 13Dmonitor.com | 系列视频 | 专业追踪 activist campaigns 的机构，了解 13D 和 13G 的区别 |

### A.5 综合投资分析平台（免费层足够）

| # | 平台 | 核心功能 | 对你的价值 |
|---|------|---------|-----------|
| 11 | **WhaleWisdom** (whalewisdom.com) | 13F 汇总 + 13D/G 追踪，free tier 可见 Top 机构持仓 | 快速验证你的 13F 采集结果是否正确（对比同一个机构在同一竞品的持仓数字） |
| 12 | **Dataroma** (dataroma.com) | Superinvestors 的 13F 持仓变动可视化 | 参考它的交叉持股视角——它怎么展示"一个机构持有多只股票" |
| 13 | **OpenInsider** (openinsider.com) | Form 4 内部人交易实时聚合 | 验证你的 insider_tracker 采集结果 |

### A.6 学习方法建议

```
不要看完全部视频再开始工作。按需学习：

优先级 1（今天就看）:
  视频 #1 (How to Read 10-K, 15 min)
  网站 #13 (OpenInsider, 10 min 浏览)
  网站 #11 (WhaleWisdom, 10 min 浏览)

优先级 2（本周末）:
  视频 #4 (13F 讲解, 12 min)
  网站 #12 (Dataroma, 15 min 浏览)
  视频 #7 (Form 4 讲解, 8 min)

优先级 3（有空再看）:
  视频 #9 (Activist Investing, 20 min)
  视频 #3 (EDGAR 搜索教程, 20 min)

总投入: ~2 小时视频 + 30 分钟浏览网站
```

---

## 附录 B：课程之间的依赖关系

```
0001 SEC Filing 101
  ├── 0002 财务指标 (依赖 0001: 知道 10-K/10-Q 是什么)
  ├── 0003 8-K 事件 (依赖 0001: 知道 8-K 是什么)
  ├── 0007 Form 3/4/5 内部人交易 (依赖 0001: 知道 SEC 披露体系)
  └── 0006 13F 机构持仓 (依赖 0001: 知道 SEC 披露体系)

0004 Earnings Call (独立，但依赖 0002 的财务知识)
0005 竞品框架 (依赖 0002+0003+0004: 综合运用)
0008 13D/G Activist (依赖 0006: 理解 13F 后才能理解 13D 的区别)
0009 交叉持股方法论 (依赖 0006+0007+0008: 综合运用)

推荐学习顺序:
  0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 → 0008 → 0009
  (前 5 课已学完，现在从 0006 开始)
```
